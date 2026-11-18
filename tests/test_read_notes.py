"""`read_notes`: several whole notes in one call (ledger Q127).

A voice relay's fourteen parallel `read_note` calls failed in the claude.ai
app; this tool reads them in one call. What must hold: each note is exactly
what a whole-note `read_note` returns when it fits, the notes share one
response cap, a cut-off note says so in its own `notice` (the round-trip rule
lives there, not in the description), and one bad path never sinks the call.

Setup follows `test_read_response_cap.py`.
"""

import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.read_result import READ_NOTES_MAX_PATHS  # noqa: E402


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    return tmp_path


def _write(vault, name, text):
    (vault / name).write_text(text, encoding="utf-8")
    return name


@pytest.mark.asyncio
async def test_each_note_is_what_read_note_returns_in_the_order_given(vault):
    _write(vault, "a.md", "---\ntags: [x]\n---\nAlpha body\n")
    _write(vault, "b.md", "Beta body\n")
    out = await tools.read_notes_impl(["b.md", "a.md"])
    assert out.error is None
    assert [n.path for n in out.notes] == ["b.md", "a.md"]
    for note in out.notes:
        alone = await tools.read_note_impl(note.path)
        assert note.model_dump() == alone.model_dump()
    assert out._body_outcome is None


@pytest.mark.asyncio
async def test_a_missing_path_gets_its_own_error_and_the_others_still_come_back(vault):
    _write(vault, "a.md", "Alpha\n")
    out = await tools.read_notes_impl(["missing.md", "a.md"])
    missing, found = out.notes
    assert missing.path == "missing.md" and missing.content is None
    assert "Note not found: missing.md" in missing.error and '"code":"not_found"' in missing.error
    assert found.content == "Alpha\n" and found.error is None
    # Something was read: the call is not a failed one.
    assert out._body_outcome is None


@pytest.mark.asyncio
async def test_a_call_that_reads_nothing_is_logged_as_the_first_refusal(vault):
    out = await tools.read_notes_impl(["missing.md", "../outside.md"])
    assert all(n.error for n in out.notes)
    assert out._body_outcome is not None and out._body_outcome.marker == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, READ_NOTES_MAX_PATHS + 1])
async def test_an_empty_or_too_long_list_is_refused(vault, count):
    out = await tools.read_notes_impl(["a.md"] * count)
    assert out.notes is None
    assert f"give 1 to {READ_NOTES_MAX_PATHS} paths (got {count})" in out.error
    assert out._body_outcome.marker == "invalid_argument"


@pytest.mark.asyncio
async def test_the_notes_share_the_cap_and_a_cut_off_note_carries_the_warning(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 1_000)
    _write(vault, "short.md", "s" * 100)
    _write(vault, "long1.md", "x" * 5_000)
    _write(vault, "long2.md", "y" * 5_000)
    out = await tools.read_notes_impl(["long1.md", "short.md", "long2.md"])
    long1, short, long2 = out.notes
    # The short note's unused share goes to the others: 100 + 450 + 450.
    assert short.content == "s" * 100 and short.truncated is False and short.notice is None
    assert len(long1.content) == len(long2.content) == 450
    assert sum(len(n.content) for n in out.notes) <= 1_000
    assert long1.truncated is True and long1.next_offset == 450 and long1.outline is None
    assert 'Continue with `read_note(path="long1.md", offset=450)`.' in long1.notice
    assert "Don't write this note's `content` back with `edit_note` before reading the rest" in long1.notice


@pytest.mark.asyncio
async def test_twenty_notes_stay_within_what_one_read_note_may_return(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "max_read_response_chars", 1_000)
    tags = "\n".join(f"  - tag{i:04d}" for i in range(400))
    paths = [_write(vault, f"n{i:02d}.md", f"---\ntags:\n{tags}\n---\n" + "z" * 2_000) for i in range(READ_NOTES_MAX_PATHS)]
    out = await tools.read_notes_impl(paths)
    assert sum(len(n.content) for n in out.notes) <= 1_000
    # Each note's metadata got an equal share and dropped what did not fit.
    assert all(n.tags is None and n.metadata_omissions for n in out.notes)


def test_share_budget_gives_equal_shares_and_passes_on_what_is_unused():
    assert tools.share_budget([10, 1_000, 1_000], 1_000) == [10, 495, 495]
    assert tools.share_budget([0, 0], 1_000) == [0, 0]
    assert tools.share_budget([300, 300, 300], 1_000) == [300, 300, 300]
    assert sum(tools.share_budget([7, 999, 50, 4_000, 3], 1_000)) <= 1_000
