"""The task tools' structured results.

Structured, like `read_note` and `fetch_page`, because an agent chains these
calls: `list_tasks` hands back each task's `ref`, exact `text` and the note's
`content_hash`, which are the three things `edit_task` and `set_task_status`
must be given back. Every outcome rides on `_body_outcome`; an error result
carries only `error`.
"""

from __future__ import annotations

from pydantic import PrivateAttr

from src.mcp_server.read_result import _OmitNone
from src.services.tool_outcomes import BodyOutcome


class TaskItem(_OmitNone):
    ref: str                       # "path:line", 1-based
    path: str
    line: int
    text: str                      # the exact line, as on disk
    status_symbol: str
    status_name: str
    status_type: str
    description: str
    tags: list[str]
    priority: str                  # highest, high, medium, none, low, lowest
    created: str | None = None
    start: str | None = None
    scheduled: str | None = None
    #: True when `scheduled` comes from the file name (plugin setting), not the line.
    scheduled_from_filename: bool | None = None
    due: str | None = None
    done: str | None = None
    cancelled: str | None = None
    recurrence: str | None = None
    on_completion: str | None = None
    id: str | None = None
    depends_on: list[str] | None = None
    urgency: float
    #: invalid_date, unknown_on_completion, field_in_description.
    warnings: list[str] | None = None
    #: The note's current `content_hash`, to pass back as `expected_hash`.
    content_hash: str


class TaskProblem(_OmitNone):
    ref: str
    path: str
    line: int
    text: str
    reason: str


class StaleTask(_OmitNone):
    ref: str
    path: str
    line: int
    indexed_text: str
    reason: str


class TaskListResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    tasks: list[TaskItem] | None = None
    total_matched: int | None = None
    truncated: bool | None = None
    #: The Tasks query block this call answered, to paste into Obsidian.
    query: str | None = None
    #: Lines that look like tasks but may not be read as tasks by the plugin
    #: (in the `path` filter's scope), and how many there are in all.
    problems: list[TaskProblem] | None = None
    problems_total: int | None = None
    #: Matches whose line changed on disk since the index read it.
    stale: list[StaleTask] | None = None
    today: str | None = None
    settings: dict | None = None
    notes: list[str] | None = None
    error: str | None = None


class TaskWriteResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    path: str | None = None
    ref: str | None = None
    line: int | None = None
    previous_text: str | None = None
    #: The line(s) now at `line`; empty when `🏁 delete` removed the task.
    text: str | None = None
    removed: bool | None = None
    changed: bool | None = None
    created_note: bool | None = None
    content_hash: str | None = None
    indexed: bool | None = None
    index_note: str | None = None
    today: str | None = None
    error: str | None = None
