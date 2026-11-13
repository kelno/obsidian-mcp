"""`create_task`, `edit_task`, `set_task_status`: what they write and refuse.

Offline: the vault is a tmp dir, usage logging is stubbed, and the two
database touches — the area index and the immediate task re-index — are
replaced by fakes, so these tests are about the bytes written. The database
side (the rows, `list_tasks`) is in tests/integration/test_task_tools_pg.py.
"""

import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from src.auth.session import current_user_id
from src.config import settings
from src import plugins
from src.mcp_server import tools
from src.mcp_server.auth import current_permission
from src.services import task_ops, task_query, vault as vault_service
from src.services.vault import content_hash_for_bytes


TODAY = date(2026, 10, 31)

OWNER_SETTINGS = {
    "globalFilter": "",
    "globalQuery": "",
    "setCreatedDate": False,
    "setDoneDate": True,
    "setCancelledDate": True,
    "taskFormat": "tasksPluginEmoji",
    "statusSettings": {
        "coreStatuses": [
            {"symbol": " ", "name": "Todo", "nextStatusSymbol": "x", "availableAsCommand": True, "type": "TODO"},
            {"symbol": "x", "name": "Done", "nextStatusSymbol": " ", "availableAsCommand": True, "type": "DONE"},
        ],
        "customStatuses": [
            {"symbol": "/", "name": "In Progress", "nextStatusSymbol": "x", "availableAsCommand": True, "type": "IN_PROGRESS"},
            {"symbol": "-", "name": "Cancelled", "nextStatusSymbol": " ", "availableAsCommand": True, "type": "CANCELLED"},
        ],
    },
}

#: The fake immediate re-index's record of calls: `(rel_path, text)`.
REINDEXED: list = []

AREAS = [
    ("projects/Area X/Area X.md", {"area_tag": "area/area-x"}),
    ("projects/Other/Other.md", {"area_tag": "area/other"}),
]


def code(result):
    assert result.error, result
    return json.loads(result.error.rsplit("\n", 1)[-1].removeprefix("MCP-REFUSAL "))["code"]


def write_settings(root, data=None):
    folder = root / ".obsidian" / "plugins" / "obsidian-tasks-plugin"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "data.json").write_text(json.dumps(OWNER_SETTINGS if data is None else data), encoding="utf-8")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    monkeypatch.setattr(tools.settings, "write_precondition_required", False)
    vault_service.clear_user_vault_cache()

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)

    async def areas(uid):
        return task_query.AreaIndex.from_rows(AREAS)

    monkeypatch.setattr(tools, "_area_index", areas)
    async def reindex(uid, rel, text):
        REINDEXED.append((rel, text))
        return True, "task index updated"

    monkeypatch.setattr(tools._task_index, "reindex_note_tasks_now", reindex)
    monkeypatch.setattr(task_ops, "owner_today", lambda tz, now=None: TODAY)
    write_settings(tmp_path)
    permission = current_permission.set("readwrite")
    user = current_user_id.set(None)
    REINDEXED[:] = []
    yield tmp_path
    current_user_id.reset(user)
    current_permission.reset(permission)


def note(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return content_hash_for_bytes(text.encode("utf-8"))


def read(root, rel):
    return (root / rel).read_bytes().decode("utf-8")


# --- create_task ---------------------------------------------------------------

async def test_create_appends_the_dialog_line_in_canonical_order(vault):
    note(vault, "n.md", "# T\n- [ ] old\n")
    result = await tools.create_task_impl(
        "Call the bank #next", path="n.md", due="2026-11-05", priority="high", scheduled="tomorrow"
    )
    assert result.error is None
    assert read(vault, "n.md") == "# T\n- [ ] old\n- [ ] Call the bank #next ⏫ ⏳ 2026-11-01 📅 2026-11-05\n"
    assert result.ref == "n.md:3"
    assert result.content_hash == content_hash_for_bytes(read(vault, "n.md").encode())
    assert REINDEXED == [("n.md", read(vault, "n.md"))]
    assert result.indexed is True


async def test_create_in_inbox_creates_it_when_missing(vault):
    result = await tools.create_task_impl("first")
    assert read(vault, "Tasks inbox.md") == "- [ ] first\n"
    assert result.created_note is True


async def test_create_refuses_a_missing_note_other_than_the_inbox(vault):
    result = await tools.create_task_impl("x", path="nope.md")
    assert code(result) == "not_found"
    assert not (vault / "nope.md").exists()


async def test_create_under_a_heading_goes_after_the_sections_last_line(vault):
    note(vault, "n.md", "# A\n- [ ] a1\n\n# B\ntext\n")
    result = await tools.create_task_impl("a2", path="n.md", heading="A")
    assert read(vault, "n.md") == "# A\n- [ ] a1\n- [ ] a2\n\n# B\ntext\n"
    assert result.line == 3


async def test_create_keeps_crlf_terminators(vault):
    note(vault, "n.md", "- [ ] a\r\n")
    await tools.create_task_impl("b", path="n.md")
    assert read(vault, "n.md") == "- [ ] a\r\n- [ ] b\r\n"


async def test_create_adds_a_terminator_to_an_unterminated_last_line(vault):
    note(vault, "n.md", "text")
    await tools.create_task_impl("b", path="n.md")
    assert read(vault, "n.md") == "text\n- [ ] b\n"


async def test_create_infers_the_area_from_the_folder(vault):
    note(vault, "projects/Area X/notes.md", "")
    await tools.create_task_impl("do", path="projects/Area X/notes.md")
    assert read(vault, "projects/Area X/notes.md") == "- [ ] do #area/area-x\n"


async def test_create_with_explicit_areas_and_none(vault):
    note(vault, "projects/Area X/notes.md", "")
    await tools.create_task_impl("a", path="projects/Area X/notes.md", areas=["other", "#area/area-x"])
    await tools.create_task_impl("b", path="projects/Area X/notes.md", areas=[])
    assert read(vault, "projects/Area X/notes.md") == "- [ ] a #area/other #area/area-x\n- [ ] b\n"


async def test_create_refuses_an_unknown_area(vault):
    note(vault, "n.md", "")
    result = await tools.create_task_impl("a", path="n.md", areas=["nope"])
    assert code(result) == "invalid_argument"
    assert read(vault, "n.md") == ""


async def test_create_refuses_a_description_that_reads_back_as_a_field(vault):
    note(vault, "n.md", "")
    result = await tools.create_task_impl("pay rent 📅 2026-11-01", path="n.md")
    assert code(result) == "invalid_argument"
    assert read(vault, "n.md") == ""


async def test_create_refuses_inside_an_unclosed_fence(vault):
    note(vault, "n.md", "```\ncode\n")
    result = await tools.create_task_impl("a", path="n.md")
    assert code(result) == "content_unsafe"
    assert read(vault, "n.md") == "```\ncode\n"


async def test_create_done_adds_the_done_date(vault):
    note(vault, "n.md", "")
    await tools.create_task_impl("already", path="n.md", status="done")
    assert read(vault, "n.md") == "- [x] already ✅ 2026-10-31\n"


async def test_create_follows_set_created_date_and_global_filter(vault):
    write_settings(vault, {**OWNER_SETTINGS, "setCreatedDate": True, "globalFilter": "#task"})
    note(vault, "n.md", "")
    await tools.create_task_impl("a", path="n.md")
    assert read(vault, "n.md") == "- [ ] #task a ➕ 2026-10-31\n"


@pytest.mark.parametrize("description", ["#task a", "#task #task a", "a #task", "#task a #task", "a  #task"])
async def test_create_writes_the_global_filter_once(vault, description):
    write_settings(vault, {**OWNER_SETTINGS, "globalFilter": "#task"})
    note(vault, "n.md", "")
    result = await tools.create_task_impl(description, path="n.md")
    assert result.error is None
    assert read(vault, "n.md") == "- [ ] #task a\n"


@pytest.mark.parametrize(("description", "written"), [
    ("x  y #task z", "x  y z"),
    ("a #task #task #task b", "a b"),
    ("a #task b", "a b"),
])
async def test_create_drops_the_filter_without_touching_other_spacing(vault, description, written):
    write_settings(vault, {**OWNER_SETTINGS, "globalFilter": "#task"})
    note(vault, "n.md", "")
    await tools.create_task_impl(description, path="n.md")
    assert read(vault, "n.md") == f"- [ ] #task {written}\n"


async def test_create_keeps_a_tag_that_only_starts_with_the_global_filter(vault):
    write_settings(vault, {**OWNER_SETTINGS, "globalFilter": "#task"})
    note(vault, "n.md", "")
    await tools.create_task_impl("#tasks a #task/x", path="n.md")
    assert read(vault, "n.md") == "- [ ] #task #tasks a #task/x\n"


async def test_create_with_only_the_global_filter_is_refused(vault):
    write_settings(vault, {**OWNER_SETTINGS, "globalFilter": "#task"})
    note(vault, "n.md", "")
    result = await tools.create_task_impl("#task #task", path="n.md")
    assert code(result) == "invalid_argument"
    assert read(vault, "n.md") == ""


async def test_create_refuses_a_stale_hash(vault):
    note(vault, "n.md", "a\n")
    result = await tools.create_task_impl("b", path="n.md", expected_hash="sha256:" + "0" * 64)
    assert code(result) == "stale_precondition"
    assert read(vault, "n.md") == "a\n"


async def test_create_refuses_unusable_settings(vault):
    (vault / ".obsidian/plugins/obsidian-tasks-plugin/data.json").write_text("{not json", encoding="utf-8")
    note(vault, "n.md", "")
    result = await tools.create_task_impl("a", path="n.md")
    assert code(result) == "task_settings_invalid"


async def test_absent_settings_file_means_plugin_defaults(vault):
    (vault / ".obsidian/plugins/obsidian-tasks-plugin/data.json").unlink()
    note(vault, "n.md", "")
    result = await tools.create_task_impl("a", path="n.md", status="done")
    assert result.error is None
    assert read(vault, "n.md") == "- [x] a ✅ 2026-10-31\n"


# --- set_task_status -------------------------------------------------------------

async def status(vault, text, line, expected, **kw):
    h = note(vault, "n.md", text)
    return await tools.set_task_status_impl("n.md", line, expected, h, **kw)


async def test_done_adds_done_date_and_changes_only_that_line(vault):
    result = await status(vault, "# T\n- [ ] a 📅 2026-11-01\n- [ ] b\n", 2, "- [ ] a 📅 2026-11-01", status="done")
    assert read(vault, "n.md") == "# T\n- [x] a 📅 2026-11-01 ✅ 2026-10-31\n- [ ] b\n"
    assert result.previous_text == "- [ ] a 📅 2026-11-01"
    assert result.text == "- [x] a 📅 2026-11-01 ✅ 2026-10-31"


async def test_cancel_and_back_to_todo(vault):
    await status(vault, "- [ ] a\n", 1, "- [ ] a", status="cancelled")
    assert read(vault, "n.md") == "- [-] a ❌ 2026-10-31\n"
    h = content_hash_for_bytes((vault / "n.md").read_bytes())
    await tools.set_task_status_impl("n.md", 1, "- [-] a ❌ 2026-10-31", h, status="todo")
    assert read(vault, "n.md") == "- [ ] a\n"


async def test_custom_status_by_symbol(vault):
    await status(vault, "- [ ] a\n", 1, "- [ ] a", symbol="/")
    assert read(vault, "n.md") == "- [/] a\n"


async def test_unknown_symbol_is_refused(vault):
    result = await status(vault, "- [ ] a\n", 1, "- [ ] a", symbol="?")
    assert code(result) == "invalid_argument"


async def test_same_status_writes_nothing(vault):
    result = await status(vault, "- [x] a ✅ 2026-10-01\n", 1, "- [x] a ✅ 2026-10-01", status="done")
    assert result.changed is False
    assert read(vault, "n.md") == "- [x] a ✅ 2026-10-01\n"
    assert REINDEXED == []


async def test_on_completion_delete_removes_the_line(vault):
    result = await status(vault, "- [ ] a 🏁 delete\n- [ ] b\n", 1, "- [ ] a 🏁 delete", status="done")
    assert read(vault, "n.md") == "- [ ] b\n"
    assert result.removed is True


async def test_on_completion_delete_of_the_last_unterminated_line(vault):
    await status(vault, "- [ ] b\n- [ ] a 🏁 delete", 2, "- [ ] a 🏁 delete", status="done")
    assert read(vault, "n.md") == "- [ ] b"


async def test_on_completion_keep_keeps_it(vault):
    await status(vault, "- [ ] a 🏁 keep\n", 1, "- [ ] a 🏁 keep", status="done")
    assert read(vault, "n.md") == "- [x] a 🏁 keep ✅ 2026-10-31\n"


async def test_recurring_task_is_refused(vault):
    text = "- [ ] water plants 🔁 every week 📅 2026-10-31\n"
    result = await status(vault, text, 1, text.rstrip("\n"), status="done")
    assert code(result) == "recurring_task"
    assert "Obsidian" in result.error
    assert read(vault, "n.md") == text


async def test_changed_hash_is_refused(vault):
    note(vault, "n.md", "- [ ] a\n")
    result = await tools.set_task_status_impl("n.md", 1, "- [ ] a", "sha256:" + "1" * 64, status="done")
    assert code(result) == "stale_precondition"
    assert read(vault, "n.md") == "- [ ] a\n"


async def test_changed_line_is_refused(vault):
    result = await status(vault, "- [ ] a\n- [ ] b\n", 1, "- [ ] b", status="done")
    assert code(result) == "line_mismatch"


async def test_line_out_of_range_is_refused(vault):
    result = await status(vault, "- [ ] a\n", 9, "- [ ] a", status="done")
    assert code(result) == "line_mismatch"


@pytest.mark.parametrize("text", [
    "- \\[ ] escaped legacy",
    "- plain item",
    "-\t[ ] tab after marker",
])
async def test_not_a_task_is_refused(vault, text):
    result = await status(vault, text + "\n", 1, text, status="done")
    assert code(result) == "not_a_task"
    assert read(vault, "n.md") == text + "\n"


async def test_task_in_code_block_is_refused(vault):
    result = await status(vault, "```\n- [ ] code\n```\n", 2, "- [ ] code", status="done")
    assert code(result) == "not_a_task"


async def test_global_filter_makes_a_line_without_it_not_a_task(vault):
    write_settings(vault, {**OWNER_SETTINGS, "globalFilter": "#task"})
    result = await status(vault, "- [ ] a\n", 1, "- [ ] a", status="done")
    assert code(result) == "not_a_task"


async def test_invalid_date_line_is_refused(vault):
    result = await status(vault, "- [ ] a 📅 2026-02-30\n", 1, "- [ ] a 📅 2026-02-30", status="done")
    assert code(result) == "not_a_task"


async def test_missing_hash_is_a_protocol_argument_error():
    with pytest.raises(TypeError):
        await tools.set_task_status_impl("n.md", 1, "- [ ] a", status="done")


async def test_read_only_key_is_refused(vault):
    token = current_permission.set("read")
    try:
        result = await status(vault, "- [ ] a\n", 1, "- [ ] a", status="done")
    finally:
        current_permission.reset(token)
    assert code(result) == "permission_denied"


async def test_done_date_uses_the_owner_timezone(vault, monkeypatch):
    # 23:30 UTC on 31 Oct is already 1 Nov in Brussels (UTC+1 after the DST change).
    late = datetime(2026, 10, 31, 23, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(task_ops, "owner_today", lambda tz, now=None: _real_owner_today(tz, late))
    await status(vault, "- [ ] a\n", 1, "- [ ] a", status="done")
    assert read(vault, "n.md") == "- [x] a ✅ 2026-11-01\n"


_real_owner_today = task_ops.owner_today


def test_owner_today_both_sides_of_midnight():
    assert _real_owner_today("Europe/Brussels", datetime(2026, 10, 31, 22, 59, tzinfo=timezone.utc)) == date(2026, 10, 31)
    assert _real_owner_today("Europe/Brussels", datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)) == date(2026, 11, 1)
    # Summer time: UTC+2.
    assert _real_owner_today("Europe/Brussels", datetime(2026, 7, 1, 21, 59, tzinfo=timezone.utc)) == date(2026, 7, 1)
    assert _real_owner_today("Europe/Brussels", datetime(2026, 7, 1, 22, 0, tzinfo=timezone.utc)) == date(2026, 7, 2)


def test_unknown_timezone_is_refused():
    with pytest.raises(task_ops.TaskOpError):
        _real_owner_today("Mars/Olympus")


def test_startup_stops_on_an_unknown_timezone(monkeypatch):
    from src import main

    monkeypatch.setattr(task_ops, "owner_today", _real_owner_today)
    monkeypatch.setattr(main.settings, "tasks_timezone", "Mars/Olympus")
    with pytest.raises(SystemExit):
        main._check_tasks_timezone()
    monkeypatch.setattr(main.settings, "tasks_timezone", "Europe/Brussels")
    main._check_tasks_timezone()


# --- edit_task -------------------------------------------------------------------

async def edit(vault, text, line, expected, **kw):
    h = note(vault, "n.md", text)
    return await tools.edit_task_impl("n.md", line, expected, h, **kw)


async def test_postpone_sets_scheduled(vault):
    await edit(vault, "- [ ] a 📅 2026-11-05\n", 1, "- [ ] a 📅 2026-11-05", scheduled="2026-11-03")
    assert read(vault, "n.md") == "- [ ] a ⏳ 2026-11-03 📅 2026-11-05\n"


async def test_edit_rewrites_in_canonical_order_like_the_plugin(vault):
    text = "- [ ] a 📅 2026-11-05 🔼"
    await edit(vault, text + "\n", 1, text, description="b #next")
    assert read(vault, "n.md") == "- [ ] b #next 🔼 📅 2026-11-05\n"


@pytest.mark.parametrize("description", ["#task b #area/x", "b #area/x", "#task #task b #area/x"])
async def test_edit_round_trips_the_listed_description_without_doubling_the_filter(vault, description):
    # The postbox case: an agent passes back `list_tasks`' description, which carries the filter.
    write_settings(vault, {**OWNER_SETTINGS, "globalFilter": "#task"})
    text = "- [ ] #task a #area/x"
    result = await edit(vault, text + "\n", 1, text, description=description)
    assert result.error is None
    assert read(vault, "n.md") == "- [ ] #task b #area/x\n"


async def test_edit_with_only_the_global_filter_is_refused(vault):
    write_settings(vault, {**OWNER_SETTINGS, "globalFilter": "#task"})
    text = "- [ ] #task a"
    result = await edit(vault, text + "\n", 1, text, description="#task")
    assert code(result) == "invalid_argument"
    assert read(vault, "n.md") == text + "\n"


async def test_edit_clear_and_priority(vault):
    text = "- [ ] a ⏫ 📅 2026-11-05"
    await edit(vault, text + "\n", 1, text, clear=["due", "priority"])
    assert read(vault, "n.md") == "- [ ] a\n"


async def test_edit_keeps_indentation_marker_and_hard_break(vault):
    text = "  1. [/] a  "
    await edit(vault, text + "\n", 1, text, due="today")
    assert read(vault, "n.md") == "  1. [/] a 📅 2026-10-31  \n"


async def test_edit_nothing_given_is_refused(vault):
    result = await edit(vault, "- [ ] a\n", 1, "- [ ] a")
    assert code(result) == "invalid_argument"


async def test_edit_recurring_is_refused(vault):
    text = "- [ ] a 🔁 every day"
    result = await edit(vault, text + "\n", 1, text, due="today")
    assert code(result) == "recurring_task"


async def test_edit_no_change_writes_nothing(vault):
    result = await edit(vault, "- [ ] a 🔼\n", 1, "- [ ] a 🔼", priority="medium")
    assert result.changed is False and REINDEXED == []


async def test_edit_bad_date_is_refused(vault):
    result = await edit(vault, "- [ ] a\n", 1, "- [ ] a", due="next week")
    assert code(result) == "invalid_argument"


async def test_edit_in_frontmatter_note_counts_lines_over_the_whole_file(vault):
    text = "---\ntags: [x]\n---\n- [ ] a\n"
    await edit(vault, text, 4, "- [ ] a", priority="low")
    assert read(vault, "n.md") == "---\ntags: [x]\n---\n- [ ] a 🔽\n"


# --- the extraction-version bump re-embeds nothing ---------------------------------

@pytest.mark.parametrize("body", ["# h\n\n```\ncode\n```\n[[Link]] - [ ] task", ""])
def test_version_four_shares_the_version_three_cleaner(body):
    from src.services import indexer
    from src.services.embeddings import clean_at_version

    assert indexer.CURRENT_EXTRACTION_VERSION == 4
    assert clean_at_version(4, body) == clean_at_version(3, body)
    assert indexer._grammar_changed_the_embedding_text(3, body) is False


# --- list_tasks, offline (the database is a fake holding index rows) ---------------

class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        return _Rows(self.rows)


def _row(path, line_number, kind, line, reason=None):
    from types import SimpleNamespace

    return SimpleNamespace(file_path=path, line_number=line_number, kind=kind, line=line, reason=reason)


async def test_list_tasks_success_shape(vault, monkeypatch):
    h = note(vault, "n.md", "# T\n- [ ] a #next 📅 2026-11-01\n- [x] b\n-\t[ ] near\n")
    note(vault, "m.md", "- [ ] changed since indexed\n")
    rows = [
        _row("n.md", 2, "task", "- [ ] a #next 📅 2026-11-01"),
        _row("n.md", 3, "task", "- [x] b"),
        _row("n.md", 4, "problem", "-\t[ ] near", "whitespace_after_marker"),
        _row("m.md", 1, "task", "- [ ] old text"),
    ]
    monkeypatch.setattr(tools, "async_session", lambda: _FakeSession(rows))
    result = await tools.list_tasks_impl()
    assert result.error is None
    assert [t.ref for t in result.tasks] == ["n.md:2"]
    task = result.tasks[0]
    assert task.content_hash == h and task.due == "2026-11-01" and task.tags == ["#next"]
    assert task.status_type == "TODO" and task.priority == "none"
    assert [s.ref for s in result.stale] == ["m.md:1"]
    assert result.total_matched == 2
    assert [p.reason for p in result.problems] == ["whitespace_after_marker"]
    assert result.query == "```tasks\nnot done\nlimit 50\n```"
    assert result.today == "2026-10-31"
    assert result.settings["source"] == "file"


async def test_list_tasks_problems_in_a_private_folder_come_without_text(vault, monkeypatch):
    # F63: problems come back with every query; a private note's line is path:line and reason only.
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])
    rows = [
        _row("People (private)/Sam.md", 3, "problem", "-\t[ ] secret", "whitespace_after_marker"),
        _row("Areas/x.md", 4, "problem", "-\t[ ] open", "whitespace_after_marker"),
    ]
    monkeypatch.setattr(tools, "async_session", lambda: _FakeSession(rows))
    result = await tools.list_tasks_impl()
    assert result.error is None
    by_ref = {p.ref: p for p in result.problems}
    hidden, shown = by_ref["People (private)/Sam.md:3"], by_ref["Areas/x.md:4"]
    assert hidden.text is None and hidden.reason == "whitespace_after_marker" and hidden.line == 3
    assert shown.text == "-\t[ ] open"
    assert "secret" not in result.model_dump_json()
    assert any("private folder" in n for n in result.notes)


async def test_list_tasks_private_tasks_by_location_unless_include_private(vault, monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])
    note(vault, "People (private)/Sam.md", "# S\n- [ ] call Sam about the secret\n- [ ] old\n")
    h = note(vault, "Areas/x.md", "- [ ] open thing\n")
    rows = [
        _row("People (private)/Sam.md", 2, "task", "- [ ] call Sam about the secret"),
        _row("People (private)/Sam.md", 3, "task", "- [ ] stale secret line"),
        _row("Areas/x.md", 1, "task", "- [ ] open thing"),
    ]
    monkeypatch.setattr(tools, "async_session", lambda: _FakeSession(rows))
    result = await tools.list_tasks_impl()
    assert result.error is None
    by_ref = {t.ref: t for t in result.tasks}
    hidden = by_ref["People (private)/Sam.md:2"]
    assert hidden.private is True and hidden.status_type == "TODO"
    assert hidden.text is None and hidden.description is None and hidden.content_hash is None
    assert by_ref["Areas/x.md:1"].text == "- [ ] open thing" and by_ref["Areas/x.md:1"].content_hash == h
    assert [s.ref for s in result.stale] == ["People (private)/Sam.md:3"] and result.stale[0].indexed_text is None
    dumped = result.model_dump_json()
    assert "secret" not in dumped and '"private":true' in dumped
    assert any("location only" in n for n in result.notes)

    # Naming the folder in `path` no longer opens it (P98): only the argument does.
    named = await tools.list_tasks_impl(path="people (private)/")
    assert named.tasks[0].private is True and named.tasks[0].text is None

    scoped = await tools.list_tasks_impl(path="people (private)/", include_private=True)
    assert [t.text for t in scoped.tasks] == ["- [ ] call Sam about the secret"]
    assert scoped.tasks[0].private is None and scoped.stale[0].indexed_text == "- [ ] stale secret line"
    assert not any("location only" in n for n in scoped.notes or [])
    everywhere = await tools.list_tasks_impl(include_private=True)
    assert "secret" in everywhere.model_dump_json()


async def test_list_tasks_without_private_folders_configured_hides_nothing(vault, monkeypatch):
    # The core serves any vault (P96): with PRIVATE_FOLDER_NAMES unset, a
    # folder named like the owner's private ones is an ordinary folder.
    monkeypatch.setattr(settings, "private_folder_names", [])
    note(vault, "People (private)/Sam.md", "# S\n- [ ] call Sam\n")
    rows = [_row("People (private)/Sam.md", 2, "task", "- [ ] call Sam")]
    monkeypatch.setattr(tools, "async_session", lambda: _FakeSession(rows))
    result = await tools.list_tasks_impl()
    assert [t.text for t in result.tasks] == ["- [ ] call Sam"] and result.tasks[0].private is None
    assert not any("private folder" in n for n in result.notes or [])


async def test_list_tasks_refuses_an_unsupported_global_query(vault, monkeypatch):
    write_settings(vault, {**OWNER_SETTINGS, "globalQuery": "not done\ngroup by path"})
    result = await tools.list_tasks_impl()
    assert code(result) == "invalid_argument"
    assert "group by path" in result.error


async def test_list_tasks_applies_a_supported_global_query(vault, monkeypatch):
    write_settings(vault, {**OWNER_SETTINGS, "globalQuery": "# only the inbox\npath includes inbox"})
    note(vault, "Tasks inbox.md", "- [ ] in\n")
    note(vault, "other.md", "- [ ] out\n")
    rows = [_row("Tasks inbox.md", 1, "task", "- [ ] in"), _row("other.md", 1, "task", "- [ ] out")]
    monkeypatch.setattr(tools, "async_session", lambda: _FakeSession(rows))
    result = await tools.list_tasks_impl()
    assert [t.ref for t in result.tasks] == ["Tasks inbox.md:1"]
    assert result.notes


async def test_list_tasks_bad_limit_area_and_date(vault):
    assert code(await tools.list_tasks_impl(limit=0)) == "invalid_argument"
    assert code(await tools.list_tasks_impl(area="nope")) == "invalid_argument"
    assert code(await tools.list_tasks_impl(due="before next week")) == "invalid_argument"


# --- found by the adversarial review ---------------------------------------------

async def test_a_line_separator_in_the_line_is_refused_not_truncated(vault):
    text = "- [ ] call the bank 📅 2026-11-05 ^blk"
    result = await status(vault, text + "\nnext\n", 1, text, status="done")
    assert code(result) == "not_a_task"
    assert read(vault, "n.md") == text + "\nnext\n"


async def test_a_line_in_defective_frontmatter_is_refused(vault):
    text = "---\nlist:\n- [ ] x\n---\nbody\n"
    result = await status(vault, text, 3, "- [ ] x", status="done")
    assert code(result) == "not_a_task"
    assert read(vault, "n.md") == text


async def test_an_unpaired_comment_marker_in_a_description_is_refused(vault):
    note(vault, "n.md", "## A\n- [ ] a\n## B\n- [ ] b\nfinal paragraph\n")
    result = await tools.create_task_impl("discount 50%% today", path="n.md", heading="A")
    assert code(result) == "invalid_argument"
    assert read(vault, "n.md") == "## A\n- [ ] a\n## B\n- [ ] b\nfinal paragraph\n"


async def test_a_paired_comment_in_a_description_is_fine(vault):
    note(vault, "n.md", "- [ ] a\n")
    result = await tools.create_task_impl("b %%note%%", path="n.md")
    assert result.error is None


async def test_an_edit_that_changes_how_other_lines_read_is_refused(vault, monkeypatch):
    # Force the read-back to pass so the other-lines check is what refuses.
    monkeypatch.setattr(task_ops, "_toggles_comment", lambda line: 0)
    text = "- [ ] a"
    result = await edit(vault, text + "\n- [ ] b\n", 1, text, description="opens %% here")
    assert code(result) == "content_unsafe"
    assert read(vault, "n.md") == "- [ ] a\n- [ ] b\n"


async def test_create_under_a_heading_stays_above_its_subsections(vault):
    note(vault, "n.md", "## A\n- [ ] a1\n### A1\n- [ ] sub\n## B\n")
    result = await tools.create_task_impl("new one", path="n.md", heading="A")
    assert read(vault, "n.md") == "## A\n- [ ] a1\n- [ ] new one\n### A1\n- [ ] sub\n## B\n"
    assert result.line == 3


async def test_filename_date_notes_accept_create_and_dated_edits(vault):
    write_settings(vault, {**OWNER_SETTINGS, "useFilenameAsScheduledDate": True})
    note(vault, "daily/2026-10-31.md", "- [ ] plain\n")
    created = await tools.create_task_impl("second", path="daily/2026-10-31.md")
    assert created.error is None
    h = content_hash_for_bytes((vault / "daily/2026-10-31.md").read_bytes())
    edited = await tools.edit_task_impl("daily/2026-10-31.md", 1, "- [ ] plain", h, due="2026-11-05")
    assert edited.error is None
    assert read(vault, "daily/2026-10-31.md") == "- [ ] plain 📅 2026-11-05\n- [ ] second\n"


async def test_a_duplicate_field_is_refused_not_dropped(vault):
    text = "- [ ] a 📅 2026-11-05 📅 2026-12-01"
    result = await edit(vault, text + "\n", 1, text, priority="low")
    assert code(result) == "not_a_task"
    assert read(vault, "n.md") == text + "\n"



# ── The note tools refuse task lines written by hand ─────────────────────────
# The refusal is this vault's rule, the `kelno_vault` plugin's check.

FILTERED = {**OWNER_SETTINGS, "globalFilter": "#task"}


@pytest.fixture
def kelno_vault(monkeypatch):
    monkeypatch.setattr(plugins, "active", lambda: plugins.load(("kelno_vault",)))


def test_without_the_vault_plugin_the_note_tools_write_task_lines(vault):
    write_settings(vault, FILTERED)
    created = asyncio.run(tools.create_note_impl("c.md", "- [ ] #task Hand-written\n"))
    assert created.startswith("Created note") and "task_line_by_hand" not in created


def test_edit_note_refuses_a_new_or_changed_task_line_and_writes_nothing(vault, kelno_vault):
    write_settings(vault, FILTERED)
    h = note(vault, "n.md", "# N\n\n- [ ] #task Old one\n- [ ] plain checkbox\n")
    added = asyncio.run(tools.edit_note_impl("n.md", "- [ ] #task New one", append=True))
    assert code(_Refused(added)) == "task_line_by_hand"
    assert "line 6: - [ ] #task New one" in added and "create_task" in added
    changed = asyncio.run(tools.edit_note_impl("n.md", "- [x] #task Old one", find="- [ ] #task Old one"))
    assert code(_Refused(changed)) == "task_line_by_hand" and "edit_task" in changed
    dry = asyncio.run(tools.edit_note_impl("n.md", "- [ ] #task Dry", append=True, dry_run=True))
    assert code(_Refused(dry)) == "task_line_by_hand"
    assert content_hash_for_bytes((vault / "n.md").read_bytes()) == h


def test_edit_note_allows_deleting_moving_mentioning_and_untouched_task_lines(vault, kelno_vault):
    write_settings(vault, FILTERED)
    note(vault, "m.md", "# M\n\n- [ ] #task Keep\n- [ ] #task Drop\nText\n")
    ok = [
        asyncio.run(tools.edit_note_impl("m.md", "", find="- [ ] #task Drop\n")),                 # delete
        asyncio.run(tools.edit_note_impl("m.md", "Text, reworded", find="Text")),               # untouched
        asyncio.run(tools.edit_note_impl("m.md", "Use `- [ ] #task x` via create_task.\n- [ ] a plain box",
                                         append=True)),                                          # mentions
        asyncio.run(tools.edit_note_impl("m.md", "# M\n\nText, reworded\n- [ ] #task Keep\n")),   # moved
        asyncio.run(tools.edit_note_impl("m.md", "```\n- [ ] #task in a fence\n```", append=True)),
    ]
    for result in ok:
        assert result.startswith(("Updated note", "Replaced")), result


def test_create_note_and_write_file_refuse_task_lines_but_not_other_checkboxes(vault, kelno_vault):
    write_settings(vault, FILTERED)
    refused = asyncio.run(tools.create_note_impl("c.md", "- [ ] #task Hand-written\n"))
    assert code(_Refused(refused)) == "task_line_by_hand" and not (vault / "c.md").exists()
    plain = asyncio.run(tools.create_note_impl("p.md", "- [ ] just a box\n"))
    assert plain.startswith("Created note")
    note(vault, "w.md", "- [ ] #task Same\n")
    same = asyncio.run(tools.write_file_impl("w.md", "Intro\n- [ ] #task Same\n", encoding="text", overwrite=True))
    assert same.startswith("Wrote")
    new = asyncio.run(tools.write_file_impl("w.md", "- [ ] #task Other\n", encoding="text", overwrite=True))
    assert code(_Refused(new)) == "task_line_by_hand"
    txt = asyncio.run(tools.write_file_impl("t.txt", "- [ ] #task not a note\n", encoding="text"))
    assert txt.startswith("Wrote")
    import base64
    latin = base64.b64encode(b"caf\xe9\n- [ ] #task Sneaky\n").decode()
    assert code(_Refused(asyncio.run(tools.write_file_impl("l.md", latin)))) == "task_line_by_hand"
    exists = asyncio.run(tools.create_note_impl("w.md", "- [ ] #task Same\n"))
    assert code(_Refused(exists)) == "already_exists"


def test_trailing_whitespace_and_indentation_are_not_a_change(vault, kelno_vault):
    write_settings(vault, FILTERED)
    note(vault, "s.md", "## Next\n- [ ] #task Keep  \n\t- [ ] #task Sub\n")
    ok = asyncio.run(tools.edit_note_impl("s.md", "- [ ] #task Keep\n    - [ ] #task Sub\n", section="Next"))
    assert ok.startswith("Updated note"), ok


def test_without_a_tasks_settings_file_checkboxes_are_not_checked(vault, kelno_vault):
    (vault / ".obsidian" / "plugins" / "obsidian-tasks-plugin" / "data.json").unlink()
    created = asyncio.run(tools.create_note_impl("x.md", "- [ ] #task anything\n"))
    assert created.startswith("Created note")


def test_settings_the_task_tools_cannot_apply_skip_the_check_quietly(vault, kelno_vault):
    write_settings(vault, {**FILTERED, "useFilenameAsScheduledDate": True,
                           "filenameAsScheduledDateFormat": "DD-MM-YYYY"})
    created = asyncio.run(tools.create_note_impl("13-11-2026.md", "- [ ] #task anything\n"))
    assert created.startswith("Created note") and "plugin_error" not in created


class _Refused:
    """A string refusal, shaped for `code()`."""

    def __init__(self, text):
        self.error = text



# ── A first heading repeating the note's name ────────────────────────────────

def test_the_note_tools_drop_a_title_heading_they_would_add(vault, monkeypatch):
    monkeypatch.setattr(plugins, "active", lambda: plugins.load(("kelno_vault",)))   # the title rule is this vault's plugin
    created = asyncio.run(tools.create_note_impl("Dog food.md", "# Dog food\n\nKibble.\n"))
    assert "title_heading_removed: the first line, `# Dog food`" in created
    assert read(vault, "Dog food.md") == "Kibble.\n"
    note(vault, "Vet.md", "Old body\n")
    dry = asyncio.run(tools.edit_note_impl("Vet.md", "# Vet\n\nNew body\n", dry_run=True))
    assert "+New body" in dry and "+# Vet" not in dry and "would be removed" in dry
    kept = note(vault, "Kept.md", "# Kept\n\nBody\n")
    untouched = asyncio.run(tools.edit_note_impl("Kept.md", "More", append=True))
    assert "title_heading_removed" not in untouched and read(vault, "Kept.md").startswith("# Kept")
    written = asyncio.run(tools.write_file_impl("W.md", "# w\nText\n", encoding="text"))
    assert "title_heading_removed" in written and read(vault, "W.md") == "Text\n"


def test_a_heading_that_is_the_whole_change_is_no_change_in_both_modes(vault, monkeypatch):
    monkeypatch.setattr(plugins, "active", lambda: plugins.load(("kelno_vault",)))   # the title rule is this vault's plugin
    h = note(vault, "Dog food.md", "Body\n")
    dry = asyncio.run(tools.edit_note_impl("Dog food.md", "# Dog food\nBody\n", dry_run=True))
    real = asyncio.run(tools.edit_note_impl("Dog food.md", "# Dog food\nBody\n"))
    assert dry.startswith("No changes for Dog food.md") and "would be removed" in dry
    assert real.startswith("No changes for Dog food.md") and "was removed" in real
    assert content_hash_for_bytes((vault / "Dog food.md").read_bytes()) == h



# ── Line endings: the caller's LF and the note's CRLF match, and the note keeps its own ──

def test_a_multi_line_find_matches_a_crlf_note_and_the_note_stays_crlf(vault):
    note(vault, "c.md", "intro\r\nline one\r\nline two\r\nend\r\n")
    result = asyncio.run(tools.edit_note_impl("c.md", "first\nsecond", find="line one\nline two"))
    assert result.startswith("Updated note"), result
    assert read(vault, "c.md") == "intro\r\nfirst\r\nsecond\r\nend\r\n"
    appended = asyncio.run(tools.edit_note_impl("c.md", "more\nlines", append=True))
    assert appended.startswith("Updated note")
    assert read(vault, "c.md") == "intro\r\nfirst\r\nsecond\r\nend\r\n\r\nmore\r\nlines"


def test_an_lf_note_stays_lf_when_the_caller_sends_crlf(vault):
    note(vault, "l.md", "a\nb\nc\n")
    asyncio.run(tools.edit_note_impl("l.md", "B1\r\nB2", find="b\r\nc"))
    assert read(vault, "l.md") == "a\nB1\nB2\n"
    asyncio.run(tools.edit_note_impl("l.md", "x\r\ny\r\n"))
    assert read(vault, "l.md") == "x\ny\n"


def test_replace_frontmatter_stays_byte_exact(vault):
    note(vault, "e.md", "a\r\nb\r\n")
    asyncio.run(tools.edit_note_impl("e.md", "x\ny\n", replace_frontmatter=True))
    assert read(vault, "e.md") == "x\ny\n"



def test_line_ending_review_cases(vault, monkeypatch):
    # A find starting with a line break takes the whole CRLF, not its LF half.
    note(vault, "r1.md", "intro\r\n## B\r\nx\r\n")
    asyncio.run(tools.edit_note_impl("r1.md", "\n## C", find="\n## B"))
    assert read(vault, "r1.md") == "intro\r\n## C\r\nx\r\n"
    # A find ending with a CR takes the whole CRLF.
    note(vault, "r2.md", "one\r\ntwo\r\n")
    asyncio.run(tools.edit_note_impl("r2.md", "ONE\r", find="one\r"))
    assert read(vault, "r2.md") == "ONE\r\ntwo\r\n"
    # Mixed endings: every copy counts, whatever its ending.
    note(vault, "r3.md", "a\r\nb\r\nz\r\na\nb\n")
    ambiguous = asyncio.run(tools.edit_note_impl("r3.md", "x", find="a\nb"))
    assert '"code":"match_ambiguous"' in ambiguous
    replaced = asyncio.run(tools.edit_note_impl("r3.md", "x", find="a\nb", replace_all=True))
    assert replaced.startswith("Replaced 2 occurrence(s)")
    # A section with no trailing newline keeps the note's CRLF around it.
    note(vault, "r4.md", "## Tasks\r\nold\r\n## Next\r\nkeep\r\n")
    asyncio.run(tools.edit_note_impl("r4.md", "new text", section="Tasks"))
    assert read(vault, "r4.md") == "## Tasks\r\nnew text\r\n## Next\r\nkeep\r\n"
    # A lone-CR note still loses a title heading the write adds.
    monkeypatch.setattr(plugins, "active", lambda: plugins.load(("kelno_vault",)))   # the title rule is this vault's plugin
    note(vault, "g.md", "old\r")
    asyncio.run(tools.edit_note_impl("g.md", "# g\nbody\n"))
    assert read(vault, "g.md") == "body\r"



def test_a_rule_edit_without_a_proposal_ref_is_nudged(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "rule_file_patterns", ["_/agent/*", "CLAUDE.md"])
    note(vault, "_/agent/Rules.md", "- Rule one. [P35]\n- Rule two.\n")
    dry = asyncio.run(tools.edit_note_impl("_/agent/Rules.md", "- Rule one, reworded. [P35]", find="- Rule one. [P35]",
                                           dry_run=True))
    assert "rule_change_unlogged: these rule lines were added or changed" in dry
    assert "- line 1: - Rule one, reworded. [P35]" in dry and "ledger_add (kind proposal)" in dry
    logged = asyncio.run(tools.edit_note_impl("_/agent/Rules.md", "- Rule one, reworded. [P35, P95]",
                                              find="- Rule one. [P35]"))
    assert logged.startswith(("Updated note", "Replaced")) and "rule_change_unlogged" not in logged
    elsewhere = asyncio.run(tools.create_note_impl("Notes/Plain.md", "- Not a rule.\n"))
    assert "rule_change_unlogged" not in elsewhere
    created = asyncio.run(tools.create_note_impl("_/agent/New.md", "- A new rule.\n"))
    assert "rule_change_unlogged" in created



def test_no_rule_patterns_no_nudge(vault):
    created = asyncio.run(tools.create_note_impl("_/agent/Unset.md", "- A new rule.\n"))
    assert "rule_change_unlogged" not in created
