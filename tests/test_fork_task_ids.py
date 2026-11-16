"""Tasks named by their id (🆔), and `TASKS_ALWAYS_ID` (fork; see "Task ids" in docs/architecture/tasks.md)."""

from __future__ import annotations

import re

import pytest

from src.mcp_server import tools
from src.services.fork_tasks import format as tf, ids as task_ids
from src.services.fork_tasks.settings import TasksSettings
from tests.test_fork_task_tools import INDEXED_IDS, code, note, read, vault  # noqa: F401 — `vault` is a fixture

ID = re.compile(r"🆔 ([a-z0-9]{6})")


@pytest.fixture
def indexed(vault, monkeypatch):
    """The fake index's notes holding an id: every note on disk carrying it, plus INDEXED_IDS."""

    async def paths_with_id(uid, task_id, task_settings, registry):
        found = {ref.rsplit(":", 1)[0] for ref in INDEXED_IDS.get(task_id, [])}
        for path in vault.rglob("*.md"):
            if f"🆔 {task_id}" in path.read_text(encoding="utf-8"):
                found.add(path.relative_to(vault).as_posix())
        return sorted(found)

    monkeypatch.setattr(tools, "_task_paths_with_id", paths_with_id)
    return vault


@pytest.fixture
def always(monkeypatch):
    monkeypatch.setattr(tools.settings, "tasks_always_id", True)


# --- addressing by id ------------------------------------------------------------

async def test_status_by_id_finds_the_task_where_it_now_is(indexed):
    note(indexed, "n.md", "# T\n- [ ] new line above\n- [ ] call the bank 🆔 bank\n")
    result = await tools.set_task_status_impl(id="bank", status="done")
    assert result.error is None, result.error
    assert read(indexed, "n.md") == "# T\n- [ ] new line above\n- [x] call the bank 🆔 bank ✅ 2026-10-31\n"
    assert result.id == "bank" and result.line == 3


async def test_edit_by_id_takes_an_optional_hash(indexed):
    h = note(indexed, "n.md", "- [ ] a 🆔 a\n")
    result = await tools.edit_task_impl(id="a", due="2026-11-05", expected_hash="sha256:" + "1" * 64)
    assert code(result) == "stale_precondition"
    result = await tools.edit_task_impl(id="a", due="2026-11-05", expected_hash=h)
    assert result.error is None, result.error
    assert read(indexed, "n.md") == "- [ ] a 🆔 a 📅 2026-11-05\n"


async def test_unknown_id_is_refused(indexed):
    note(indexed, "n.md", "- [ ] a 🆔 a\n")
    result = await tools.set_task_status_impl(id="nope", status="done")
    assert code(result) == "task_id_not_found"


async def test_a_shared_id_is_refused_and_path_chooses(indexed):
    note(indexed, "a.md", "- [ ] copy one 🆔 dup\n")
    note(indexed, "b.md", "- [ ] copy two 🆔 dup\n")
    result = await tools.set_task_status_impl(id="dup", status="done")
    assert code(result) == "task_id_ambiguous"
    assert "a.md:1" in result.error and "b.md:1" in result.error
    result = await tools.set_task_status_impl(id="dup", path="b.md", status="done")
    assert result.error is None, result.error
    assert read(indexed, "a.md") == "- [ ] copy one 🆔 dup\n"
    assert read(indexed, "b.md").startswith("- [x] copy two 🆔 dup")


async def test_path_finds_a_note_the_index_does_not_know_yet(indexed, monkeypatch):
    async def nothing(uid, task_id, task_settings, registry):
        return []

    monkeypatch.setattr(tools, "_task_paths_with_id", nothing)
    note(indexed, "inbox.md", "- [ ] a 🆔 a\n")
    assert code(await tools.set_task_status_impl(id="a", status="done")) == "task_id_not_found"
    result = await tools.set_task_status_impl(id="a", path="inbox", status="done")
    assert result.error is None, result.error


async def test_id_and_line_together_are_refused(indexed):
    h = note(indexed, "n.md", "- [ ] a 🆔 a\n")
    result = await tools.edit_task_impl("n.md", 1, "- [ ] a 🆔 a", h, due="today", id="a")
    assert code(result) == "invalid_argument"


async def test_by_line_needs_all_four(indexed):
    note(indexed, "n.md", "- [ ] a\n")
    result = await tools.edit_task_impl("n.md", 1, due="today")
    assert code(result) == "invalid_argument"
    assert "expected_line" in result.error and "expected_hash" in result.error


async def test_without_always_id_line_addressing_is_as_before(indexed):
    h = note(indexed, "n.md", "- [ ] a 🆔 a\n")
    result = await tools.set_task_status_impl("n.md", 1, "- [ ] a 🆔 a", h, status="done")
    assert result.error is None, result.error


async def test_edit_renames_with_new_id(indexed):
    note(indexed, "n.md", "- [ ] a 🆔 old\n")
    result = await tools.edit_task_impl(id="old", new_id="fresh")
    assert result.error is None, result.error
    assert read(indexed, "n.md") == "- [ ] a 🆔 fresh\n" and result.id == "fresh"


# --- TASKS_ALWAYS_ID ---------------------------------------------------------------

async def test_create_makes_up_an_id(indexed, always):
    note(indexed, "n.md", "# T\n")
    result = await tools.create_task_impl("call the bank", path="n.md")
    assert result.error is None, result.error
    m = ID.search(read(indexed, "n.md"))
    assert m and result.id == m.group(1)


async def test_create_keeps_a_given_id(indexed, always):
    note(indexed, "n.md", "# T\n")
    result = await tools.create_task_impl("call the bank", path="n.md", id="bank")
    assert read(indexed, "n.md") == "# T\n- [ ] call the bank 🆔 bank\n" and result.id == "bank"


async def test_a_task_with_an_id_is_refused_by_line(indexed, always):
    h = note(indexed, "n.md", "- [ ] a 🆔 a\n")
    result = await tools.set_task_status_impl("n.md", 1, "- [ ] a 🆔 a", h, status="done")
    assert code(result) == "task_has_id"
    assert 'id="a"' in result.error


async def test_a_shared_id_can_still_be_reached_by_line(indexed, always):
    h = note(indexed, "n.md", "- [ ] a 🆔 dup\n- [ ] b 🆔 dup\n")
    result = await tools.edit_task_impl("n.md", 2, "- [ ] b 🆔 dup", h, new_id="b")
    assert result.error is None, result.error
    assert read(indexed, "n.md") == "- [ ] a 🆔 dup\n- [ ] b 🆔 b\n"


async def test_a_task_without_an_id_gets_one_when_written_by_line(indexed, always):
    h = note(indexed, "n.md", "- [ ] a\n")
    result = await tools.set_task_status_impl("n.md", 1, "- [ ] a", h, status="done")
    assert result.error is None, result.error
    text = read(indexed, "n.md")
    assert re.fullmatch(r"- \[x\] a 🆔 [a-z0-9]{6} ✅ 2026-10-31\n", text), text
    assert result.id == ID.search(text).group(1)


async def test_edit_by_line_gives_an_id_too(indexed, always):
    h = note(indexed, "n.md", "- [ ] a\n")
    result = await tools.edit_task_impl("n.md", 1, "- [ ] a", h, due="2026-11-05")
    assert result.error is None, result.error
    assert re.fullmatch(r"- \[ \] a 🆔 [a-z0-9]{6} 📅 2026-11-05\n", read(indexed, "n.md"))


async def test_clearing_the_id_is_refused(indexed, always):
    note(indexed, "n.md", "- [ ] a 🆔 a\n")
    result = await tools.edit_task_impl(id="a", clear=["id"])
    assert code(result) == "invalid_argument"


async def test_a_ticked_recurring_task_hands_its_id_to_its_next_occurrence(indexed, always):
    note(indexed, "n.md", "- [ ] water 🆔 water 🔁 every day 📅 2026-10-31\n")
    result = await tools.set_task_status_impl(id="water", status="done")
    assert result.error is None, result.error
    lines = read(indexed, "n.md").splitlines()
    assert len(lines) == 2
    done = next(line for line in lines if "[x]" in line)
    upcoming = next(line for line in lines if "[ ]" in line)
    assert "🆔 water" in upcoming
    m = ID.search(done)
    assert m and m.group(1) != "water"
    assert result.next_occurrence_id == "water" and result.id == m.group(1)
    assert any("moved to the next occurrence" in n for n in result.notes)
    # The id now names the task still to do.
    again = await tools.set_task_status_impl(id="water", status="done")
    assert again.error is None and again.changed is True, again


# --- the id maker and lookup ---------------------------------------------------------

def test_new_id_avoids_taken_ids_and_digits_only(monkeypatch):
    picks = iter("111111" "aaaaaa" "bbbbbb")
    monkeypatch.setattr(task_ids.secrets, "choice", lambda alphabet: next(picks))
    assert task_ids.new_id({"aaaaaa"}) == "bbbbbb"


def test_lines_with_id_reads_only_tasks():
    settings = TasksSettings.plugin_defaults()
    registry = tf.StatusRegistry.from_settings(settings)
    text = "- [ ] a 🆔 x\n`- [ ] code 🆔 x`\n```\n- [ ] fenced 🆔 x\n```\n- [ ] b 🆔 xy\n"
    assert task_ids.lines_with_id(text, "n.md", "x", settings, registry) == [(1, "- [ ] a 🆔 x")]


# --- review findings (2026-11-16) ----------------------------------------------------

async def test_a_private_task_found_by_id_alone_comes_without_its_text(indexed, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])
    note(indexed, "Health (private)/n.md", "- [ ] secret appointment 🆔 sec\n")
    result = await tools.set_task_status_impl(id="sec", status="done")
    assert result.error is None, result.error
    assert result.text is None and result.previous_text is None and result.id == "sec"
    named = await tools.edit_task_impl(id="sec", path="Health (private)/n.md", due="2026-11-05")
    assert "secret appointment" in named.text


async def test_a_done_recurring_occurrence_reached_by_id_says_where_the_next_one_is(indexed):
    note(indexed, "n.md", "- [x] water 🆔 water 🔁 every day 📅 2026-10-30 ✅ 2026-10-30\n")
    result = await tools.set_task_status_impl(id="water", status="done")
    assert result.changed is False
    assert any("next occurrence" in n for n in result.notes)


async def test_an_unreadable_note_holding_the_id_makes_it_ambiguous(indexed, monkeypatch):
    note(indexed, "a.md", "- [ ] a 🆔 x1\n")
    (indexed / "b.md").write_bytes(b"\xff\xfe not utf-8 \xf0\x9f\x86\x94 x1\n")

    async def both(uid, task_id, task_settings, registry):
        return ["a.md", "b.md"]

    monkeypatch.setattr(tools, "_task_paths_with_id", both)
    result = await tools.set_task_status_impl(id="x1", status="done")
    assert code(result) == "task_id_ambiguous" and "b.md" in result.error


async def test_a_read_only_caller_is_refused_before_any_lookup(indexed, monkeypatch):
    from src.mcp_server.auth import current_permission

    async def boom(*args, **kwargs):
        raise AssertionError("looked up")

    monkeypatch.setattr(tools, "_task_paths_with_id", boom)
    token = current_permission.set("read")
    try:
        result = await tools.set_task_status_impl(id="x", status="done")
    finally:
        current_permission.reset(token)
    assert code(result) == "permission_denied"
