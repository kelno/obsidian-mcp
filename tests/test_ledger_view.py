"""Ledger entry links (`src/services/ledger_view.py`, fork 2026-11-18): the token, offline.

What the database adds (the credential checks, the tools' `view_url`, the
page's data) is in `tests/integration/test_ledger_view_pg.py`.
"""
from __future__ import annotations

import datetime

import httpx
import pytest
import pytest_asyncio

from src.config import settings
from src.services import ledger_view

NOW = datetime.datetime(2026, 11, 18, 12, 0, tzinfo=datetime.timezone.utc)


def claims(**kw):
    base = dict(user_id=None, ref="C146", key_id=7, grant_id=None,
                expires=int((NOW + datetime.timedelta(days=30)).timestamp()))
    base.update(kw)
    return ledger_view.Claims(**base)


def test_a_token_round_trips():
    for c in (claims(), claims(user_id=3, key_id=None, grant_id="g" * 32)):
        assert ledger_view.decode(ledger_view.encode(c), now=NOW) == c


def test_a_changed_token_is_refused():
    token = ledger_view.encode(claims())
    payload, mac = token.split(".")
    other = ledger_view.encode(claims(ref="C1")).split(".")[0]
    assert ledger_view.decode(f"{other}.{mac}", now=NOW) is None          # another payload, this signature
    assert ledger_view.decode(f"{payload}.{mac[:-2]}AA", now=NOW) is None  # a changed signature
    assert ledger_view.decode(token + ".x", now=NOW) is None
    assert ledger_view.decode(token.replace(".", ""), now=NOW) is None
    assert ledger_view.decode("a" * (ledger_view.MAX_TOKEN_CHARS + 1), now=NOW) is None
    assert ledger_view.decode(f"{payload}=.{mac}", now=NOW) is None       # padding is not in the alphabet
    assert ledger_view.decode("", now=NOW) is None


def test_another_secret_key_refuses_every_link(monkeypatch):
    token = ledger_view.encode(claims())
    monkeypatch.setattr(settings, "secret_key", settings.secret_key + "-rotated")
    assert ledger_view.decode(token, now=NOW) is None


def test_an_expired_token_is_refused():
    token = ledger_view.encode(claims())
    assert ledger_view.decode(token, now=NOW + datetime.timedelta(days=30)) is None
    assert ledger_view.decode(token, now=NOW + datetime.timedelta(days=29, hours=23)) is not None


@pytest.mark.parametrize("bad", [
    dict(key_id=7, grant_id="g"),          # two credentials
    dict(key_id=None, grant_id=None),      # none
    dict(ref=""),
    dict(ref="X" * 17),
    dict(key_id=True),                     # a bool is not an id
])
def test_a_signed_token_of_the_wrong_shape_is_refused(bad):
    # Signed by this server, so only the shape check stands between it and use.
    assert ledger_view.decode(ledger_view.encode(claims(**bad)), now=NOW) is None


def test_a_link_to_another_entry_keeps_the_credential_and_the_end(monkeypatch):
    monkeypatch.setattr(settings, "_public_origin_explicit", True)
    monkeypatch.setattr(settings, "base_url", "https://mcp.example.org")
    url = ledger_view.for_ref(claims(), "P149")
    assert url.startswith("https://mcp.example.org/transfer/ledger#P149/until-2026-12-18/")
    assert ledger_view.decode(url.rsplit("/", 1)[1], now=NOW) == claims(ref="P149")


def test_the_link_shows_its_end_date_the_last_day_it_opens(monkeypatch):
    # Readable in the link itself, so an agent sees it is temporary (owner,
    # 2026-11-19). A credential ending sooner shows its own, earlier date.
    monkeypatch.setattr(settings, "_public_origin_explicit", True)
    monkeypatch.setattr(settings, "base_url", "https://mcp.example.org")
    early = int(datetime.datetime(2026, 11, 25, 23, 0, tzinfo=datetime.timezone.utc).timestamp())
    assert "#C146/until-2026-11-25/" in ledger_view.url_for(claims(expires=early))


def test_no_public_origin_no_link(monkeypatch):
    monkeypatch.setattr(settings, "_public_origin_explicit", False)
    assert ledger_view.url_for(claims()) is None
    assert ledger_view.link(None, "C1") is None


@pytest_asyncio.fixture
async def client():
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 4242)),
        base_url="http://localhost:8000",
    ) as c:
        yield c


async def test_the_page_is_static_and_nonce_guarded(client):
    r = await client.get("/transfer/ledger")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'nonce-" in csp
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "/transfer/ledger/info" in r.text and "innerHTML" not in r.text


async def test_info_refuses_without_or_with_a_bad_token_with_the_uniform_404(client):
    missing = await client.get("/transfer/ledger/info")
    bad = await client.get("/transfer/ledger/info", headers={"Authorization": "Bearer nope.nope"})
    expired = await client.get("/transfer/ledger/info", headers={
        "Authorization": "Bearer " + ledger_view.encode(claims(expires=int(NOW.timestamp())))})
    from src.transfer import routes as transfer_routes

    uniform = transfer_routes._not_found().body  # the transfer routes' one refusal
    for r in (missing, bad, expired):
        assert r.status_code == 404
        assert r.content == uniform
