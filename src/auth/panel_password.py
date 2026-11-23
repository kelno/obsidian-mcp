"""A password for the panel in single-user mode (fork; `PANEL_PASSWORD`).

Single-user mode has no login of its own: `get_current_user` hands every
request the admin sentinel, and the deployment is expected to put a login gate
(a reverse proxy's password, forward auth) in front of `/admin`, `/api` and
`/authorize`. Anything that reaches the app's port without going through that
gate (another container on the host, a VPN peer) then opens the panel, mints
`readwrite` API keys and approves OAuth grants with no password at all.

With `PANEL_PASSWORD` set, those three surfaces need a session that this module
minted after the password was typed. Off (empty) by default: nothing changes.
Multi-user mode has accounts and refuses the setting at startup.

What it is, and why it is shaped this way (docs/architecture/control-panel.md,
"Single-user panel password"):

* **One password, from the environment, compared in constant time.** The
  setting lives beside `SECRET_KEY`, which already lets whoever reads it forge
  any session cookie, so storing a hash there would protect nothing that the
  file does not already give away. Both sides are SHA-256'd before
  `hmac.compare_digest`, so the comparison takes the same time whatever the
  lengths.
* **Sessions are server-side, in this process's memory.** The cookie carries a
  random identifier (`panel_sid`); the table holds its SHA-256 and expiry. A
  logout deletes the row, so a copied cookie stops working at once (the reason
  upstream's #198 registry exists). In memory rather than in the database
  because `--workers 1` makes this process the server, and because a restart
  signing everyone out is acceptable here: it is also what makes a password
  change (an `.env` edit plus a restart) end every session with no code. The
  table is capped, oldest dropped first, so it cannot grow without bound.
* **No username.** There is one identity, the sentinel; the login form shows
  the password field only.
* **The failed-login budgets are upstream's**: 5 attempts a minute per address
  on the POST (the decorator), and the per-account budget under a key no
  `users` row can have. With one account that budget is global: whoever can
  reach the login can spend it and keep the right password out for a window
  (an accepted limitation, see the architecture note), and the page then says
  so rather than "Invalid password".
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.config import settings
from src.csrf import generate_csrf_token, verify_csrf
from src.limiter import limiter
from src.services import panel_csp, rate_limits, security_events

logger = logging.getLogger(__name__)

#: The cookie key the identifier travels under. Distinct from multi-user's
#: `sid`, so a cookie from one mode never validates in the other.
SESSION_KEY = "panel_sid"

#: Live sessions kept at most; the oldest is dropped past it. One person signs
#: in from a handful of browsers; the cap only bounds a runaway.
MAX_SESSIONS = 64

#: The per-account login budget's key. `users.id` is a serial starting at 1,
#: so 0 is never a real account's counter.
LOGIN_BUDGET_KEY = 0

LOGIN_PATH = "/admin/auth/login"
LOGOUT_PATH = "/admin/auth/logout"


def enabled() -> bool:
    """Whether the panel password gates the panel. Single-user mode only."""
    return bool(settings.panel_password) and not settings.multi_user_mode


@dataclass
class _Session:
    expires_at: float  # time.time()


# sha256(identifier) -> _Session, oldest first.
_sessions: "OrderedDict[str, _Session]" = OrderedDict()


def reset_sessions() -> None:
    """Forget every session (tests)."""
    _sessions.clear()


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


def check_password(submitted: str) -> bool:
    """Constant-time comparison with `PANEL_PASSWORD`."""
    configured = settings.panel_password
    if not configured or not isinstance(submitted, str):
        return False
    return hmac.compare_digest(_digest(submitted), _digest(configured))


def _cookie(request: Request):
    try:
        return request.session
    except (AssertionError, AttributeError):
        return None


def _sweep(now: float) -> None:
    for key in [k for k, s in _sessions.items() if s.expires_at <= now]:
        del _sessions[key]


def start(request: Request) -> str | None:
    """Mint a session and write its identifier into a fresh cookie."""
    cookie = _cookie(request)
    if cookie is None:
        return None
    now = time.time()
    _sweep(now)
    sid = secrets.token_urlsafe(32)
    _sessions[_digest(sid).hex()] = _Session(expires_at=now + settings.session_max_age)
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)
    # A mint rewrites the cookie from scratch (session fixation). Everything
    # in it goes, the CSRF nonce included: another open tab must reload before
    # its next POST. Don't carry keys across the mint. A flash set before it
    # would be lost too; nothing here sets one.
    cookie.clear()
    cookie[SESSION_KEY] = sid
    return sid


def is_signed_in(request: Request) -> bool:
    """Whether this request carries a live panel session. Clears a dead one."""
    cookie = _cookie(request)
    if cookie is None:
        return False
    sid = cookie.get(SESSION_KEY)
    if not sid:
        return False
    if not isinstance(sid, str):
        cookie.pop(SESSION_KEY, None)
        return False
    now = time.time()
    entry = _sessions.get(_digest(sid).hex())
    if entry is not None and entry.expires_at > now:
        return True
    if entry is not None:
        del _sessions[_digest(sid).hex()]
    security_events.emit(
        "panel_session_replay_refused",
        subject=security_events.subject_for(request=request),
        reason="unknown_session" if entry is None else "expired_session",
        user_id=None,
        token_tag=security_events.redacted_token_tag(sid),
        route=getattr(getattr(request, "url", None), "path", None),
        client_ip=security_events.client_ip(request),
    )
    cookie.pop(SESSION_KEY, None)
    return False


def revoke(request: Request) -> int:
    """End this request's session, server-side. Returns rows removed."""
    cookie = _cookie(request)
    sid = cookie.get(SESSION_KEY) if cookie is not None else None
    if not isinstance(sid, str) or not sid:
        return 0
    return 1 if _sessions.pop(_digest(sid).hex(), None) is not None else 0


# ── Routes: mounted by src/main.py only while `enabled()` ────────────────────

router = APIRouter(tags=["auth"], dependencies=[Depends(verify_csrf)])

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "control_panel", "templates"),
    context_processors=[panel_csp.template_context],
)


def _safe_next(next_url: str | None) -> str:
    # The multi-user router's rule, imported lazily: src.auth.routes pulls in
    # OIDC and the bootstrap machinery, none of which this mode uses.
    from src.auth.routes import _safe_next as upstream_safe_next

    return upstream_safe_next(next_url)


def _render_login(
    request: Request, *, error: str | None = None, next_url: str = "/admin/", status_code: int = 200
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "next": next_url,
            "username": "",
            "csrf_token": generate_csrf_token(request),
            "oidc_enabled": False,
            "oidc_display_name": "",
            "local_login_enabled": True,
            "password_only": True,
        },
        status_code=status_code,
    )


@router.get(LOGIN_PATH, response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/admin/"):
    if is_signed_in(request):
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_302_FOUND)
    return _render_login(request, next_url=_safe_next(next))


@router.post(LOGIN_PATH)
@limiter.limit("5/minute")
async def login_submit(
    request: Request,
    password: str = Form(...),
    next: str = Form("/admin/"),
):
    target = _safe_next(next)
    invalid = "Invalid password"
    throttled = rate_limits.check_login_failures(LOGIN_BUDGET_KEY)
    if throttled is not None:
        security_events.emit(
            "panel_login_account_throttled",
            subject=security_events.subject_for(request=request),
            username_submitted="admin",
            limit_count=throttled.limit,
            window_seconds=throttled.window_seconds,
            client_ip=security_events.client_ip(request),
            route=request.url.path,
        )
        # Said plainly, unlike multi-user mode: there is one account, so the
        # message names no user, and an owner told "Invalid password" while a
        # guesser holds the budget would doubt the right password.
        minutes = max(1, -(-throttled.window_seconds // 60))
        return _render_login(
            request,
            error=f"Too many failed attempts. Try again in up to {minutes} minutes.",
            next_url=target,
            status_code=429,
        )
    if not check_password(password):
        security_events.emit(
            "panel_login_failed",
            subject=security_events.subject_for(request=request),
            reason="bad_password",
            username_submitted="admin",
            client_ip=security_events.client_ip(request),
            route=request.url.path,
        )
        rate_limits.record_login_failure(LOGIN_BUDGET_KEY)
        return _render_login(request, error=invalid, next_url=target, status_code=401)
    if start(request) is None:
        return _render_login(request, error=invalid, next_url=target, status_code=401)
    security_events.emit(
        "panel_login_succeeded",
        level=logging.INFO,
        subject=security_events.subject_for(request=request),
        username="admin",
        client_ip=security_events.client_ip(request),
        route=request.url.path,
    )
    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.post(LOGOUT_PATH)
async def logout(request: Request):
    revoked = revoke(request)
    security_events.emit(
        "panel_logout",
        level=logging.INFO,
        subject=security_events.subject_for(request=request),
        username_session="admin",
        client_ip=security_events.client_ip(request),
    )
    if revoked:
        security_events.emit(
            "panel_sessions_revoked",
            level=logging.INFO,
            subject=security_events.subject_for(request=request),
            reason="logout",
            count=revoked,
        )
    cookie = _cookie(request)
    if cookie is not None:
        cookie.clear()
    return RedirectResponse(LOGIN_PATH, status_code=status.HTTP_302_FOUND)
