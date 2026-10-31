"""The Tasks plugin's emoji task format, ported from its source.

The task tools must give the same answer as the Obsidian Tasks plugin to the
same question and write the lines the plugin itself would write. So this
module is a port of the plugin's own code, not a reading of its documentation:
version **8.6.0** (the reference vault's installed version), function by
function, each named where it is mirrored. Pure: no I/O, no clock — "today"
is always passed in by the caller, computed in `TASKS_TIMEZONE`.

JavaScript semantics that differ from Python's and are reproduced on purpose:

- `\\s` and `String.trim()` use ECMAScript's whitespace set (`JS_WHITESPACE`),
  not Python's `str.isspace()`.
- `.` excludes `\\n`, `\\r`, U+2028 and U+2029.
- `\\d` in the field patterns is ASCII only.
- `line.match(re)` / `line.replace(re, '')` act on the first match only.

Recurrence is deliberately not ported: a `🔁` rule is read and kept as text,
and every status change on a recurring task raises `RecurrenceUnsupported`
(the owner's decision for the first version: a task must never be ticked
without its next occurrence).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date

from src.services.task_settings import StatusConfig, TasksSettings

# ECMAScript WhiteSpace + LineTerminator: what `\s` and `trim()` mean in JS.
JS_WHITESPACE = (
    "\t\n\u000b\u000c\r   "
    "           "
    "    　﻿"
)
_WS = "[" + re.escape(JS_WHITESPACE) + "]"
_DOT = "[^\n\r  ]"


def js_trim(text: str) -> str:
    return text.strip(JS_WHITESPACE)


# --- TaskRegularExpressions.ts -----------------------------------------------

INDENTATION = "([" + re.escape(JS_WHITESPACE) + ">]*)"
LIST_MARKER = r"([-*+]|[0-9]+[.)])"
#: `taskRegex`: indentation, list marker, one or more spaces (not tabs), a
#: one-character checkbox, then the rest of the line.
TASK_RE = re.compile("^" + INDENTATION + LIST_MARKER + " +" + r"\[(" + _DOT + r")\]" + " *(" + _DOT + "*)")
BLOCK_LINK_RE = re.compile(r" \^[a-zA-Z0-9-]+\Z")
HASHTAG_RE = re.compile("(^|" + _WS + ")#[^ !@#$%^&*(),.?\":{}|<>]+")
HASHTAG_FROM_END_RE = re.compile(HASHTAG_RE.pattern + r"\Z")


# --- DefaultTaskSerializer.ts ------------------------------------------------

def _field_re(symbols: str, value: str) -> re.Pattern:
    # `fieldRegex`: the symbol, an optional U+FE0F, ` *value`, anchored at the end.
    source = symbols + "️?"
    if value:
        source += " *" + value
    return re.compile(source + r"\Z")


def _date_re(symbols: str) -> re.Pattern:
    return _field_re(symbols, "([0-9]{4}-[0-9]{2}-[0-9]{2})")


_TASK_ID = "[a-zA-Z0-9-_]+"
_TASK_ID_SEQUENCE = _TASK_ID + "( *, *" + _TASK_ID + " *)*"

PRIORITY_RE = _field_re("(🔺|⏫|🔼|🔽|⏬)", "")
START_RE = _date_re("🛫")
CREATED_RE = _date_re("➕")
SCHEDULED_RE = _date_re("(?:⏳|⌛)")
DUE_RE = _date_re("(?:📅|📆|🗓)")
DONE_RE = _date_re("✅")
CANCELLED_RE = _date_re("❌")
RECURRENCE_RE = _field_re("🔁", "([a-zA-Z0-9, !]+)")
ON_COMPLETION_RE = _field_re("🏁", "([a-zA-Z]+)")
DEPENDS_ON_RE = _field_re("⛔", "(" + _TASK_ID_SEQUENCE + ")")
ID_RE = _field_re("🆔", "(" + _TASK_ID + ")")
TASK_ID_RE = re.compile(_TASK_ID + r"\Z")

#: Every symbol the format gives meaning to. One left inside a description is
#: text to the plugin, which is worth telling the caller (`field_in_description`).
FIELD_SYMBOLS = ("🔺", "⏫", "🔼", "🔽", "⏬", "🛫", "➕", "⏳", "⌛", "📅", "📆", "🗓", "✅", "❌", "🔁", "🏁", "🆔", "⛔")


class Priority:
    """`Priority.ts`: the enum's string values, which are also its sort order."""

    HIGHEST = "0"
    HIGH = "1"
    MEDIUM = "2"
    NONE = "3"
    LOW = "4"
    LOWEST = "5"


PRIORITY_SYMBOLS = {
    Priority.HIGHEST: "🔺",
    Priority.HIGH: "⏫",
    Priority.MEDIUM: "🔼",
    Priority.LOW: "🔽",
    Priority.LOWEST: "⏬",
}
_SYMBOL_PRIORITY = {v: k for k, v in PRIORITY_SYMBOLS.items()}
PRIORITY_NAMES = {
    "highest": Priority.HIGHEST,
    "high": Priority.HIGH,
    "medium": Priority.MEDIUM,
    "none": Priority.NONE,
    "low": Priority.LOW,
    "lowest": Priority.LOWEST,
}
PRIORITY_NAME_OF = {v: k for k, v in PRIORITY_NAMES.items()}

ON_COMPLETION_VALUES = ("", "keep", "delete")


# --- statuses ----------------------------------------------------------------

STATUS_TYPES = ("TODO", "DONE", "IN_PROGRESS", "ON_HOLD", "CANCELLED", "NON_TASK")
#: `Status.typeGroupText` prefixes: the status-type sort order.
STATUS_TYPE_GROUP = {"IN_PROGRESS": 1, "TODO": 2, "ON_HOLD": 3, "DONE": 4, "CANCELLED": 5, "NON_TASK": 6, "EMPTY": 7}
_DONE_TYPES = ("DONE", "CANCELLED", "NON_TASK")


@dataclass(frozen=True)
class Status:
    symbol: str
    name: str
    next_symbol: str
    available_as_command: bool
    type: str

    def identical_to(self, other: "Status") -> bool:
        # `Status.identicalTo` compares these five fields.
        return self == other


class StatusRegistry:
    """`StatusRegistry.ts`, populated from the owner's status settings."""

    def __init__(self, statuses: list[Status]):
        self._statuses = statuses

    @classmethod
    def from_settings(cls, settings: TasksSettings) -> "StatusRegistry":
        registered: list[Status] = []
        seen: set[str] = set()
        for config in settings.statuses:
            # `add` keeps the first status registered for a symbol; the empty
            # symbol is the plugin's EMPTY status, never a real one.
            if config.symbol == "" or config.symbol in seen:
                continue
            seen.add(config.symbol)
            registered.append(_from_config(config))
        return cls(registered)

    @property
    def registered(self) -> tuple[Status, ...]:
        return tuple(self._statuses)

    def by_symbol(self, symbol: str) -> Status | None:
        for status in self._statuses:
            if status.symbol == symbol:
                return status
        return None

    def by_symbol_or_create(self, symbol: str) -> Status:
        # `bySymbolOrCreate` → `Status.createUnknownStatus`: type TODO.
        return self.by_symbol(symbol) or Status(symbol, "Unknown", "x", False, "TODO")

    def next_status(self, status: Status) -> Status:
        # `getNextStatusOrCreate`.
        found = self.by_symbol(status.next_symbol) if status.next_symbol else None
        return found or Status(status.next_symbol, "Unknown", "x", False, "TODO")

    def first_of_type(self, status_type: str) -> Status | None:
        for status in self._statuses:
            if status.type == status_type:
                return status
        return None


def _from_config(config: StatusConfig) -> Status:
    # `Status.getTypeFromStatusTypeString`: an unknown type string is TODO.
    kind = config.type if config.type in STATUS_TYPES else "TODO"
    return Status(config.symbol, config.name, config.next_symbol, config.available_as_command, kind)


# --- the task ----------------------------------------------------------------

@dataclass(frozen=True)
class TaskDate:
    """A date field as written. `value` is None when it is not a real date.

    The plugin parses with `moment(text, 'YYYY-MM-DD')`, which gives an
    *invalid* moment for `2026-02-30`; the field is still present (`has due
    date` is true) but compares as invalid.
    """

    text: str
    value: date | None

    @classmethod
    def parse(cls, text: str) -> "TaskDate":
        try:
            return cls(text, date.fromisoformat(text))
        except ValueError:
            return cls(text, None)

    @classmethod
    def of(cls, value: date) -> "TaskDate":
        return cls(value.isoformat(), value)


DATE_FIELDS = ("created", "start", "scheduled", "due", "cancelled", "done")
DATE_SYMBOLS = {"created": "➕", "start": "🛫", "scheduled": "⏳", "due": "📅", "cancelled": "❌", "done": "✅"}


@dataclass(frozen=True)
class Task:
    indentation: str
    list_marker: str
    status: Status
    description: str
    priority: str = Priority.NONE
    created: TaskDate | None = None
    start: TaskDate | None = None
    scheduled: TaskDate | None = None
    due: TaskDate | None = None
    done: TaskDate | None = None
    cancelled: TaskDate | None = None
    recurrence: str | None = None
    on_completion: str = ""
    id: str = ""
    depends_on: tuple[str, ...] = ()
    block_link: str = ""
    tags: tuple[str, ...] = ()
    hard_break: str = ""
    #: The scheduled date came from the file name (`useFilenameAsScheduledDate`)
    #: and is never written to the line.
    scheduled_inferred: bool = False
    #: Things about the line the caller should know. `invalid_date`,
    #: `unknown_on_completion` and `duplicate_field` mark text a rewrite by the
    #: plugin would lose or mangle, so the write tools refuse such a line.
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def is_done(self) -> bool:
        # `Task.isDone`: DONE, CANCELLED and NON_TASK count as done.
        return self.status.type in _DONE_TYPES

    def happens_dates(self) -> tuple[TaskDate | None, ...]:
        return (self.start, self.scheduled, self.due)


def extract_hashtags(description: str) -> tuple[str, ...]:
    # `Task.extractHashtags`: every match, trimmed.
    return tuple(js_trim(m.group(0)) for m in HASHTAG_RE.finditer(description))


@dataclass
class _State:
    line: str
    matched: bool = False


def _extract(state: _State, pattern: re.Pattern) -> re.Match | None:
    # `extractField`: first match, removed, the remainder trimmed.
    match = pattern.search(state.line)
    if match is None:
        return None
    state.line = js_trim(state.line[:match.start()] + state.line[match.end():])
    state.matched = True
    return match


def deserialize(body: str) -> dict:
    """`DefaultTaskSerializer.deserialize`: the fields of a task body."""
    state = _State(body)
    out: dict = {
        "priority": Priority.NONE, "created": None, "start": None, "scheduled": None,
        "due": None, "done": None, "cancelled": None, "recurrence": None,
        "on_completion": "", "id": "", "depends_on": (),
    }
    warnings: list[str] = []
    seen: set[str] = set()

    def once(name: str) -> None:
        # A second value for the same field overwrites the first (the plugin
        # does the same), so a rewrite would silently drop one of them.
        if name in seen and "duplicate_field" not in warnings:
            warnings.append("duplicate_field")
        seen.add(name)

    trailing_tags = ""
    runs = 0
    while True:
        state.matched = False
        if m := _extract(state, PRIORITY_RE):
            once("priority")
            out["priority"] = _SYMBOL_PRIORITY.get(m.group(1), Priority.NONE)
        for name, pattern in (("done", DONE_RE), ("cancelled", CANCELLED_RE), ("due", DUE_RE),
                              ("scheduled", SCHEDULED_RE), ("start", START_RE), ("created", CREATED_RE)):
            if m := _extract(state, pattern):
                once(name)
                out[name] = TaskDate.parse(m.group(1))
        if m := _extract(state, RECURRENCE_RE):
            once("recurrence")
            out["recurrence"] = js_trim(m.group(1))
        if m := _extract(state, ON_COMPLETION_RE):
            once("on_completion")
            value = js_trim(m.group(1)).lower()
            # `parseOnCompletionValue`: anything else is Ignore — and the text
            # has already been removed from the line.
            out["on_completion"] = value if value in ("keep", "delete") else ""
            if value not in ("keep", "delete"):
                warnings.append("unknown_on_completion")
        if m := _extract(state, HASHTAG_FROM_END_RE):
            tag = js_trim(m.group(0))
            trailing_tags = f"{tag} {trailing_tags}" if trailing_tags else tag
        if m := _extract(state, ID_RE):
            once("id")
            out["id"] = js_trim(m.group(1))
        if m := _extract(state, DEPENDS_ON_RE):
            once("depends_on")
            out["depends_on"] = tuple(p for p in m.group(1).replace(" ", "").split(",") if p)
        runs += 1
        if not (state.matched and runs <= 20):
            break
    if trailing_tags:
        state.line += (" " if state.line else "") + trailing_tags
    out["description"] = state.line
    out["tags"] = extract_hashtags(state.line)
    if any(d is not None and d.value is None for d in (out[k] for k in DATE_FIELDS)):
        warnings.append("invalid_date")
    if any(symbol in state.line for symbol in FIELD_SYMBOLS):
        warnings.append("field_in_description")
    out["warnings"] = tuple(warnings)
    return out


def _hard_break(line: str) -> str:
    # `ListItem.getMarkdownHardBreak`: two or more trailing spaces, kept.
    if not line.endswith("  "):
        return ""
    return line[len(line.rstrip(" ")):]


def parse_task_line(
    line: str,
    registry: StatusRegistry,
    *,
    global_filter: str = "",
    fallback_date: date | None = None,
) -> Task | None:
    """`Task.fromLine` (with `parseTaskSignifiers`), or None for a non-task.

    `line` is one line without its terminator. Whether the line is a list item
    at all (not in code, frontmatter or an HTML block) is `task_scan`'s
    question; this answers only what the plugin answers about a line Obsidian
    already listed as a task.
    """
    match = TASK_RE.match(line)
    if match is None:
        return None
    indentation, marker, symbol, rest = match.groups()
    body = js_trim(rest)
    block_link = ""
    if (bl := BLOCK_LINK_RE.search(body)) is not None:
        block_link = bl.group(0)
        body = js_trim(body[:bl.start()])
    if global_filter not in body:
        return None
    details = deserialize(body)
    scheduled_inferred = False
    if fallback_date is not None and details["start"] is None and details["scheduled"] is None and details["due"] is None:
        details["scheduled"] = TaskDate.of(fallback_date)
        scheduled_inferred = True
    tags = tuple(t for t in details.pop("tags") if t != global_filter)
    warnings = details.pop("warnings")
    return Task(
        indentation=indentation,
        list_marker=marker,
        status=registry.by_symbol_or_create(symbol),
        block_link=block_link,
        tags=tags,
        hard_break=_hard_break(line),
        scheduled_inferred=scheduled_inferred,
        warnings=warnings,
        **details,
    )


def serialize(task: Task) -> str:
    """`DefaultTaskSerializer.serialize`, in `TaskLayoutComponent` order."""
    out = task.description
    if task.id:
        out += f" 🆔 {task.id}"
    if task.depends_on:
        out += f" ⛔ {','.join(task.depends_on)}"
    if task.priority in PRIORITY_SYMBOLS:
        out += " " + PRIORITY_SYMBOLS[task.priority]
    if task.recurrence:
        out += f" 🔁 {task.recurrence}"
    if task.on_completion:
        out += f" 🏁 {task.on_completion}"
    for name in DATE_FIELDS:
        value = getattr(task, name)
        if value is None or (name == "scheduled" and task.scheduled_inferred):
            continue
        out += f" {DATE_SYMBOLS[name]} {value.text}"
    return out + task.block_link


def to_file_line(task: Task) -> str:
    """`Task.toFileLineString`."""
    return f"{task.indentation}{task.list_marker} [{task.status.symbol}] {serialize(task)}{task.hard_break}"


# --- status changes ----------------------------------------------------------

class RecurrenceUnsupported(Exception):
    """A status change on a task with a `🔁` rule: not implemented yet."""


def apply_status(task: Task, new_status: Status, today: date, settings: TasksSettings) -> list[Task]:
    """`handleNewStatusWithRecurrenceInUsersOrder` for a non-recurring task.

    Returns the tasks that replace the line: `[task]` unchanged when the
    status is identical, `[]` when `🏁 delete` removes a completed task,
    otherwise the one changed task. Raises `RecurrenceUnsupported` for a task
    with a recurrence rule, before anything is computed.
    """
    if task.recurrence is not None:
        raise RecurrenceUnsupported()
    if new_status.identical_to(task.status):
        return [task]
    done = _new_date(task, new_status, "DONE", task.done, settings.set_done_date, today)
    cancelled = _new_date(task, new_status, "CANCELLED", task.cancelled, settings.set_cancelled_date, today)
    changed = replace(task, status=new_status, done=done, cancelled=cancelled)
    # `handleOnCompletion`: delete only when the type becomes DONE from not DONE.
    if task.on_completion == "delete" and new_status.type == "DONE" and task.status.type != "DONE":
        return []
    return [changed]


def _new_date(task: Task, new_status: Status, status_type: str, old: TaskDate | None, enabled: bool, today: date):
    # `Task.newDate`.
    if new_status.type != status_type:
        return None
    if task.status.type != status_type:
        return TaskDate.of(today) if enabled else None
    return old


# --- urgency and sorting -----------------------------------------------------

def urgency(task: Task, today: date) -> float:
    """`Urgency.calculate`, with "now" as the owner's local today."""
    score = 0.0
    if task.due is not None and task.due.value is not None:
        days_overdue = (today - task.due.value).days
        if days_overdue >= 7.0:
            multiplier = 1.0
        elif days_overdue >= -14.0:
            multiplier = ((days_overdue + 14.0) * 0.8) / 21.0 + 0.2
        else:
            multiplier = 0.2
        score += multiplier * 12.0
    if task.scheduled is not None and task.scheduled.value is not None:
        if today >= task.scheduled.value:
            score += 1 * 5.0
    if task.start is not None and task.start.value is not None:
        if today < task.start.value:
            score += 1 * -3.0
    if task.priority == Priority.HIGHEST:
        score += 1.5 * 6.0
    elif task.priority == Priority.HIGH:
        score += 1.0 * 6.0
    elif task.priority == Priority.MEDIUM:
        score += 0.65 * 6.0
    elif task.priority == Priority.NONE:
        score += 0.325 * 6.0
    elif task.priority == Priority.LOWEST:
        score -= 0.3 * 6.0
    return score


def due_sort_key(task: Task) -> tuple:
    # `compareByDate`: an invalid date first, then dates ascending, none last.
    if task.due is None:
        return (2,)
    if task.due.value is None:
        return (0,)
    return (1, task.due.value)


def locale_key(text: str) -> tuple:
    """Approximates JS `localeCompare(…, {numeric: true})` without ICU.

    Case-insensitive, digit runs compared as numbers. ICU's accent and
    punctuation weights are not reproduced; declared in tasks.md.
    """
    parts = re.split(r"([0-9]+)", text)
    return tuple((0, int(p), p) if p.isdigit() else (1, p.casefold(), p) for p in parts if p)


def default_sort_key(task: Task, path: str, line_number: int, today: date) -> tuple:
    """`Sort.defaultSorters`: status type, urgency, due, priority, path.

    The plugin's sort is stable over its cache order (file, then line), so
    `(path, line_number)` is the final tie-break.
    """
    return (
        STATUS_TYPE_GROUP.get(task.status.type, 7),
        -urgency(task, today),
        due_sort_key(task),
        int(task.priority),
        locale_key(path),
        path,
        line_number,
    )
