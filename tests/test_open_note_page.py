"""The note-link page, `/open` (fork, 2026-11-21): what it accepts, where it sends, and its tab-closing script."""
from __future__ import annotations

import html
import re

import httpx
import pytest
import pytest_asyncio

from src.config import Settings, settings


@pytest_asyncio.fixture
async def client():
    from src.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 4242)),
        base_url="http://localhost:8000",
    ) as c:
        yield c


@pytest.fixture
def vault(monkeypatch):
    monkeypatch.setattr(settings, "open_note_vault", "My Vault")


def target(page: str) -> str:
    """Where the page sends the browser: its meta refresh, unescaped."""
    m = re.search(r'content="0;url=([^"]*)"', page)
    assert m, page
    return html.unescape(m.group(1))


async def test_without_a_vault_name_there_is_no_page(client, monkeypatch):
    monkeypatch.setattr(settings, "open_note_vault", None)
    assert (await client.get("/open?file=Notes/A")).status_code == 404


@pytest.mark.parametrize("file", [
    "Areas/Caf%C3%A9%20%26%20co%20%231",
    "Reference/People%20%28private%29/Someone",
    # Some clients turn %28/%29 back into ( and ) before opening a link (F148).
    "Reference/People%20(private)/Someone",
    "Areas/Note%23What%20it%20would%20change%20(draft)",
    "Log/2026-11-21%20Done!%20It's%20fine,%20a+b=c%3B%20x:y@z*",
])
async def test_a_path_goes_to_obsidian_as_received(client, vault, file):
    r = await get_raw(client, f"/open?file={file}")
    assert r.status_code == 200
    assert target(r.text) == f"obsidian://open?vault=My%20Vault&file={file}"
    assert r.text.count("obsidian://open?vault=My%20Vault&amp;file=") == 2  # the refresh and the fallback link


class Raw:
    """A response to a request made straight on the app, its query string sent byte for byte (httpx would
    percent-encode `"`, `<`, `'`, a space and the like, as browsers mostly do; a proxy passes them on raw)."""

    def __init__(self, status_code: int, text: str):
        self.status_code, self.text = status_code, text


async def get_raw(client, target: str) -> Raw:
    from src.main import app

    path, _, query = target.partition("?")
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": "", "query_string": query.encode("latin-1"),
        "headers": [(b"host", b"localhost:8000")], "client": ("203.0.113.9", 4242), "server": ("localhost", 8000),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Raw(start["status"], body.decode())


@pytest.mark.parametrize("query", [
    "", "file=", "other=Notes/A", 'file=Notes/A"x', "file=Notes/A<x", "file=Notes/A>x", "file=Notes/A x",
    "file=Notes/A`x", "file=Notes/A{x}", "file=Notes/A$x", "file=Notes/A\\x",
])
async def test_anything_else_gets_the_usage_line(client, vault, query):
    r = await get_raw(client, f"/open?{query}")
    assert r.status_code == 400
    assert r.text.startswith("Usage: /open?file=")


async def test_an_ampersand_ends_the_path_and_never_reaches_the_page(client, vault):
    r = await client.get("/open?file=Notes/A&vault=Other")
    assert target(r.text) == "obsidian://open?vault=My%20Vault&file=Notes/A"


async def test_the_page_closes_its_tab_with_its_one_allowed_script(client, vault):
    r = await client.get("/open?file=Notes/A")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", r.text, re.DOTALL)
    assert scripts == [(f' nonce="{nonce}"', scripts[0][1])]
    assert "window.close()" in scripts[0][1] and "3000" in scripts[0][1]
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"


async def test_head_answers_like_get(client, vault):
    assert (await client.head("/open?file=Notes/A")).status_code == 200


@pytest.mark.parametrize("value,expected", [(None, None), ("", None), ("  ", None), (" Obsidian ", "Obsidian")])
def test_a_blank_vault_name_means_unset(value, expected):
    assert make_settings(open_note_vault=value).open_note_vault == expected


def test_a_vault_name_with_a_control_character_is_refused():
    with pytest.raises(ValueError, match="OPEN_NOTE_VAULT"):
        make_settings(open_note_vault="My\nVault")


def make_settings(**kw) -> Settings:
    return Settings(secret_key="test", _env_file=None, **kw)
