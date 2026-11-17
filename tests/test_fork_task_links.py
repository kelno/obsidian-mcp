"""Task links and block ids (fork; "Task links and block ids" in docs/architecture/tasks.md).

Every result naming a task carries `open_url`; with `TASKS_BLOCK_ID` the
task writers make a task's id its block id (`^<id>`), so the link opens the
task's own line.
"""

from __future__ import annotations

import pytest

from scripts import fork_tasks_assign_ids as script
from src.mcp_server import tools
from src.services import open_note_url
from src.services.fork_tasks import format as tf
from src.services.ledger_tasks import CitingTask
from tests.test_fork_task_ids import ID, always, indexed  # noqa: F401 — fixtures
from tests.test_fork_task_tools import note, read, vault, write_settings  # noqa: F401 — `vault` is a fixture

BASE = "https://notes.example.org/open?file="


@pytest.fixture
def links(monkeypatch):
    monkeypatch.setattr(open_note_url.settings, "open_note_url", BASE + "{path}")


@pytest.fixture
def blocks(monkeypatch):
    monkeypatch.setattr(tools.settings, "tasks_block_id", True)


# --- the link -----------------------------------------------------------------------

def test_a_block_link_opens_the_line(links):
    assert open_note_url.open_note_url("A b/N.md", "x1") == BASE + "A%20b/N%23%5Ex1"
    assert open_note_url.open_note_url("N.md", "not_a_block") == BASE + "N"
    assert open_note_url.open_line_url("N.md", "- [ ] a 🆔 x1 ^x1  ") == BASE + "N%23%5Ex1"
    assert open_note_url.open_line_url("N.md", "- [ ] a 🆔 x1") == BASE + "N"
    assert open_note_url.open_line_url("N.md", "- [ ] a^x1") == BASE + "N"  # no space: not a block link


def test_no_template_no_link():
    assert open_note_url.open_line_url("N.md", "- [ ] a ^x1") is None


def test_a_citing_task_carries_its_link(links):
    item = tools._ledger_task_dict(CitingTask(ref="N.md:3", status_type="TODO", text="- [ ] a [F1] 🆔 ab ^ab", id="ab"))
    assert item["open_url"] == BASE + "N%23%5Eab"


# --- writing block ids ----------------------------------------------------------------

async def test_create_gives_the_id_as_block_id_and_links_to_it(indexed, always, blocks, links):
    note(indexed, "n.md", "")
    result = await tools.create_task_impl("call", path="n.md", id="call-1")
    assert result.error is None, result.error
    assert read(indexed, "n.md") == "- [ ] call 🆔 call-1 ^call-1\n"
    assert result.open_url == BASE + "n%23%5Ecall-1"


async def test_without_the_setting_no_block_id_and_the_note_link(indexed, always, links):
    note(indexed, "n.md", "")
    result = await tools.create_task_impl("call", path="n.md", id="call-1")
    assert read(indexed, "n.md") == "- [ ] call 🆔 call-1\n"
    assert result.open_url == BASE + "n"


async def test_an_id_with_an_underscore_gets_no_block_id(indexed, blocks):
    note(indexed, "n.md", "")
    await tools.create_task_impl("call", path="n.md", id="call_1")
    assert read(indexed, "n.md") == "- [ ] call 🆔 call_1\n"


async def test_edit_adds_one_keeps_a_foreign_block_and_follows_a_renamed_id(indexed, blocks, links):
    note(indexed, "n.md", "- [ ] a 🆔 a1\n- [ ] b 🆔 b1 ^mine\n- [ ] c 🆔 c1 ^c1\n")
    first = await tools.edit_task_impl(id="a1", due="2026-11-05")
    await tools.edit_task_impl(id="b1", due="2026-11-05")
    await tools.edit_task_impl(id="c1", new_id="c2")
    assert read(indexed, "n.md") == (
        "- [ ] a 🆔 a1 📅 2026-11-05 ^a1\n- [ ] b 🆔 b1 📅 2026-11-05 ^mine\n- [ ] c 🆔 c2 ^c2\n")
    assert first.open_url == BASE + "n%23%5Ea1"


async def test_a_ticked_recurring_task_hands_its_block_id_on_with_its_id(indexed, always, blocks, links):
    note(indexed, "n.md", "- [ ] water 🆔 water 🔁 every day 📅 2026-10-31 ^water\n")
    result = await tools.set_task_status_impl(id="water", status="done")
    assert result.error is None, result.error
    lines = read(indexed, "n.md").splitlines()
    done = next(line for line in lines if "[x]" in line)
    upcoming = next(line for line in lines if "[ ]" in line)
    new = ID.search(done).group(1)
    assert upcoming.endswith(" ^water") and done.endswith(f" ^{new}")
    assert result.open_url == BASE + f"n%23%5E{new}"
    assert result.next_occurrence_open_url == BASE + "n%23%5Ewater"


async def test_a_task_removed_by_delete_links_to_its_note(indexed, blocks, links):
    note(indexed, "n.md", "- [ ] a 🆔 a1 🏁 delete ^a1\n")
    result = await tools.set_task_status_impl(id="a1", status="done")
    assert result.removed is True and read(indexed, "n.md") == ""
    assert result.open_url == BASE + "n"


# --- the conversion -------------------------------------------------------------------

def test_the_script_gives_ids_as_block_ids_and_nothing_else(tmp_path, capsys):
    write_settings(tmp_path)
    text = ("- [ ] one 🆔 one\n- [ ] linked 🆔 two ^blk1\n- [ ] none\n- [ ] under 🆔 a_b\n"
            "- [x] done 🆔 thr ✅ 2026-10-01\n- [ ] hard 🆔 hrd  \n")
    (tmp_path / "a.md").write_text(text, encoding="utf-8")
    assert script.main([str(tmp_path), "--block-ids"]) == 0
    assert "would get a block id" in capsys.readouterr().out
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == text
    assert script.main([str(tmp_path), "--block-ids", "--apply"]) == 0
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == (
        "- [ ] one 🆔 one ^one\n- [ ] linked 🆔 two ^blk1\n- [ ] none\n- [ ] under 🆔 a_b\n"
        "- [x] done 🆔 thr ✅ 2026-10-01 ^thr\n- [ ] hard 🆔 hrd ^hrd  \n")
    # A second run finds nothing to do.
    assert script.main([str(tmp_path), "--block-ids", "--apply"]) == 0
    assert "Written: 0 note(s)" in capsys.readouterr().out


def test_with_block_id_rules():
    from dataclasses import replace

    from src.services.fork_tasks import ops

    base = tf.Task(indentation="", list_marker="-", status=None, description="a", id="x1")
    assert ops.with_block_id(base).block_link == " ^x1"
    assert ops.with_block_id(replace(base, block_link=" ^own")).block_link == " ^own"
    assert ops.with_block_id(replace(base, block_link=" ^old"), "old").block_link == " ^x1"
    assert ops.with_block_id(replace(base, id="")).block_link == ""


# --- found by the adversarial review ---------------------------------------------------

async def test_no_block_id_another_line_of_the_note_already_has(indexed, blocks, links):
    note(indexed, "n.md", "Context. ^intro\n")
    result = await tools.create_task_impl("call", path="n.md", id="intro")
    assert read(indexed, "n.md") == "Context. ^intro\n- [ ] call 🆔 intro\n"
    assert result.open_url == BASE + "n"


async def test_a_rename_to_an_id_no_block_can_hold_drops_the_old_block(indexed, blocks):
    note(indexed, "n.md", "- [ ] c 🆔 c1 ^c1\n")
    await tools.edit_task_impl(id="c1", new_id="c_2")
    assert read(indexed, "n.md") == "- [ ] c 🆔 c_2\n"


async def test_a_private_task_links_only_by_its_own_id(indexed, links, monkeypatch):
    monkeypatch.setattr(tools.private_folders.settings, "private_folder_names", ["* (private)"])
    note(indexed, "P (private)/n.md", "- [ ] lawyer 🆔 k3 ^custody\n- [ ] b 🆔 k4 ^k4\n")
    first = await tools.edit_task_impl(id="k3", due="2026-11-05")
    second = await tools.edit_task_impl(id="k4", due="2026-11-05")
    assert first.text is None and "custody" not in first.open_url
    assert second.open_url.endswith("%23%5Ek4")
    item = tools._ledger_task_dict(CitingTask(ref="P (private)/n.md:1", status_type="TODO",
                                              text="- [ ] lawyer [F1] 🆔 k3 ^custody", id="k3"))
    assert "text" not in item and "custody" not in item["open_url"]


def test_the_script_lists_shared_ids_and_ids_no_block_can_hold(tmp_path, capsys):
    write_settings(tmp_path)
    text = "- [ ] a 🆔 x1\n- [ ] b 🆔 x1\n- [ ] c 🆔 a_b\nPara ^y1\n- [ ] d 🆔 y1\n"
    (tmp_path / "a.md").write_text(text, encoding="utf-8")
    assert script.main([str(tmp_path), "--block-ids", "--apply"]) == 0
    out = capsys.readouterr().out
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == text
    assert "another task carries the same id" in out and "characters a block id cannot hold" in out
    assert "already ends with that block id" in out
