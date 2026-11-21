"""The note-link page, `/open` (fork, 2026-11-21): what it accepts, where it sends, and its tab-closing script."""
from __future__ import annotations

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
    return m.group(1).replace("&amp;", "&")


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


async def get_raw(client, target: str) -> httpx.Response:
    """GET with the request target sent byte for byte: httpx would percent-encode `"`, `<`, a space and the like."""
    return await client.send(client.build_request("GET", httpx.URL("http://localhost:8000", raw_path=target.encode())))


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
    assert Settings(open_note_vault=value, _env_file=None).open_note_vault == expected


def test_a_vault_name_with_a_control_character_is_refused():
    with pytest.raises(ValueError, match="OPEN_NOTE_VAULT"):
        Settings(open_note_vault="My\nVault", _env_file=None)
