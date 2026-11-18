"""Ledger entry links against a real PostgreSQL (fork, 2026-11-18).

`ledger_get`, `ledger_add` and `ledger_update` give each entry a `view_url`;
`/transfer/ledger/info` redeems it for the page's data while the credential
that minted it holds: an API key while it is active, unexpired and the same
owner's; an OAuth grant while some token of it is live and its absolute
deadline is ahead, so a link outlives the hour-long access token. The token
itself (signature, shape, expiry) is in `tests/test_ledger_view.py`.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import contextlib
import datetime
import logging
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.auth.session import current_user_id
from src.config import settings
from src.mcp_server import tools
from src.mcp_server.auth import current_api_key_id, current_oauth_token_id, current_permission
from src.models.db import APIKey, OAuthClient, OAuthToken, User
from src.oauth.grants import new_grant_id
from src.services import ledger, ledger_embed, ledger_view, security_events
from src.transfer import routes as transfer_routes
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)
BASE = "https://mcp.example.org"


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("ledgerview", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def ctx(sessionmaker, monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", False)
    monkeypatch.setattr(settings, "_public_origin_explicit", True)
    monkeypatch.setattr(settings, "base_url", BASE)
    for module in (src.database, tools, ledger_embed, transfer_routes):
        monkeypatch.setattr(module, "async_session", sessionmaker)
    monkeypatch.setattr(ledger_embed, "enqueue", lambda entry_id: True)

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL ledger.allow_delete = 'on'"))
        for table in ("ledger_case_rules", "ledger_revisions", "ledger_entry_topics", "ledger_entry_links",
                      "ledger_entries", "ledger_ref_counters", "oauth_tokens", "oauth_clients", "api_keys", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        key = APIKey(name="k", key_hash=uuid.uuid4().hex * 2, key_prefix="omcp_test", permission="readwrite",
                     is_active=True)
        client = OAuthClient(client_id=uuid.uuid4().hex, client_secret_hash="x" * 64, client_name="c",
                             redirect_uris=["https://example.com/cb"], scope="readwrite")
        session.add_all([key, client])
        await session.flush()
        grant = new_grant_id()
        access = OAuthToken(token_hash=uuid.uuid4().hex * 2, token_type="access", client_id=client.client_id,
                            scope="readwrite", grant_id=grant, expires_at=_now() + datetime.timedelta(hours=1),
                            revoked=False)
        refresh = OAuthToken(token_hash=uuid.uuid4().hex * 2, token_type="refresh", client_id=client.client_id,
                             scope="readwrite", grant_id=grant, expires_at=_now() + datetime.timedelta(days=30),
                             revoked=False)
        session.add_all([access, refresh])
        await session.commit()
    tokens = [current_permission.set("readwrite"), current_user_id.set(None), current_api_key_id.set(key.id),
              current_oauth_token_id.set(None)]
    yield {"key": key, "access": access, "refresh": refresh, "grant": grant}
    for var, token in zip((current_permission, current_user_id, current_api_key_id, current_oauth_token_id),
                          tokens):
        var.reset(token)


@pytest_asyncio.fixture
async def client():
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.11", 4242)),
        base_url="http://localhost:8000",
    ) as c:
        yield c


@contextlib.contextmanager
def calling_as(*, key_id=None, oauth_token_id=None):
    """Calls made inside come from this credential (the others' context is restored after)."""
    a, b = current_api_key_id.set(key_id), current_oauth_token_id.set(oauth_token_id)
    try:
        yield
    finally:
        current_oauth_token_id.reset(b)
        current_api_key_id.reset(a)


async def info(client, url):
    assert url and url.startswith(f"{BASE}/transfer/ledger#"), url
    return await client.get("/transfer/ledger/info", headers={"Authorization": "Bearer " + url.rsplit("/", 1)[1]})


async def refused_body(client):
    return (await client.get("/transfer/ledger/info", headers={"Authorization": "Bearer x.y"})).content


async def test_the_three_tools_give_a_link_that_opens_the_entry_with_its_history_and_links(ctx, client):
    first = await tools.ledger_add_impl("quirk", "A first quirk", body="Seen **twice**.",
                                        details={"certainty": "tested"})
    assert first.error is None, first.error
    second = await tools.ledger_add_impl("quirk", "A second quirk", details={"certainty": "tested"},
                                         related=[first.ref])
    assert second.view_url
    updated = await tools.ledger_update_impl(ref=first.ref, expected_revision=first.revision, reason="more",
                                             append="Seen again.")
    assert updated.error is None and updated.view_url
    got = await tools.ledger_get_impl([first.ref])
    url = got.entries[0].view_url

    r = await info(client, url)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    data = r.json()
    entry = data["entry"]
    assert entry["ref"] == first.ref and "Seen again." in entry["body"]
    assert entry.get("history"), "the page shows the history"
    assert "view_url" not in entry
    assert set(data["links"]) == {second.ref}
    assert (await info(client, data["links"][second.ref])).json()["entry"]["ref"] == second.ref
    assert data["expires_at"] <= (_now() + datetime.timedelta(days=30, minutes=1)).isoformat()


async def test_a_deactivated_or_expired_key_ends_its_links(ctx, sessionmaker, client):
    added = await tools.ledger_add_impl("quirk", "Keyed", details={"certainty": "tested"})
    assert (await info(client, added.view_url)).status_code == 200
    async with sessionmaker() as session:
        await session.execute(update(APIKey).where(APIKey.id == ctx["key"].id).values(is_active=False))
        await session.commit()
    r = await info(client, added.view_url)
    assert r.status_code == 404 and r.content == await refused_body(client)
    async with sessionmaker() as session:
        await session.execute(update(APIKey).where(APIKey.id == ctx["key"].id).values(
            is_active=True, expires_at=_now() - datetime.timedelta(seconds=1)))
        await session.commit()
    assert (await info(client, added.view_url)).status_code == 404
    # A key past its end mints no link either.
    assert (await tools.ledger_get_impl([added.ref])).entries[0].view_url is None


async def test_an_oauth_link_lives_with_its_grant_not_its_access_token(ctx, sessionmaker, client):
    with calling_as(oauth_token_id=ctx["access"].id):
        added = await tools.ledger_add_impl("quirk", "Granted", details={"certainty": "tested"})
    assert added.view_url
    async with sessionmaker() as session:  # the access token runs out; the refresh token lives on
        await session.execute(update(OAuthToken).where(OAuthToken.id == ctx["access"].id).values(
            expires_at=_now() - datetime.timedelta(seconds=1)))
        await session.commit()
    assert (await info(client, added.view_url)).status_code == 200
    async with sessionmaker() as session:  # revoking the grant revokes every token of it
        await session.execute(update(OAuthToken).where(OAuthToken.grant_id == ctx["grant"]).values(revoked=True))
        await session.commit()
    assert (await info(client, added.view_url)).status_code == 404


async def test_a_grant_past_its_absolute_deadline_ends_its_links(ctx, sessionmaker, client):
    with calling_as(oauth_token_id=ctx["access"].id):
        added = await tools.ledger_add_impl("quirk", "Deadline", details={"certainty": "tested"})
    assert (await info(client, added.view_url)).status_code == 200
    async with sessionmaker() as session:
        await session.execute(update(OAuthToken).where(OAuthToken.grant_id == ctx["grant"]).values(
            grant_issued_at=_now() - datetime.timedelta(days=settings.oauth_grant_absolute_lifetime_days + 1)))
        await session.commit()
    assert (await info(client, added.view_url)).status_code == 404


async def test_a_link_to_an_entry_that_is_not_there_is_the_uniform_404(ctx, client):
    added = await tools.ledger_add_impl("quirk", "Real", details={"certainty": "tested"})
    claims = ledger_view.decode(added.view_url.rsplit("/", 1)[1])
    ghost = ledger_view.for_ref(claims, "Q999999")
    r = await info(client, ghost)
    assert r.status_code == 404 and r.content == await refused_body(client)


async def test_no_credential_no_link(ctx):
    with calling_as():
        added = await tools.ledger_add_impl("quirk", "Unkeyed", details={"certainty": "tested"})
    assert added.error is None and added.view_url is None


# ── Two owners, multi-user mode (review round 1, 2026-11-18) ────────────────


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def named(self, event):
        standard = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}
        return [{k: v for k, v in r.__dict__.items() if k not in standard}
                for r in self.records if r.getMessage() == event]


@pytest.fixture
def captured():
    handler = _Capture()
    logger = security_events.logger
    logger.addHandler(handler)
    propagate, level = logger.propagate, logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    security_events.reset_state()
    try:
        with security_events.suppression_disabled():
            yield handler
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate
        logger.setLevel(level)
        security_events.reset_state()


@pytest_asyncio.fixture(loop_scope="module")
async def owners(ctx, sessionmaker, monkeypatch):
    """Users A and B, each with a vault, a key and ledger entries: A has Q1; B has Q1 and Q2."""
    monkeypatch.setattr(settings, "multi_user_mode", True)
    async with sessionmaker() as session:
        a = User(username=f"a{uuid.uuid4().hex[:8]}", password_hash="x", is_active=True, vault_path="/vaults/a")
        b = User(username=f"b{uuid.uuid4().hex[:8]}", password_hash="x", is_active=True, vault_path="/vaults/b")
        session.add_all([a, b])
        await session.flush()
        keys = {}
        for user in (a, b):
            keys[user.id] = APIKey(user_id=user.id, name="k", key_hash=uuid.uuid4().hex * 2, key_prefix="omcp_test",
                                   permission="read", is_active=True)
        session.add_all(keys.values())
        await session.commit()
    for uid, titles in ((a.id, ["A's own"]), (b.id, ["B's first", "B's second"])):
        for title in titles:
            async with sessionmaker() as session:
                await ledger.add_entry(session, uid, kind="quirk", title=title, details={"certainty": "tested"})
    async with sessionmaker() as session:
        claims_a = await ledger_view.principal_claims(session, key_id=keys[a.id].id, oauth_token_id=None,
                                                      user_id=a.id)
    assert claims_a is not None
    return {"a": a, "b": b, "key_a": keys[a.id], "claims_a": claims_a}


async def test_a_link_reads_only_its_owners_ledger(owners, client, captured):
    own = await info(client, ledger_view.link(owners["claims_a"], "Q1"))
    assert own.status_code == 200 and own.json()["entry"]["title"] == "A's own"
    assert captured.named("ledger_link_opened")[-1]["user_id"] == owners["a"].id
    # B has a Q2; A has none. A's link, re-signed for Q2, reads A's ledger only.
    other = await info(client, ledger_view.link(owners["claims_a"], "Q2"))
    assert other.status_code == 404 and other.content == await refused_body(client)
    assert captured.named("transfer_refused")[-2]["reason"] == "ledger_entry_missing"


@pytest.mark.parametrize("change", ["no_vault", "deactivated", "key_reassigned"])
async def test_the_owners_state_now_decides(owners, sessionmaker, client, captured, change):
    url = ledger_view.link(owners["claims_a"], "Q1")
    assert (await info(client, url)).status_code == 200
    async with sessionmaker() as session:
        if change == "no_vault":
            await session.execute(update(User).where(User.id == owners["a"].id).values(vault_path=None))
        elif change == "deactivated":
            await session.execute(update(User).where(User.id == owners["a"].id).values(is_active=False))
        else:
            await session.execute(update(APIKey).where(APIKey.id == owners["key_a"].id).values(
                user_id=owners["b"].id))
        await session.commit()
    r = await info(client, url)
    assert r.status_code == 404 and r.content == await refused_body(client)
    refused = captured.named("transfer_refused")
    assert refused[-2]["reason"] == "ledger_link_credential"
    assert refused[-2]["route"] == "/transfer/ledger/info" and refused[-2]["token_tag"].startswith("sha:")
