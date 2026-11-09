"""The agent-edit stamp and `move_note`'s archive hint (fork, 2026-11-10).

`LAST_AI_EDIT_FIELD` set: every tool that writes a note's text sets that
property to the write's date, time and UTC offset, as a line edit of the
block (comments, quoting and key order kept), never over a malformed block.
`ARCHIVE_FOLDER` set: `move_note` says when the folder a note left has a
counterpart under the archive, and changes nothing there.

Offline: a tmp_path vault, `_log_usage` stubbed, no DB.
"""

import asyncio
import os
import re
import tempfile
from datetime import datetime, timezone

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.config import Settings  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import ai_edit_stamp  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services import vault_fs  # noqa: E402

NOW = datetime(2026, 11, 10, 0, 45, 12, 345678, tzinfo=timezone.utc)
STAMP = "2026-11-10T01:45:12+01:00"  # NOW in Europe/Brussels, to the second
STAMP_RE = re.compile(r"last_ai_edit: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\n")


@pytest.fixture
def on(monkeypatch):
    s = ai_edit_stamp.settings
    monkeypatch.setattr(s, "last_ai_edit_field", "last_ai_edit")
    monkeypatch.setattr(s, "last_ai_edit_exclude", ["_/*", "CLAUDE.md"])
    monkeypatch.setattr(s, "tasks_timezone", "Europe/Brussels")


def stamp(text, path="n.md"):
    return ai_edit_stamp.stamp(text, path, now=NOW)


# ── the value ───────────────────────────────────────────────────────────────


def test_the_value_is_the_owners_local_time_to_the_second(on):
    assert ai_edit_stamp.stamp_value(NOW) == STAMP


# ── off unless configured, and the excluded paths ───────────────────────────


def test_unset_stamps_nothing():
    assert ai_edit_stamp.settings.last_ai_edit_field is None
    assert stamp("body\n") == "body\n"


@pytest.mark.parametrize("path", ["_/agent/Rules.md", "_/x.md", "CLAUDE.md"])
def test_excluded_paths_are_left_alone(on, path):
    assert stamp("body\n", path) == "body\n"


@pytest.mark.parametrize("path", ["Areas/CLAUDE.md", "Notes/_/x.md", "a_/x.md"])
def test_only_the_configured_patterns_are_excluded(on, path):
    assert stamp("body\n", path).startswith("---\nlast_ai_edit: ")


# ── the line edit ───────────────────────────────────────────────────────────


def test_a_note_without_properties_gets_a_block(on):
    assert stamp("# Title\nbody\n") == f"---\nlast_ai_edit: {STAMP}\n---\n# Title\nbody\n"


def test_an_empty_note_gets_a_block(on):
    assert stamp("") == f"---\nlast_ai_edit: {STAMP}\n---\n"


def test_the_property_is_appended_and_the_rest_kept_byte_identically(on):
    note = "---\ntitle: 'Quoted'  # a comment\ntags:\n  - a\n---\nbody\n"
    assert stamp(note) == (
        f"---\ntitle: 'Quoted'  # a comment\ntags:\n  - a\nlast_ai_edit: {STAMP}\n---\nbody\n"
    )


def test_a_date_only_value_from_before_is_replaced(on):
    """Agents stamped `YYYY-MM-DD` by hand before 2026-11-10: the key is found
    whatever its value, and the line is rewritten in the full format."""
    note = "---\nlast_ai_edit: 2026-11-06\ntitle: T\n---\nbody\n"
    assert stamp(note) == f"---\nlast_ai_edit: {STAMP}\ntitle: T\n---\nbody\n"


def test_a_quoted_or_multi_line_value_is_replaced_whole(on):
    note = "---\nlast_ai_edit:\n  - 2026-11-01\n  - 2026-11-02\nkeep: 1\n---\nbody\n"
    assert stamp(note) == f"---\nlast_ai_edit: {STAMP}\nkeep: 1\n---\nbody\n"
    note = "---\nlast_ai_edit: '2026-11-06'\n---\n"
    assert stamp(note) == f"---\nlast_ai_edit: {STAMP}\n---\n"


def test_an_empty_block_gets_the_property(on):
    assert stamp("---\n---\nbody\n") == f"---\nlast_ai_edit: {STAMP}\n---\nbody\n"


def test_a_crlf_note_stays_crlf(on):
    note = "---\r\ntitle: T\r\n---\r\nbody\r\n"
    assert stamp(note) == f"---\r\ntitle: T\r\nlast_ai_edit: {STAMP}\r\n---\r\nbody\r\n"


def test_an_empty_value_on_a_crlf_or_cr_note_is_replaced_not_duplicated(on):
    """Review finding 1: `field:` then `\r` was not read as the key, and a
    second key was appended."""
    note = "---\r\ntitle: a\r\nlast_ai_edit:\r\ntags: [x]\r\n---\r\n"
    assert stamp(note) == f"---\r\ntitle: a\r\nlast_ai_edit: {STAMP}\r\ntags: [x]\r\n---\r\n"
    note = "---\rlast_ai_edit:\r  - x\rkeep: 1\r---\r"
    assert stamp(note) == f"---\rlast_ai_edit: {STAMP}\rkeep: 1\r---\r"


@pytest.mark.parametrize("key", ['"last_ai_edit"', "'last_ai_edit'"])
def test_a_quoted_key_is_left_alone_rather_than_duplicated(on, key):
    note = f"---\n{key}: old\n---\nbody\n"
    assert stamp(note) == note


@pytest.mark.parametrize("note", ["\ufeff---\ntitle: a\n---\nbody\n", "--- \ntitle: a\n---\nbody\n", "---x\nbody\n"])
def test_no_block_is_prepended_where_another_reader_may_see_one(on, note):
    """Review finding 3: a BOM note, or a first line opening with `---`."""
    assert stamp(note) == note


def test_an_indented_comment_under_the_property_is_kept(on):
    note = "---\nlast_ai_edit: x\n  # keep me\ntitle: T\n---\n"
    assert stamp(note) == f"---\nlast_ai_edit: {STAMP}\n  # keep me\ntitle: T\n---\n"


def test_a_key_that_only_starts_like_the_property_is_not_it(on):
    note = "---\nlast_ai_edit_by: me\n---\n"
    assert stamp(note) == f"---\nlast_ai_edit_by: me\nlast_ai_edit: {STAMP}\n---\n"


@pytest.mark.parametrize(
    "note",
    [
        "---\ntitle: [unclosed\n---\nbody\n",  # YAML error
        "---\ntitle: T\nbody without a closing fence\n",  # unclosed fence
        "---\n- a list\n---\nbody\n",  # not a mapping
        "---\nlast_ai_edit: a\nlast_ai_edit: b\n---\n",  # duplicate key: would not re-parse as set
    ],
)
def test_a_block_it_cannot_edit_safely_is_left_alone(on, note):
    assert stamp(note) == note


# ── settings ────────────────────────────────────────────────────────────────


def _settings(**kw):
    return Settings(_env_file=None, secret_key="test", **kw)


def test_blank_settings_mean_unset():
    s = _settings(last_ai_edit_field="  ", archive_folder="")
    assert s.last_ai_edit_field is None and s.archive_folder is None


def test_the_exclude_list_takes_csv_or_json():
    assert _settings(last_ai_edit_exclude="_/*, CLAUDE.md").last_ai_edit_exclude == ["_/*", "CLAUDE.md"]
    assert _settings(last_ai_edit_exclude='["/_/*"]').last_ai_edit_exclude == ["_/*"]


@pytest.mark.parametrize("bad", ["a b", "a:b", "#x", "-x", "'q'", "true", "yes", "null", "Off"])
def test_the_property_name_must_be_plain(bad):
    with pytest.raises(ValueError):
        _settings(last_ai_edit_field=bad)


def test_the_archive_folder_loses_its_slashes():
    assert _settings(archive_folder="/Archive/").archive_folder == "Archive"


# ── through the tools ───────────────────────────────────────────────────────


@pytest.fixture
def vault(monkeypatch, tmp_path, on):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


def read(vault, name):
    return (vault / name).read_bytes().decode("utf-8")


def _hash_in(result) -> str:
    return re.search(r"content_hash: (sha256:[0-9a-f]{64})", str(result)).group(1)


def test_create_note_stamps_and_reports_the_written_hash(vault):
    result = asyncio.run(tools.create_note_impl("n.md", "body\n"))
    text = read(vault, "n.md")
    assert STAMP_RE.search(text) and text.endswith("---\nbody\n")
    assert _hash_in(result) == vault_service.content_hash_for_bytes(text.encode())


def test_edit_note_stamps_a_change_and_replaces_the_old_date(vault):
    (vault / "n.md").write_text("---\nlast_ai_edit: 2026-11-06\n---\nold\n")
    asyncio.run(tools.edit_note_impl("n.md", "new\n"))
    text = read(vault, "n.md")
    assert "2026-11-06" not in text and STAMP_RE.search(text) and text.endswith("new\n")


def test_edit_note_leaves_a_no_op_alone(vault):
    (vault / "n.md").write_text("plain\n")
    result = asyncio.run(tools.edit_note_impl("n.md", "plain\n"))
    assert read(vault, "n.md") == "plain\n", result


def test_edit_note_dry_run_shows_the_stamp_and_writes_nothing(vault):
    (vault / "n.md").write_text("old\n")
    diff = asyncio.run(tools.edit_note_impl("n.md", "new\n", dry_run=True))
    assert "+last_ai_edit: " in str(diff)
    assert read(vault, "n.md") == "old\n"


def test_set_frontmatter_stamps_after_the_caller_s_keys(vault):
    (vault / "n.md").write_text("---\na: 1\n---\nbody\n")
    asyncio.run(tools.set_frontmatter_impl("n.md", {"b": 2}))
    text = read(vault, "n.md")
    assert text.startswith("---\na: 1\nb: 2\nlast_ai_edit: ")


def test_an_excluded_note_is_written_unstamped(vault):
    (vault / "_").mkdir()
    asyncio.run(tools.create_note_impl("_/rules.md", "body\n"))
    assert read(vault, "_/rules.md") == "body\n"


def test_the_guide_names_the_property_only_when_it_is_on(vault, monkeypatch):
    assert "`last_ai_edit`" in tools._agent_edit_stamp_guide()
    monkeypatch.setattr(ai_edit_stamp.settings, "last_ai_edit_field", None)
    assert tools._agent_edit_stamp_guide() == ""


def test_a_task_line_moves_down_by_the_lines_the_stamp_adds(on):
    """`_publish_task_text` reports the task's line shifted by the block the
    stamp adds above it (3 lines for a new block, 1 for a new property)."""
    for text, shift in (("- [ ] t\n", 3), ("---\na: 1\n---\n- [ ] t\n", 1),
                        ("---\nlast_ai_edit: 2026-11-06\n---\n- [ ] t\n", 0)):
        stamped = stamp(text)
        assert tools._line_count(stamped) - tools._line_count(text) == shift


# ── the archive hint ────────────────────────────────────────────────────────


@pytest.fixture
def archive(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "archive_folder", "Archive")
    (vault / "Reference" / "Servers").mkdir(parents=True)
    (vault / "Archive" / "Reference" / "Servers").mkdir(parents=True)
    return vault


def hint(from_rel, to_rel):
    return tools._archive_counterpart_hint(None, from_rel, to_rel)


def test_leaving_a_folder_with_a_counterpart_says_so(archive):
    h = hint("Reference/Servers/a.md", "Reference/Servers/Windows/a.md")
    assert "Archive/Reference/Servers/" in h and "Nothing under Archive/ was changed" in h


@pytest.mark.parametrize(
    "from_rel,to_rel",
    [
        ("Reference/Servers/a.md", "Reference/Servers/b.md"),  # same folder: a rename
        ("Reference/Other/a.md", "Reference/Servers/a.md"),  # no counterpart
        ("Reference/Servers/a.md", "Archive/Reference/Servers/a.md"),  # archiving
        ("Archive/Reference/Servers/a.md", "Reference/Servers/a.md"),  # restoring
        ("a.md", "Reference/a.md"),  # from the root
    ],
)
def test_no_hint_otherwise(archive, from_rel, to_rel):
    assert hint(from_rel, to_rel) is None


def test_no_hint_when_unset(archive, monkeypatch):
    monkeypatch.setattr(tools.settings, "archive_folder", None)
    assert hint("Reference/Servers/a.md", "Elsewhere/a.md") is None


def test_move_note_carries_the_hint_and_moves_nothing_in_the_archive(archive, monkeypatch):
    async def no_refresh(*_a, **_k):
        return None

    (archive / "Reference" / "Servers" / "a.md").write_text("body\n")
    (archive / "Archive" / "Reference" / "Servers" / "old.md").write_text("old\n")
    result = asyncio.run(tools.move_note_impl("Reference/Servers/a.md", "Reference/Hosts/a.md"))
    assert "counterpart Archive/Reference/Servers/" in str(result)
    assert (archive / "Archive" / "Reference" / "Servers" / "old.md").read_text() == "old\n"
    assert not (archive / "Archive" / "Reference" / "Hosts").exists()


def test_set_frontmatter_removing_the_last_key_leaves_no_block(vault):
    """Review finding 5: the result says the block is gone, so no stamp
    brings one back."""
    (vault / "n.md").write_text("---\na: 1\n---\nbody\n")
    asyncio.run(tools.set_frontmatter_impl("n.md", {}, remove=["a"]))
    assert read(vault, "n.md") == "body\n"


def test_a_root_error_after_the_move_is_no_hint(archive, monkeypatch):
    """Review finding 6: `_vault_root` raises RuntimeError for an unassigned
    or quarantined root; the move has stood, so that is just no hint."""
    def boom(_uid):
        raise RuntimeError("quarantined")

    monkeypatch.setattr(tools, "_vault_root", boom)
    assert hint("Reference/Servers/a.md", "Elsewhere/a.md") is None
