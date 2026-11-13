"""Task lines go through the task tools: the note writers refuse one written by hand.

The vault's rule: tasks are created and changed only with the task tools
(`create_task`, `edit_task`, `set_task_status`), which write the Tasks
plugin's format and re-index the note at once. A task line is a line the
task scanner finds (outside code fences and frontmatter) that the plugin,
under the owner's settings and global filter, reads as a task. The lines are
compared as a multiset: deleting a task line, or moving one unchanged within
the note, is allowed; adding one, or changing one (its old text gone, a new
one in), is refused.

When the Tasks settings cannot be read the check cannot tell a task from a
checkbox and lets the write through (logged). A vault with no Tasks settings
file is not checked: without the plugin there, a checkbox is just a checkbox.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from src import plugins
from src.config import settings
from src.services.fork_tasks import format as task_format, ops as task_ops
from src.services.fork_tasks.scan import scan_note
from src.services.fork_tasks.settings import TasksSettingsError, read_settings
from src.services.vault import _vault_root

logger = logging.getLogger(__name__)

#: The refusal code (listed in `src/services/refusals.py`).
CODE = "task_line_by_hand"
#: Offending lines named in the refusal; past it, "and N more".
SHOWN = 10


async def check(write: plugins.NoteWrite) -> plugins.Refusal | None:
    """A refusal when `write.new` holds a task line `write.old` did not hold as it is; else None."""
    try:
        root = _vault_root(write.user_id)
        task_settings = await asyncio.to_thread(read_settings, root, settings.tasks_plugin_settings_path)
    except TasksSettingsError:
        # debug, not warning: a caller drives this path (the D18 permit rule); list_tasks names the problem.
        logger.debug("%s: the hand-written task check was skipped (Tasks settings unreadable)", write.tool)
        return None
    if task_settings.source != "file":
        return None   # no Tasks plugin settings file: the vault may not use the plugin, a checkbox is a checkbox
    if task_settings.global_filter and task_settings.global_filter not in write.new:
        return None
    registry = task_format.StatusRegistry.from_settings(task_settings)

    def task_lines(text: str | None) -> list[tuple[int, str]]:
        if not text:
            return []
        normalised = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        return [(s.line_number, s.text) for s in scan_note(normalised).lines
                if s.kind == "task" and task_ops.parse_line(s.text, write.path, task_settings, registry) is not None]

    def offending_lines() -> list[tuple[int, str]]:
        # Compared without surrounding whitespace: trailing spaces and the
        # indentation's characters are not what a task says.
        before = Counter(line.strip() for _n, line in task_lines(write.old))
        offending = []
        for number, line in task_lines(write.new):
            if before[line.strip()] > 0:
                before[line.strip()] -= 1
            else:
                offending.append((number, line))
        return offending

    try:
        offending = await asyncio.to_thread(offending_lines)
    except task_ops.TaskOpError:
        # Settings the task tools cannot apply (an unsupported file-name date
        # format): skipped like unreadable settings; list_tasks names the problem.
        logger.debug("%s: the hand-written task check was skipped (Tasks settings not applicable)", write.tool,
                     exc_info=True)
        return None
    if not offending:
        return None
    shown = "\n".join(f"- line {n}: {line.strip()[:300]}" for n, line in offending[:SHOWN])
    more = len(offending) - SHOWN
    return plugins.Refusal(CODE, (
        f"{write.tool}: this write adds or changes task lines by hand, which the vault's rules leave to the task "
        f"tools. Nothing was written.\n{shown}" + (f"\n- and {more} more" if more > 0 else "") + "\n"
        "Create a task with create_task; change one with edit_task (its text or dates) or set_task_status (done, "
        "cancelled), which write the Tasks plugin's format. Write the rest of your text without these lines. "
        "Allowed: deleting a task line; keeping one unchanged (the same text, wherever it sits in the note, so "
        "reordering is fine); showing one in a code span or fenced code block."))


TOOL_TEXT = (
    "**Task lines go through the task tools.** A write that adds or changes a task line (a checkbox line the Tasks "
    "plugin reads as a task, with the vault's global filter such as `#task`) is refused with `task_line_by_hand`, "
    "naming the lines, and writes nothing: use `create_task`, `edit_task` or `set_task_status`. Deleting a task "
    "line, keeping one unchanged (the same text, wherever it now sits in the note, so reordering is fine), and "
    "showing one in a code span or fenced code block are allowed."
)

WRITE_FILE_TEXT = ("A write that adds or changes a task line is refused like `edit_note`'s (`task_line_by_hand`): "
                   "tasks go through the task tools.")

GUIDE = (
    "## Task lines (this server)\n\n"
    "The note tools (`edit_note`, `create_note`, `write_file`) refuse a write that adds or changes a task line "
    "(`task_line_by_hand`), naming the lines and writing nothing: add or change tasks with `create_task`, "
    "`edit_task` and `set_task_status`. Deleting a task line with them, keeping one unchanged (the same text, "
    "wherever it sits in the note), or showing one in a code span or fenced code block is allowed."
)
