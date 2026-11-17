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
    #: The task's 🆔: the name to change it by (`edit_task(id=…)`) or cite it elsewhere.
    id: str | None = None
    ref: str                       # "path:line", 1-based: where it is now
    path: str
    line: int
    #: With `OPEN_NOTE_URL` set: the link that opens the task in Obsidian, on
    #: its own line when the line ends with a block id (`^…`), else the note.
    open_url: str | None = None
    #: True for a task in a private folder listed by a query not scoped to
    #: that folder: only `id`, `ref`, `path`, `line` and `status_type` are given.
    private: bool | None = None
    text: str | None = None        # the exact line, as on disk
    status_symbol: str | None = None
    status_name: str | None = None
    status_type: str
    description: str | None = None
    tags: list[str] | None = None
    priority: str | None = None    # highest, high, medium, none, low, lowest
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
    depends_on: list[str] | None = None
    urgency: float | None = None
    #: invalid_date, unknown_on_completion, field_in_description.
    warnings: list[str] | None = None
    #: The note's current `content_hash`, to pass back as `expected_hash`.
    content_hash: str | None = None


class TaskProblem(_OmitNone):
    ref: str
    path: str
    line: int
    #: The line as written; left out for a note in a private folder, as a task's text is.
    text: str | None = None
    reason: str


class StaleTask(_OmitNone):
    ref: str
    path: str
    line: int
    #: Left out for a note in a private folder, as a task's text is.
    indexed_text: str | None = None
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

    #: The task's 🆔, the way to name it in the next call (`id=`); for a
    #: recurring task ticked done, the done line's (the new occurrence's is
    #: `next_occurrence_id`; with `TASKS_ALWAYS_ID`, the id the call named).
    id: str | None = None
    path: str | None = None
    ref: str | None = None
    line: int | None = None
    #: With `OPEN_NOTE_URL` set: the link that opens the task in Obsidian, on
    #: its own line when the line ends with a block id (`^…`), else the note.
    open_url: str | None = None
    previous_text: str | None = None
    #: The line(s) now at `line`; empty when `🏁 delete` removed the task.
    #: For a recurring task ticked done, `line`, `ref` and `text` are the
    #: ticked task's own; its next occurrence is `next_occurrence`.
    text: str | None = None
    #: True when `🏁 delete` removed the task (for a recurring task, its
    #: completed instance: its next occurrence then stands in its place).
    removed: bool | None = None
    #: A recurring task ticked done: the new occurrence's line, as written,
    #: and its 1-based line number, placed as the Tasks plugin's settings say.
    next_occurrence: str | None = None
    next_occurrence_line: int | None = None
    next_occurrence_id: str | None = None
    next_occurrence_open_url: str | None = None
    changed: bool | None = None
    created_note: bool | None = None
    content_hash: str | None = None
    indexed: bool | None = None
    index_note: str | None = None
    today: str | None = None
    #: Agent-ledger entries the line cites (`[F12]`, `[Q14, P3]`), any kind,
    #: as they stand now: ref, kind, title, status, a quirk's certainty, a
    #: case's outcome, the day it last changed, a proposal's current rule
    #: cases (`cases`: total, outcomes, refs, and the same split into `before` and `since` the
    #: rule was adopted), and `tasks`, the state of every task citing it
    #: (open, done or cancelled). `status: unknown` for a ref with no entry.
    ledger_refs: list[dict] | None = None
    #: Plain-language remarks, such as a bare `F12` that links nothing.
    notes: list[str] | None = None
    error: str | None = None
