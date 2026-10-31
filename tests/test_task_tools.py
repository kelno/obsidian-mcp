"""`create_task`, `edit_task`, `set_task_status`: what they write and refuse.

Offline: the vault is a tmp dir, usage logging is stubbed, and the two
database touches — the area index and the immediate task re-index — are
replaced by fakes, so these tests are about the bytes written. The database
side (the rows, `list_tasks`) is in tests/integration/test_task_tools_pg.py.
"""

import json
from datetime import date, datetime, timezone

import pytest

from src.auth.session import current_user_id
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
