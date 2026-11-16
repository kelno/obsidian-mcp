"""Task ids (🆔) as the address of a task: making one, finding one in a note.

Pure. The tools name a task by its id rather than by a line number, which
goes stale whenever a line above it moves; see "Task ids" in
docs/architecture/tasks.md.
"""

from __future__ import annotations

import secrets

from src.services.fork_tasks.ops import TaskOpError, parse_line
from src.services.fork_tasks.scan import scan_note
from src.services.fork_tasks.settings import TasksSettings
from src.services.fork_tasks import format as tf

#: The characters and length of a made-up id: six lowercase letters and
#: digits, like the ids the Tasks plugin's dialog generates.
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
ID_LENGTH = 6
#: Attempts before giving up; with 36**6 ids this is never reached in practice.
_MAX_ATTEMPTS = 100


def new_id(taken: set[str]) -> str:
    """A random id that is not in `taken` (compared case-sensitively, as the plugin does)."""
    for _ in range(_MAX_ATTEMPTS):
        candidate = "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))
        if candidate not in taken and not candidate.isdigit():
            return candidate
    raise TaskOpError("concurrent_write", "no free task id could be made. Nothing was written; call again.")


def lines_with_id(text: str, path: str, task_id: str, settings: TasksSettings,
                  registry: tf.StatusRegistry) -> list[tuple[int, str]]:
    """`[(line_number, line), …]`: the task lines of `text` whose 🆔 is `task_id`."""
    out = []
    for scanned in scan_note(text).lines:
        if scanned.kind != "task" or "🆔" not in scanned.text:
            continue
        task = parse_line(scanned.text, path, settings, registry)
        if task is not None and task.id == task_id:
            out.append((scanned.line_number, scanned.text))
    return out
