"""Links that open one agent-ledger entry as a read-only web page (fork, owner's design 2026-11-18).

The owner reads ledger entries an agent points them to; a ref alone means
asking an agent to read it out. `ledger_get`, `ledger_add` and
`ledger_update` give each entry a `view_url`: a link to `/transfer/ledger`
whose fragment is a token signed by this server. The link is its own
permission, like `request_download`'s (no login), and follows the same rules
(`docs/architecture/file-transfer.md`):

- the token travels in the URL **fragment**, never sent to the server, and is
  redeemed only from `Authorization: Bearer` by `/transfer/ledger/info`;
- **a link never outlives the credential that minted it**: an API key's link
  dies when the key is revoked, deactivated, expired or reassigned; an OAuth
  one when its grant family is revoked, has no live token left or passes its
  absolute deadline (#326). The grant, not the access token: an access token
  lives an hour, and the owner chose links that last `LINK_DAYS`;
- every refusal is the transfer routes' uniform 404, its reason only in the
  `transfer_refused` log record.

Signed rather than stored (an AI choice the owner agreed to with the design):
no table, nothing to sweep; the cost is that a link cannot be withdrawn
before it expires except by revoking the credential behind it. The token
names the entry by ref (refs never change) and the owner it was minted for;
the page reads it in that owner's scope only.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import hmac
import json
from dataclasses import dataclass, replace

from sqlalchemy import func, select

from src.config import settings
from src.models.db import APIKey, OAuthToken, User

#: How long a link opens its entry (owner, 2026-11-18), at most: never past
#: the minting credential's own end.
LINK_DAYS = 30
#: The page every link opens; the token follows `#`.
PAGE_PATH = "/transfer/ledger"
#: The first field of every token: a later format gets a new one.
VERSION = "lv1"
#: Anything longer is refused before it is decoded (a real token is ~150).
MAX_TOKEN_CHARS = 512
_KEY_LABEL = b"obsidian-mcp ledger view link v1"


@dataclass(frozen=True)
class Claims:
    """What a link carries. Exactly one of `key_id` / `grant_id`."""

    user_id: int | None
    ref: str
    key_id: int | None
    grant_id: str | None
    expires: int  # epoch seconds, UTC

    @property
    def expires_at(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self.expires, tz=datetime.timezone.utc)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=datetime.timezone.utc)


def _key() -> bytes:
    # Derived from SECRET_KEY under its own label, as `oauth/cimd.py` derives
    # its id key: rotating SECRET_KEY kills every link, which is the point.
    return hmac.new(settings.secret_key.encode(), _KEY_LABEL, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes | None:
    if not text or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in text):
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def encode(claims: Claims) -> str:
    payload = json.dumps(
        [VERSION, claims.user_id, claims.ref, claims.key_id, claims.grant_id, claims.expires],
        separators=(",", ":"), ensure_ascii=True,
    ).encode("ascii")
    mac = hmac.new(_key(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(mac)}"


def decode(token: str, *, now: datetime.datetime | None = None) -> Claims | None:
    """The claims of a genuine, unexpired token; None for anything else."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
        return None
    payload_text, mac_text = token.split(".")
    payload, mac = _unb64(payload_text), _unb64(mac_text)
    if payload is None or mac is None:
        return None
    if not hmac.compare_digest(hmac.new(_key(), payload, hashlib.sha256).digest(), mac):
        return None
    try:
        fields = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not (isinstance(fields, list) and len(fields) == 6 and fields[0] == VERSION):
        return None
    _, user_id, ref, key_id, grant_id, expires = fields
    if not (
        (user_id is None or type(user_id) is int)
        and isinstance(ref, str) and 0 < len(ref) <= 16
        and type(expires) is int
        and ((type(key_id) is int and grant_id is None) or (key_id is None and isinstance(grant_id, str)))
    ):
        return None
    claims = Claims(user_id=user_id, ref=ref, key_id=key_id, grant_id=grant_id, expires=expires)
    if claims.expires_at <= (now or _now()):
        return None
    return claims


def url_for(claims: Claims) -> str | None:
    base = settings.public_base_url
    return f"{base}{PAGE_PATH}#{encode(claims)}" if base else None


def for_ref(claims: Claims, ref: str) -> str | None:
    """A link to another entry, for the page: the same owner, credential and
    end as the link that opened it, so following links never extends one."""
    return url_for(replace(claims, ref=ref))


async def principal_claims(session, *, key_id, oauth_token_id, user_id) -> Claims | None:
    """The credential half of a new link, or None when no link can be given.

    None with no credential behind the call (single-user without a key, the
    sandbox), with a credential no longer valid, or without a public origin
    to link to. The ref is filled in by `link`.
    """
    if settings.public_base_url is None:
        return None
    now = _now()
    end = now + datetime.timedelta(days=LINK_DAYS)
    if key_id is not None:
        key = await session.get(APIKey, key_id)
        if key is None or not _key_ok(key, user_id, now):
            return None
        if key.expires_at is not None:
            end = min(end, _aware(key.expires_at))
        return Claims(user_id=user_id, ref="", key_id=key_id, grant_id=None, expires=int(end.timestamp()))
    if oauth_token_id is not None:
        token = await session.get(OAuthToken, oauth_token_id)
        if token is None or token.revoked or token.user_id != user_id or _aware(token.expires_at) <= now:
            return None
        deadline = await _grant_deadline_if_live(session, token.grant_id, user_id, now)
        if deadline is None:
            return None
        end = min(end, deadline)
        return Claims(user_id=user_id, ref="", key_id=None, grant_id=token.grant_id, expires=int(end.timestamp()))
    return None


def link(claims: Claims | None, ref: str) -> str | None:
    return None if claims is None else for_ref(claims, ref)


def _key_ok(key: APIKey, user_id, now) -> bool:
    if not key.is_active or key.user_id != user_id:
        return False
    return key.expires_at is None or _aware(key.expires_at) > now


async def _grant_deadline_if_live(session, grant_id: str, user_id, now) -> datetime.datetime | None:
    """The grant family's absolute deadline while it has a live token, else None.

    Live: some token of the family neither revoked nor expired (the refresh
    token, 30 days), every row the same user's, and the deadline ahead.
    """
    from src.oauth.grants import grant_deadline

    row = (await session.execute(
        select(
            func.min(OAuthToken.grant_issued_at),
            func.count().filter(OAuthToken.revoked.is_(False) & (OAuthToken.expires_at > now)),
            func.count().filter(OAuthToken.user_id.is_distinct_from(user_id)),
        ).where(OAuthToken.grant_id == grant_id)
    )).one()
    issued, live, foreign = row
    if issued is None or not live or foreign:
        return None
    deadline = grant_deadline(issued)
    return deadline if deadline > now else None


async def still_valid(session, claims: Claims) -> bool:
    """The redemption check: the minting credential and its user, right now."""
    from src.services import transfer

    if transfer._ownerless_in_multi_user(claims.user_id):
        return False
    if transfer.quarantine_refusal(claims.user_id) is not None:
        return False
    now = _now()
    if claims.key_id is not None:
        key = (await session.execute(
            select(APIKey).where(APIKey.id == claims.key_id).execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if key is None or not _key_ok(key, claims.user_id, now):
            return False
    elif await _grant_deadline_if_live(session, claims.grant_id, claims.user_id, now) is None:
        return False
    if claims.user_id is not None:
        user = (await session.execute(
            select(User).where(User.id == claims.user_id).execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if user is None or not user.is_active:
            return False
    return True
