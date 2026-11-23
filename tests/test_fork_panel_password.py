"""The single-user panel password (fork; `PANEL_PASSWORD`, src/auth/panel_password.py).

Through `src.main.app`, the real middleware stack, with the database replaced
by a placeholder: every refusal asserted here happens before a handler body
(and so before any query) runs. The login router is mounted by `src/main.py`
only when the setting is on at import, and the suite imports the app with it
off, so the fixture includes it for the duration of a test.
"""
from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import main
from src.auth import panel_password
from src.auth.session import _SINGLE_USER_SENTINEL, get_current_user
from src.config import Settings, settings
from src.database import get_session
from src.limiter import limiter
from src.services import rate_limits

PASSWORD = "correct-horse-battery-staple-42"
PKCE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
AUTHORIZE = {
    "response_type": "code",
    "client_id": "some-client",
    "redirect_uri": "https://client.example/cb",
    "code_challenge": PKCE,
    "code_challenge_method": "S256",
}


class _NoDatabase:
    """Any use is a test failure: the gate must refuse before a query."""

    def __getattr__(self, name):
        raise AssertionError(f"the database was used ({name}) before the gate refused")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", False)
    monkeypatch.setattr(settings, "panel_password", PASSWORD)
    panel_password.reset_sessions()
    rate_limits._login_failures.clear()

    async def _get_session():
        yield _NoDatabase()

    overrides = dict(main.app.dependency_overrides)
    main.app.dependency_overrides[get_session] = _get_session
    before = list(main.app.router.routes)
    main.app.include_router(panel_password.router)
    limiter.reset()
    try:
        yield TestClient(main.app, base_url="https://localhost")
    finally:
        main.app.router.routes[:] = before
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(overrides)
        limiter.reset()
        panel_password.reset_sessions()
        rate_limits._login_failures.clear()


def _csrf(body: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', body)
    assert match, "the page rendered no CSRF token"
    return match.group(1)


def _login(client, password=PASSWORD, next_url="/admin/"):
    page = client.get("/admin/auth/login")
    assert page.status_code == 200
    return client.post(
        "/admin/auth/login",
        data={"password": password, "next": next_url, "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )


def _is_login_redirect(response) -> bool:
    return response.status_code == 302 and response.headers["location"].startswith(
        "/admin/auth/login"
    )


# ── settings ────────────────────────────────────────────────────────────────


def _settings(**values):
    base = {"secret_key": "x" * 32, "_env_file": None}
    base.update(values)
    return Settings(**base)


def test_off_by_default():
    assert _settings().panel_password == ""


def test_a_good_password_is_accepted():
    assert _settings(panel_password=PASSWORD).panel_password == PASSWORD


@pytest.mark.parametrize(
    "value",
    ["short", " " * 20, "changeme", "a-good-length-password\x00x"],
)
def test_weak_or_malformed_passwords_refuse_startup(value):
    with pytest.raises(ValidationError, match="PANEL_PASSWORD"):
        _settings(panel_password=value)


def test_multi_user_mode_refuses_the_setting():
    with pytest.raises(ValidationError, match="single-user mode"):
        _settings(panel_password=PASSWORD, multi_user_mode=True)


def test_the_password_is_not_in_the_settings_repr():
    assert PASSWORD not in repr(_settings(panel_password=PASSWORD))


# ── the gate: direct requests without the login are refused ─────────────────


def test_panel_page_redirects_to_login(client):
    response = client.get("/admin/", follow_redirects=False)
    assert _is_login_redirect(response)
    assert "next=%2Fadmin%2F" in response.headers["location"]


def test_key_creation_is_refused(client):
    # The finding's own request: minting a vault key straight on the port.
    response = client.post(
        "/admin/keys/create",
        data={"name": "stolen", "permission": "readwrite"},
        follow_redirects=False,
    )
    assert _is_login_redirect(response)


def test_api_is_refused(client):
    response = client.get(
        "/api/stats", headers={"Accept": "application/json"}, follow_redirects=False
    )
    assert response.status_code == 401


def test_consent_page_redirects_to_login(client):
    response = client.get("/authorize", params=AUTHORIZE, follow_redirects=False)
    assert _is_login_redirect(response)
    assert "next=%2Fauthorize%3F" in response.headers["location"]


def test_consent_approval_is_refused(client):
    response = client.post(
        "/authorize",
        data={**AUTHORIZE, "action": "approve", "scope": "readwrite", "state": "s"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.json()["error"] == "access_denied"


def test_a_forged_cookie_value_is_refused(client):
    client.cookies.set(settings.session_cookie_name, "not-a-signed-cookie")
    assert _is_login_redirect(client.get("/admin/", follow_redirects=False))


# ── the login ───────────────────────────────────────────────────────────────


def test_login_page_asks_for_the_password_only(client):
    page = client.get("/admin/auth/login")
    assert page.status_code == 200
    assert 'name="password"' in page.text
    assert 'name="username"' not in page.text
    assert "content-security-policy" in {k.lower() for k in page.headers}


def test_wrong_password_is_refused(client):
    response = _login(client, password="wrong-password-of-good-length")
    assert response.status_code == 401
    assert "Invalid password" in response.text
    assert not panel_password._sessions


def test_right_password_signs_in_and_returns_to_next(client):
    response = _login(client, next_url="/admin/keys")
    assert response.status_code == 302
    assert response.headers["location"] == "/admin/keys"
    # Signed in: the login page now sends the browser on.
    again = client.get("/admin/auth/login", follow_redirects=False)
    assert again.status_code == 302


def test_an_offsite_next_is_not_followed(client):
    response = _login(client, next_url="//evil.example/")
    assert response.headers["location"] == "/admin/"


def test_signed_in_passes_the_consent_gate(client, monkeypatch):
    from src.oauth import routes as oauth_routes

    assert _login(client).status_code == 302
    reached = []

    def _probe(*args, **kwargs):
        # The first check after the gate; everything later needs the database.
        reached.append(True)
        return False

    monkeypatch.setattr(oauth_routes, "_valid_pkce_challenge", _probe)
    response = client.get("/authorize", params=AUTHORIZE, follow_redirects=False)
    assert reached
    assert response.status_code == 400  # the probe's refusal, past the gate


def _csrf_for_current_cookie(client) -> str:
    """A CSRF token for the signed-in cookie, as a panel page would render it.

    Panel pages need the database, so a test-only route stands in for one; the
    fixture's route reset removes it again.
    """
    from fastapi.responses import PlainTextResponse

    from src.csrf import generate_csrf_token

    if not any(getattr(r, "path", None) == "/__test/csrf" for r in main.app.router.routes):
        async def _token(request: Request):
            return PlainTextResponse(generate_csrf_token(request))

        main.app.add_api_route("/__test/csrf", _token, methods=["GET"])
        # Ahead of the MCP mount at "/", which would otherwise answer it.
        main.app.router.routes.insert(0, main.app.router.routes.pop())

    response = client.get("/__test/csrf")
    assert response.status_code == 200, (response.status_code, response.text[:200])
    assert response.text and "<" not in response.text, response.text[:200]
    return response.text


def test_logout_revokes_the_session_for_every_copy_of_the_cookie(client):
    assert _login(client).status_code == 302
    token = _csrf_for_current_cookie(client)
    copied = client.cookies.get(settings.session_cookie_name)
    assert copied and len(panel_password._sessions) == 1

    logout = client.post(
        "/admin/auth/logout", data={"csrf_token": token}, follow_redirects=False
    )
    assert logout.status_code == 302
    assert logout.headers["location"] == "/admin/auth/login"
    assert not panel_password._sessions

    # Someone who copied the cookie before the logout is refused too.
    client.cookies.set(settings.session_cookie_name, copied)
    assert _is_login_redirect(client.get("/admin/", follow_redirects=False))


def test_logout_without_a_csrf_token_is_refused(client):
    assert _login(client).status_code == 302
    response = client.post("/admin/auth/logout", follow_redirects=False)
    assert response.status_code == 403
    assert len(panel_password._sessions) == 1


def test_expired_sessions_are_refused(client, monkeypatch):
    assert _login(client).status_code == 302
    # Age the server-side row only: patching the clock would also expire the
    # test client's cookie, and then nothing would reach the check.
    for entry in panel_password._sessions.values():
        entry.expires_at = time.time() - 1
    assert _is_login_redirect(client.get("/admin/", follow_redirects=False))
    assert not panel_password._sessions


def test_the_login_budget_throttles_guessing(client, monkeypatch):
    monkeypatch.setattr(settings, "panel_login_failure_limit", 3)
    monkeypatch.setattr(limiter, "enabled", False)
    for _ in range(3):
        assert _login(client, password="wrong-password-of-good-length").status_code == 401
    # Over budget: even the right password is refused until the window passes,
    # and the page says why rather than "Invalid password".
    throttled = _login(client)
    assert throttled.status_code == 429
    assert "Too many failed attempts" in throttled.text
    assert "Invalid password" not in throttled.text
    assert not panel_password._sessions


def test_the_session_table_is_capped(client, monkeypatch):
    monkeypatch.setattr(panel_password, "MAX_SESSIONS", 3)
    monkeypatch.setattr(limiter, "enabled", False)
    for _ in range(5):
        client.cookies.clear()
        assert _login(client).status_code == 302
    assert len(panel_password._sessions) == 3


# ── the dependency, directly ────────────────────────────────────────────────


def _request(cookie: dict):
    return SimpleNamespace(
        session=cookie,
        url=SimpleNamespace(path="/admin/"),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
        scope={},
    )


@pytest.mark.asyncio
async def test_get_current_user_off_is_the_sentinel(monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", False)
    monkeypatch.setattr(settings, "panel_password", "")
    assert await get_current_user(_request({}), None) is _SINGLE_USER_SENTINEL


@pytest.mark.asyncio
async def test_get_current_user_on_needs_a_session(monkeypatch):
    monkeypatch.setattr(settings, "multi_user_mode", False)
    monkeypatch.setattr(settings, "panel_password", PASSWORD)
    panel_password.reset_sessions()
    assert await get_current_user(_request({}), None) is None
    cookie: dict = {}
    assert panel_password.start(_request(cookie)) is not None
    assert await get_current_user(_request(cookie), None) is _SINGLE_USER_SENTINEL
    assert panel_password.revoke(_request(cookie)) == 1
    assert await get_current_user(_request(cookie), None) is None
    panel_password.reset_sessions()


def test_check_password_is_exact():
    original = settings.panel_password
    try:
        settings.panel_password = PASSWORD
        assert panel_password.check_password(PASSWORD)
        assert not panel_password.check_password(PASSWORD + " ")
        assert not panel_password.check_password(PASSWORD.upper())
        assert not panel_password.check_password("")
        settings.panel_password = ""
        assert not panel_password.check_password("")
    finally:
        settings.panel_password = original
