"""The task write tools' speed work: same writes, less reading (see "Speed" in docs/architecture/tasks.md).

- A new id is checked against the index by the id drawn, not by listing every id.
- The id checks look other notes' tasks up by the ids a write touches.
- One call reads the Tasks settings once and scans each text once.
"""

from __future__ import annotations

import re

import pytest

from src.mcp_server import tools
from src.services.fork_tasks import ids as task_ids, scan as task_scan
from tests.test_fork_task_tools import INDEXED_DEPS, INDEXED_IDS, code, note, read, vault  # noqa: F401 — `vault` is a fixture

ID = re.compile(r"🆔 ([a-z0-9]{6})")


@pytest.fixture
def always(monkeypatch):
    monkeypatch.setattr(tools.settings, "tasks_always_id", True)


def picks(monkeypatch, *ids):
    """`secrets.choice` draws these ids, in turn."""
    chars = iter("".join(ids))
    monkeypatch.setattr(task_ids.secrets, "choice", lambda alphabet: next(chars))


@pytest.fixture
def lookups(monkeypatch):
    """Every `needles` the fake index is asked with (None: the whole list)."""
    calls: list = []
    fake = tools._task_ids_in_index

    async def recording(uid, exclude_path, task_settings, registry, needles=None):
        calls.append(None if needles is None else set(needles))
        return await fake(uid, exclude_path, task_settings, registry, needles)

    monkeypatch.setattr(tools, "_task_ids_in_index", recording)
    return calls


# --- making an id ---------------------------------------------------------------------

async def test_a_made_up_id_skips_one_another_note_holds(vault, always, monkeypatch, lookups):
    INDEXED_IDS["aaaaaa"] = ["o.md:1"]
    note(vault, "n.md", "# T\n")
    picks(monkeypatch, "aaaaaa", "bbbbbb")
    result = await tools.create_task_impl("call the bank", path="n.md")
    assert result.error is None, result.error
    assert read(vault, "n.md") == "# T\n- [ ] call the bank 🆔 bbbbbb\n" and result.id == "bbbbbb"
    # Each id drawn is looked up on its own, and the id check asks for the new id only.
    assert lookups == [{"aaaaaa"}, {"bbbbbb"}, {"bbbbbb"}]


async def test_a_made_up_id_skips_one_the_note_holds(vault, always, monkeypatch):
    note(vault, "n.md", "- [ ] a 🆔 cccccc\n")
    picks(monkeypatch, "cccccc", "dddddd")
    result = await tools.create_task_impl("b", path="n.md")
    assert read(vault, "n.md") == "- [ ] a 🆔 cccccc\n- [ ] b 🆔 dddddd\n" and result.id == "dddddd"


async def test_a_ticked_recurring_task_draws_distinct_ids(vault, always, monkeypatch):
    h = note(vault, "n.md", "- [ ] water 🔁 every day 📅 2026-10-31\n")
    # The next occurrence takes the first id; the done line may not take it again.
    picks(monkeypatch, "eeeeee", "eeeeee", "ffffff")
    result = await tools.set_task_status_impl("n.md", 1, "- [ ] water 🔁 every day 📅 2026-10-31", h,
                                              status="done")
    assert result.error is None, result.error
    assert read(vault, "n.md") == ("- [ ] water 🆔 eeeeee 🔁 every day 📅 2026-11-01\n"
                                   "- [x] water 🆔 ffffff 🔁 every day 📅 2026-10-31 ✅ 2026-10-31\n")


async def test_a_tick_needing_no_new_id_never_reads_the_index(vault, always, monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("the index was read")

    monkeypatch.setattr(tools, "_task_ids_in_index", boom)

    async def paths(uid, task_id, task_settings, registry):
        return ["n.md"]

    monkeypatch.setattr(tools, "_task_paths_with_id", paths)
    note(vault, "n.md", "- [ ] a 🆔 a\n")
    result = await tools.set_task_status_impl(id="a", status="done")
    assert result.error is None, result.error
    assert read(vault, "n.md") == "- [x] a 🆔 a ✅ 2026-10-31\n"


# --- the id checks ---------------------------------------------------------------------

async def test_a_rename_another_notes_open_task_waits_for_is_refused_by_the_ids_it_touches(vault, lookups):
    INDEXED_DEPS.append(("o.md:3", "", ("old",)))
    h = note(vault, "n.md", "- [ ] a 🆔 old\n")
    result = await tools.edit_task_impl("n.md", 1, "- [ ] a 🆔 old", h, new_id="fresh")
    assert code(result) == "invalid_argument" and "o.md:3" in result.error
    assert lookups == [{"old", "fresh"}]


async def test_a_new_dependency_on_a_new_id_still_walks_the_whole_list(vault, lookups):
    """A cycle may run through any task, so the walk keeps the whole list."""
    INDEXED_IDS["b"] = ["o.md:1"]
    INDEXED_DEPS.append(("o.md:1", "b", ("a",)))
    note(vault, "n.md", "# T\n")
    result = await tools.create_task_impl("a", path="n.md", id="a", depends_on=["b"])
    assert code(result) == "invalid_argument" and "o.md:1" in result.error
    assert lookups == [None]


async def test_a_new_dependency_without_a_new_id_looks_up_that_id(vault, lookups):
    INDEXED_IDS["b"] = ["o.md:1"]
    h = note(vault, "n.md", "- [ ] a\n")
    result = await tools.edit_task_impl("n.md", 1, "- [ ] a", h, depends_on=["b"])
    assert result.error is None, result.error
    assert lookups == [{"b"}]


# --- one call, one settings read, one scan per text ------------------------------------

async def test_one_call_reads_the_settings_once_and_scans_each_text_once(vault, always, monkeypatch):
    reads, scans = [], []
    read_settings, scan = tools._read_task_settings, task_scan._scan_note

    def counting_read(*args):
        reads.append(args)
        return read_settings(*args)

    def counting_scan(text):
        scans.append(text)
        return scan(text)

    monkeypatch.setattr(tools, "_read_task_settings", counting_read)
    monkeypatch.setattr(task_scan, "_scan_note", counting_scan)

    async def paths(uid, task_id, task_settings, registry):
        return ["n.md"]

    monkeypatch.setattr(tools, "_task_paths_with_id", paths)
    note(vault, "n.md", "---\nk: v\n---\n# T\n- [ ] water 🆔 water 🔁 every day 📅 2026-10-31\n- [ ] other\n")
    result = await tools.set_task_status_impl(id="water", status="done")
    assert result.error is None, result.error
    assert read(vault, "n.md").splitlines()[4:] == [
        "- [ ] water 🆔 water 🔁 every day 📅 2026-11-01",
        f"- [x] water 🆔 {result.id} 🔁 every day 📅 2026-10-31 ✅ 2026-10-31",
        "- [ ] other",
    ]
    assert len(reads) == 1
    assert len(scans) == len(set(scans)) and len(scans) <= 3, scans  # the read, the new text, the stamped one


def test_outside_a_call_every_scan_is_made():
    count = []
    real = task_scan._scan_note

    def counting(text):
        count.append(text)
        return real(text)

    task_scan._scan_note, saved = counting, task_scan._scan_note
    try:
        task_scan.scan_note("- [ ] a\n")
        task_scan.scan_note("- [ ] a\n")
        with task_scan.one_call():
            first = task_scan.scan_note("- [ ] a\n")
            assert task_scan.scan_note("- [ ] a\n") is first
        task_scan.scan_note("- [ ] a\n")
    finally:
        task_scan._scan_note = saved
    assert len(count) == 4


# --- the parse cache (list_tasks, 2026-11-19) -----------------------------------------

from datetime import date  # noqa: E402

from src.services.fork_tasks import format as task_format, ops as task_ops  # noqa: E402
from src.services.fork_tasks.settings import StatusConfig, TasksSettings  # noqa: E402

PARSE_LINES = [
    "- [ ] plain task",
    "- [x] done task ✅ 2026-11-01",
    "- [/] in progress #area/x ⏳ 2026-11-20 📅 2026-11-25 🆔 abc123",
    "- [?] odd status 🔁 every week",
    "  * [ ] indented ⛔ abc123,def456 🔼",
    "- [ ] #task only with the global filter",
    "- [ ] bad date 📅 2026-13-40",
    "not a task",
    "- [ ]",
    "",
]
CUSTOM = (StatusConfig("?", "Question", " ", True, "IN_PROGRESS"),
          StatusConfig(" ", "Todo", "x", True, "TODO"),
          StatusConfig("x", "Done", " ", True, "DONE"))
SETTINGS = [
    TasksSettings(),
    TasksSettings(statuses=CUSTOM),
    TasksSettings(global_filter="#task"),
    TasksSettings(use_filename_as_scheduled_date=True, filename_as_scheduled_date_format="YYYY-MM-DD"),
]
PATHS = ["Areas/x.md", "Daily/2026-11-19.md", "Daily/2026-11-20.md"]


def test_a_cached_parse_is_the_parse_for_every_line_settings_and_path():
    """Twice over (the second answered from the cache), every combination
    gives exactly what `parse_task_line` gives: the statuses, the global
    filter and the file-name date are all in the cache's key."""
    for _ in range(2):
        for s in SETTINGS:
            registry = task_format.StatusRegistry.from_settings(s)
            for path in PATHS:
                for line in PARSE_LINES:
                    expected = task_format.parse_task_line(
                        line, registry, global_filter=s.global_filter,
                        fallback_date=task_ops.fallback_date(path, s))
                    got = task_ops.parse_line(line, path, s, registry)
                    assert got == expected, (line, path, s)
                    if got is not None:
                        assert got.warnings == expected.warnings  # compare=False on the field
                        assert got.status == expected.status


def test_the_cache_tells_registries_and_file_name_dates_apart():
    line = "- [?] odd status"
    plain = TasksSettings()
    custom = TasksSettings(statuses=CUSTOM)
    a = task_ops.parse_line(line, "x.md", plain, task_format.StatusRegistry.from_settings(plain))
    b = task_ops.parse_line(line, "x.md", custom, task_format.StatusRegistry.from_settings(custom))
    assert a.status.name == "Unknown" and b.status.name == "Question"
    dated = TasksSettings(use_filename_as_scheduled_date=True, filename_as_scheduled_date_format="YYYY-MM-DD")
    registry = task_format.StatusRegistry.from_settings(dated)
    one = task_ops.parse_line("- [ ] t", "2026-11-19.md", dated, registry)
    two = task_ops.parse_line("- [ ] t", "2026-11-20.md", dated, registry)
    assert one.scheduled.value == date(2026, 11, 19) and two.scheduled.value == date(2026, 11, 20)


def test_a_settings_error_is_raised_on_every_call_never_cached():
    bad = TasksSettings(use_filename_as_scheduled_date=True, filename_as_scheduled_date_format="DD.MM.YYYY")
    registry = task_format.StatusRegistry.from_settings(bad)
    for _ in range(2):
        with pytest.raises(task_ops.TaskOpError):
            task_ops.parse_line("- [ ] t", "2026-11-19.md", bad, registry)
