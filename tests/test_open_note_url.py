"""Open-note links (`OPEN_NOTE_URL`, fork, 2026-11-12).

Set: the tools that name a note for the agent to show the user carry an
http(s) link opening it in Obsidian, the template with `{path}` replaced by the
note's vault-relative path without `.md`, percent-encoded with `/` kept.
Unset (the default): no link, and every result is as before.

Offline: a tmp_path vault for the file tools, faked sessions and services for
the index-backed ones, `_log_usage` stubbed.
"""

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.config import Settings  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import open_note_url  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services import vault_fs  # noqa: E402

TEMPLATE = "https://notes.example.org/open?file={path}"
BASE = "https://notes.example.org/open?file="


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(open_note_url.settings, "open_note_url", TEMPLATE)


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)


# ── the setting ─────────────────────────────────────────────────────────────


def test_unset_and_blank_mean_off():
    assert Settings(_env_file=None).open_note_url is None
    assert Settings(open_note_url="  ", _env_file=None).open_note_url is None


@pytest.mark.parametrize("value", [
    TEMPLATE,
    "http://localhost:8080/open/{path}",
    "https://notes.example.org/open?vault=My%20Vault&file={path}#top",
])
def test_a_good_template_is_kept(value):
    assert Settings(open_note_url=value, _env_file=None).open_note_url == value


@pytest.mark.parametrize("value, words", [
    ("https://notes.example.org/open", "exactly once"),
    ("https://notes.example.org/{path}?file={path}", "exactly once"),
    ("obsidian://open?file={path}", "http(s)"),
    ("javascript:alert(1)//{path}", "http(s)"),
    ("notes.example.org/open?file={path}", "http(s)"),
    ("https:///open?file={path}", "http(s)"),
    ("https://{path}.example.org/", "not its host"),
    ("https://notes.example.org/open file={path}", "whitespace"),
])
def test_a_bad_template_fails_at_startup(value, words):
    with pytest.raises(ValidationError) as exc:
        Settings(open_note_url=value, _env_file=None)
    assert "OPEN_NOTE_URL" in str(exc.value) and words in str(exc.value)


# ── the encoding ────────────────────────────────────────────────────────────


def test_off_gives_no_link():
    assert open_note_url.settings.open_note_url is None
    assert open_note_url.open_note_url("a.md") is None
    assert open_note_url.text_suffix("a.md") == ""


@pytest.mark.parametrize("path, encoded", [
    ("Note.md", "Note"),
    ("Areas/Home/Home.md", "Areas/Home/Home"),
    ("My notes/Two words.md", "My%20notes/Two%20words"),
    ("Q&A #1?.md", "Q%26A%20%231%3F"),
    ("100% sure+x=y.md", "100%25%20sure%2Bx%3Dy"),
    ("Café/Ünïcode 日本.md", "Caf%C3%A9/%C3%9Cn%C3%AFcode%20%E6%97%A5%E6%9C%AC"),
    ("a/../b.md", "a/../b"),
    ("notes.md.md", "notes.md"),
])
def test_the_path_is_percent_encoded_with_slashes_kept(on, path, encoded):
    assert open_note_url.open_note_url(path) == BASE + encoded


@pytest.mark.parametrize("path", ["image.png", "Note.MD", "Note", ".md", "caf\udce9.md"])
def test_no_link_for_what_is_not_an_encodable_note(on, path):
    assert open_note_url.open_note_url(path) is None


def test_the_suffix_is_one_labelled_clause(on):
    assert open_note_url.text_suffix("a b.md") == f" — open_url: {BASE}a%20b"


# ── read_note and create_note (a tmp vault) ─────────────────────────────────


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    (tmp_path / "Folder").mkdir()
    (tmp_path / "Folder" / "A note #1.md").write_text("body\n", encoding="utf-8")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


def test_read_note_carries_the_link_when_on(vault, on):
    result = asyncio.run(tools.read_note_impl("Folder/A note #1.md"))
    assert result.error is None and result.open_url == BASE + "Folder/A%20note%20%231"


def test_read_note_has_no_link_when_off_or_on_error(vault, monkeypatch):
    result = asyncio.run(tools.read_note_impl("Folder/A note #1.md"))
    assert result.open_url is None and "open_url" not in result.model_dump(exclude_none=True)
    monkeypatch.setattr(open_note_url.settings, "open_note_url", TEMPLATE)
    missing = asyncio.run(tools.read_note_impl("Folder/missing.md"))
    assert missing.error and missing.open_url is None


def test_create_note_ends_its_first_line_with_the_link(vault, on):
    result = str(asyncio.run(tools.create_note_impl("Folder/New one.md", "body\n")))
    first = result.splitlines()[0]
    assert first.startswith("Created note: Folder/New one.md — content_hash: ")
    assert first.endswith(f" — open_url: {BASE}Folder/New%20one")


def test_create_note_is_unchanged_when_off(vault):
    result = str(asyncio.run(tools.create_note_impl("Folder/New one.md", "body\n")))
    assert "open_url" not in result


# ── the index-backed listings (faked) ───────────────────────────────────────


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return SimpleNamespace(all=lambda: self._rows, fetchall=lambda: self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None


_WHEN = datetime(2026, 11, 12, 9, 30, tzinfo=timezone.utc)
_ROW = SimpleNamespace(file_path="Log/2026-11-12 Call & co.md", title="Call & co", tags=["x"],
                       modified_at=_WHEN, file_size=12)
_HIT = {"path": "Log/2026-11-12 Call & co.md", "title": "Call & co", "tags": ["x"], "rank": 0.5,
        "similarity": 0.9, "stale": False, "embedding_truncated": False, "chunk": "preview"}
_LINK = f" — open_url: {BASE}Log/2026-11-12%20Call%20%26%20co"


def _listings(monkeypatch):
    monkeypatch.setattr(tools, "async_session", lambda: _Session([_ROW]))

    async def fts(*_a, **_k):
        return [dict(_HIT)]

    async def sem(*_a, **_k):
        return [dict(_HIT)]

    monkeypatch.setattr(tools, "full_text_search", fts)
    monkeypatch.setattr(tools, "semantic_search", sem)
    return {
        "keyword_search": lambda: tools.search_notes_impl("call"),
        "semantic_search": lambda: tools.semantic_search_impl("call"),
        "list_notes": lambda: tools.list_notes_impl("Log/"),
        "get_recent": lambda: tools.get_recent_impl(),
        "find_orphans": lambda: tools.find_orphans_impl(),
    }


@pytest.mark.parametrize("tool", ["keyword_search", "semantic_search", "list_notes", "get_recent", "find_orphans"])
def test_each_hit_line_ends_with_its_link_when_on(monkeypatch, on, tool):
    out = str(asyncio.run(_listings(monkeypatch)[tool]()))
    hit = next(line for line in out.splitlines() if "Log/2026-11-12 Call & co.md" in line)
    assert hit.endswith(_LINK), hit
    assert out.count("open_url:") == 1


@pytest.mark.parametrize("tool", ["keyword_search", "semantic_search", "list_notes", "get_recent", "find_orphans"])
def test_no_hit_line_carries_a_link_when_off(monkeypatch, tool):
    out = str(asyncio.run(_listings(monkeypatch)[tool]()))
    assert "Log/2026-11-12 Call & co.md" in out and "open_url" not in out


# ── the guide ───────────────────────────────────────────────────────────────


def test_the_guide_says_what_the_link_is_for():
    primer = tools._VAULT_GUIDE_PRIMER
    assert "OPEN_NOTE_URL" in primer and "open_url" in primer and "main_note_open_url" in primer
