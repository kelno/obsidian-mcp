"""What the task write tools do to a note's text, without any I/O.

`create_task`, `edit_task` and `set_task_status` each compute the note's new
text here from the bytes they read, then publish it through the repo's one
anchored write path (`tools.py`). Keeping the text work pure makes every rule
below testable on strings:

- **Only that line changes, or one line is added, or (`🏁 delete`) that line
  is removed.** Every other byte, terminators included, is copied.
- **The new line is the plugin's**: `task_format` writes it in canonical order,
  exactly as the plugin's dialog and toggle do (they re-serialize the whole
  line too).
- **Every written line is checked by reading it back**: the new text is
  scanned again and the line must be a task there, parsing to exactly the task
  intended. A description ending in `📅 2026-01-01` or a task appended inside
  an unclosed code fence would otherwise change meaning or vanish.
- **A line the plugin would damage is refused**, not rewritten: an invalid
  date (the plugin writes `Invalid date`) or an unknown `🏁` value (its parser
  drops it).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import PurePosixPath
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.services import task_format as tf
from src.services.task_query import QueryError, resolve_date
from src.services.task_scan import _toggles_comment, scan_note, split_lines
from src.services.task_settings import TasksSettings


class TaskOpError(Exception):
    """A refusal: `code` is the refusal code, the message says what to do."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


#: Line warnings a rewrite would turn into damage (see the module docstring).
DAMAGING_WARNINGS = {
    "invalid_date": "it has a date that is not a real calendar date; the Tasks plugin would rewrite it as 'Invalid date'",
    "unknown_on_completion": "its 🏁 value is neither 'keep' nor 'delete'; the Tasks plugin's parser drops it on any rewrite",
    "duplicate_field": "it gives the same field twice (two due dates, two priorities, …); the Tasks plugin keeps one and drops the other on any rewrite",
}

STATUS_NAMES = {"todo": "TODO", "done": "DONE", "cancelled": "CANCELLED", "in_progress": "IN_PROGRESS"}
EDIT_DATE_FIELDS = ("created", "start", "scheduled", "due")
CLEARABLE = ("priority", "on_completion", *EDIT_DATE_FIELDS)


def owner_today(timezone_name: str, now: datetime | None = None) -> date:
    """The owner's local date, never the server clock's day."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TaskOpError(
            "task_settings_invalid",
            f"TASKS_TIMEZONE {timezone_name!r} is not a time zone this server knows ({exc}); "
            "the task tools cannot tell which day it is",
        ) from None
    return (now.astimezone(zone) if now is not None else datetime.now(zone)).date()


def fallback_date(path: str, settings: TasksSettings) -> date | None:
    """`DateFallback.fromPath`: the scheduled date a file name implies, or None."""
    if not settings.use_filename_as_scheduled_date:
        return None
    if settings.filename_as_date_folders and not any(
        path.startswith(folder + "/") for folder in settings.filename_as_date_folders
    ):
        return None
    name = PurePosixPath(path).name
    basename = name[: name.rfind(".")] if "." in name else name
    fmt = settings.filename_as_scheduled_date_format
    if fmt:
        if fmt not in ("YYYY-MM-DD", "YYYYMMDD"):
            raise TaskOpError(
                "task_settings_invalid",
                f"the Tasks plugin's file-name date format {fmt!r} is not one the task tools "
                "implement (YYYY-MM-DD or YYYYMMDD)",
            )
        pattern = r"([0-9]{4})-([0-9]{2})-([0-9]{2})\Z" if fmt == "YYYY-MM-DD" else r"([0-9]{4})([0-9]{2})([0-9]{2})\Z"
        if (m := re.match(pattern, basename)) and (d := _ymd(*m.groups())):
            return d
    for pattern in (r"([0-9]{4})-([0-9]{2})-([0-9]{2})", r"([0-9]{4})([0-9]{2})([0-9]{2})"):
        if m := re.search(pattern, basename):
            return _ymd(*m.groups())
    return None


def _ymd(y: str, m: str, d: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_line(text: str, path: str, settings: TasksSettings, registry: tf.StatusRegistry) -> tf.Task | None:
    return tf.parse_task_line(
        text, registry, global_filter=settings.global_filter, fallback_date=fallback_date(path, settings)
    )


@dataclass(frozen=True)
class Located:
    lines: list[tuple[str, str]]   # (content, terminator) for every line
    index: int                     # 0-based index of the task line
    task: tf.Task
    text: str                      # the whole note, as read


def locate(text: str, path: str, line: int, expected_line: str, settings: TasksSettings,
           registry: tf.StatusRegistry) -> Located:
    """The task at `line` (1-based), checked against what the caller expects."""
    lines = split_lines(text)
    if not 1 <= line <= len(lines):
        raise TaskOpError("line_mismatch", f"{path} has {len(lines)} line(s); there is no line {line}. Nothing was written.")
    actual = lines[line - 1][0]
    if actual != expected_line:
        raise TaskOpError(
            "line_mismatch",
            f"line {line} of {path} is not the expected_line given. Nothing was written. "
            "Call list_tasks again and use the ref and line it returns.",
        )
    scan = scan_note(text)
    _refuse_unmatched_fence(scan, path)
    scanned = next((s for s in scan.lines if s.line_number == line), None)
    if scanned is None or scanned.kind != "task":
        why = f" ({scanned.reason})" if scanned is not None and scanned.reason else ""
        raise TaskOpError("not_a_task", f"line {line} of {path} is not a task the Tasks plugin reads{why}. Nothing was written.")
    task = parse_line(actual, path, settings, registry)
    if task is None:
        raise TaskOpError(
            "not_a_task",
            f"line {line} of {path} is not a task the Tasks plugin reads (it lacks the global filter "
            f"{settings.global_filter!r}). Nothing was written.",
        )
    for warning in task.warnings:
        if warning in DAMAGING_WARNINGS:
            raise TaskOpError(
                "not_a_task",
                f"line {line} of {path} cannot be rewritten safely: {DAMAGING_WARNINGS[warning]}. "
                "Fix the line by hand (edit_note) first. Nothing was written.",
            )
    return Located(lines, line - 1, task, text)


def refuse_unsafe_note(text: str, path: str) -> None:
    """Refuse a note whose code-fence extent cannot be decided."""
    _refuse_unmatched_fence(scan_note(text), path)


def _refuse_unmatched_fence(scan, path: str) -> None:
    if scan.unmatched_fence_lines:
        where = ", ".join(str(n) for n in scan.unmatched_fence_lines)
        raise TaskOpError(
            "content_unsafe",
            f"{path} has an indented code fence that nothing closes (line {where}); whether the "
            "lines below it are code cannot be decided, so the task tools do not write this note. "
            "Close the fence first. Nothing was written.",
        )


def resolve_status(registry: tf.StatusRegistry, status: str | None, symbol: str | None) -> tf.Status:
    """`status` (a type name) or `symbol` (any registered status), exactly one."""
    if (status is None) == (symbol is None):
        raise TaskOpError("invalid_argument", "give exactly one of status (todo, done, cancelled, in_progress) or symbol")
    if symbol is not None:
        found = registry.by_symbol(symbol)
        if found is None:
            known = ", ".join(f"{s.symbol!r} ({s.name})" for s in registry.registered)
            raise TaskOpError("invalid_argument", f"no status with symbol {symbol!r} is set up in the Tasks plugin; known: {known}")
        return found
    wanted = STATUS_NAMES.get(status.strip().lower())
    if wanted is None:
        raise TaskOpError("invalid_argument", f"status must be one of {', '.join(STATUS_NAMES)} (got {status!r})")
    found = registry.first_of_type(wanted)
    if found is None:
        raise TaskOpError("invalid_argument", f"no status of type {wanted} is set up in the Tasks plugin")
    return found


def _date_arg(name: str, value: str, today: date) -> tf.TaskDate:
    try:
        return tf.TaskDate.of(resolve_date(value, today))
    except QueryError as exc:
        raise TaskOpError("invalid_argument", f"{name}: {exc}") from None


def _priority_arg(value: str) -> str:
    found = tf.PRIORITY_NAMES.get(value.strip().lower())
    if found is None:
        raise TaskOpError("invalid_argument", f"priority must be one of {', '.join(tf.PRIORITY_NAMES)} (got {value!r})")
    return found


def _on_completion_arg(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in ("keep", "delete"):
        return lowered
    if lowered in ("none", "ignore", ""):
        return ""
    raise TaskOpError("invalid_argument", f"on_completion must be keep, delete or none (got {value!r})")


def _description_arg(value: str) -> str:
    if any(ch in value for ch in "\n\r  "):
        raise TaskOpError("invalid_argument", "a task description is one line; it may not contain a line break")
    trimmed = tf.js_trim(value)
    if not trimmed:
        raise TaskOpError("invalid_argument", "a task needs a description")
    return trimmed


def apply_fields(task: tf.Task, *, today: date, description: str | None = None, priority: str | None = None,
                 on_completion: str | None = None, dates: dict[str, str | None] | None = None,
                 clear: list[str] | None = None) -> tf.Task:
    """The dialog's `applyEdits` for the fields the tools expose."""
    changes: dict = {}
    if description is not None:
        changes["description"] = description
    if priority is not None:
        changes["priority"] = _priority_arg(priority)
    if on_completion is not None:
        changes["on_completion"] = _on_completion_arg(on_completion)
    for name, value in (dates or {}).items():
        if value is not None:
            changes[name] = _date_arg(name, value, today)
            if name == "scheduled":
                changes["scheduled_inferred"] = False
    for name in clear or ():
        if name not in CLEARABLE:
            raise TaskOpError("invalid_argument", f"clear accepts {', '.join(CLEARABLE)} (got {name!r})")
        if name in changes:
            raise TaskOpError("invalid_argument", f"{name} is both set and cleared")
        changes[name] = tf.Priority.NONE if name == "priority" else "" if name == "on_completion" else None
        if name == "scheduled":
            changes["scheduled_inferred"] = False
    updated = replace(task, **changes)
    if "description" in changes:
        updated = replace(updated, tags=tf.extract_hashtags(updated.description))
    return updated


def edited_description(task: tf.Task, description: str | None, settings: TasksSettings) -> str | None:
    """The description as `EditableTask.applyEdits` saves it, or None if unchanged.

    The dialog shows the description with the global filter removed and adds
    it back in front on save when the task had it as a word or lacked it.
    """
    if description is None:
        return None
    new = _description_arg(description)
    gf = settings.global_filter
    if gf:
        shown = _remove_as_word(task.description, gf)
        add_back = shown != task.description or gf not in task.description
        if add_back:
            new = f"{gf} {_without_global_filter(new, gf)}"
    return new


def _remove_as_word(description: str, gf: str) -> str:
    # `GlobalFilter.removeAsWordFrom`.
    pattern = re.compile("(^|" + tf._WS + ")" + re.escape(gf) + "($|" + tf._WS + ")")
    if pattern.search(description):
        description = tf.js_trim(pattern.sub(lambda m: m.group(1) + m.group(2), description).replace("  ", " ", 1))
    return description


def _without_global_filter(description: str, gf: str) -> str:
    """The description argument with every whole-word copy of the global filter removed.

    Deliberate divergence from the dialog, which shows the description without
    the filter, so a person never types it: an agent does, often by passing
    back the `description` `list_tasks` returned. The dialog would then save
    the filter twice (`#task #task a`); the tools put it in front once.
    Each copy goes with the whitespace character before it, so the gaps left
    are the ones the text already had (not `_remove_as_word`, whose double-
    space collapse hits the first one anywhere in the text).
    """
    pattern = re.compile("(?:^|" + tf._WS + ")" + re.escape(gf) + "(?=$|" + tf._WS + ")")
    description = tf.js_trim(pattern.sub("", description))
    if not description:
        raise TaskOpError("invalid_argument", "a task needs a description besides the global filter "
                          f"({gf!r}), which the tools add themselves")
    return description


def new_task(*, description: str, settings: TasksSettings, registry: tf.StatusRegistry, today: date,
             area_tags: list[str], status: tf.Status | None, priority: str | None, on_completion: str | None,
             dates: dict[str, str | None]) -> tf.Task:
    """`createTaskLineModal`: the dialog's new task, as it would be saved."""
    text = _description_arg(description)
    if settings.global_filter:
        text = _without_global_filter(text, settings.global_filter)
    for tag in area_tags:
        if not re.search("(^|" + tf._WS + ")" + re.escape("#" + tag) + "($|" + tf._WS + ")", text):
            text = f"{text} #{tag}"
    if settings.global_filter:
        text = f"{settings.global_filter} {text}"
    todo = registry.by_symbol_or_create(" ")
    created = tf.TaskDate.of(today) if settings.set_created_date else None
    base = tf.Task(indentation="", list_marker="-", status=todo, description=text, created=created,
                   tags=tf.extract_hashtags(text))
    task = apply_fields(base, today=today, priority=priority, on_completion=on_completion, dates=dates)
    if status is not None:
        result = tf.apply_status(task, status, today, settings)
        if not result:
            raise TaskOpError(
                "invalid_argument",
                "a new task marked done with 🏁 delete is removed at once by the Tasks plugin, so there "
                "is nothing to write",
            )
        [task] = result
    return task


def new_status_tasks(task: tf.Task, status: tf.Status, today: date, settings: TasksSettings) -> list[tf.Task] | None:
    """The replacement tasks, `[]` for `🏁 delete`, None when nothing changes."""
    try:
        result = tf.apply_status(task, status, today, settings)
    except tf.RecurrenceUnsupported:
        raise TaskOpError(
            "recurring_task",
            "this task repeats (🔁). Recurring tasks are not supported by the task tools yet: "
            "tick it in Obsidian, so its next occurrence is created. Nothing was written.",
        ) from None
    if result == [task]:
        return None
    return result


def splice(lines: list[tuple[str, str]], index: int, new_lines: list[str]) -> str:
    """The note with line `index` replaced by `new_lines` (each taking its terminator).

    Removing the last line of a note that has no final terminator also removes
    the terminator before it — the plugin's `lines.splice(…).join('\\n')`.
    """
    out = list(lines)
    content, terminator = out[index]
    if new_lines:
        out[index:index + 1] = [(new_lines[0], terminator)] + [
            (line, terminator or _dominant_terminator(lines)) for line in new_lines[1:]
        ]
    else:
        del out[index]
        if not terminator and index > 0:
            prev, _ = out[index - 1]
            out[index - 1] = (prev, "")
    return "".join(c + t for c, t in out)


def _dominant_terminator(lines: list[tuple[str, str]]) -> str:
    for _content, terminator in lines:
        if terminator:
            return terminator
    return "\n"


def insert_line(text: str, new_line: str, heading: str | None, path: str) -> tuple[str, int]:
    """`(new text, 1-based line number of the inserted line)`.

    With `heading`, after the last non-blank line of that section (resolved by
    the repo's section resolver, over the frontmatter-stripped body as
    `edit_note(section=…)` does); otherwise at the end of the note.
    """
    from src.services import vault

    lines = split_lines(text)
    term = _dominant_terminator(lines)
    if heading is None:
        start, end = 0, len(text)
    else:
        diagnosis = vault.parse_frontmatter_diagnose(text)[2]
        if not diagnosis.valid and diagnosis.defect:
            raise TaskOpError("content_unsafe", f"{path} has a defective frontmatter block ({diagnosis.message}); fix it before adding a task under a heading. Nothing was written.")
        offset = len(diagnosis.block) if diagnosis.valid else 0
        body = text[offset:]
        headings = vault._scan_headings(body)
        idx, err = vault._resolve_section_index(headings, heading)
        if err is not None:
            raise TaskOpError("selector_unresolved", f"{err} Nothing was written.")
        body_start, body_end = vault._section_body_span(body, headings, idx)
        # The section's own lines only: a task added under `## Tasks` must not
        # land under its last subsection (`### Done`), which `body_end`
        # includes.
        if idx + 1 < len(headings):
            body_end = min(body_end, headings[idx + 1]["line_start"])
        start, end = offset + body_start, offset + body_end

    # The last non-blank line inside [start, end).
    position = None
    line_starts = []
    pos = 0
    for content, terminator in lines:
        line_starts.append((pos, content, terminator))
        pos += len(content) + len(terminator)
    for line_start, content, terminator in line_starts:
        if line_start < start or line_start >= end:
            continue
        if content.strip(" \t"):
            position = (line_start, content, terminator)
    if position is None:
        if heading is None:
            if not text:
                return new_line + term, 1
            insert_at = len(text)
            prefix_needs_break = not text.endswith(("\n", "\r"))
        else:
            insert_at = start
            prefix_needs_break = start > 0 and not text[:start].endswith(("\n", "\r"))
    else:
        line_start, content, terminator = position
        insert_at = line_start + len(content) + len(terminator)
        prefix_needs_break = not terminator
    prefix = text[:insert_at] + (term if prefix_needs_break else "")
    new_text = prefix + new_line + term + text[insert_at:]
    number = len(split_lines(prefix)) if prefix else 1
    return new_text, number


def verify_written(new_text: str, line_number: int, intended: tf.Task, path: str,
                   settings: TasksSettings, registry: tf.StatusRegistry, *, allow_field_text: bool) -> None:
    """The written line must read back as exactly the task intended."""
    scan = scan_note(new_text)
    scanned = next((s for s in scan.lines if s.line_number == line_number), None)
    if scanned is None or scanned.kind != "task":
        why = f" ({scanned.reason})" if scanned is not None and scanned.reason else ""
        raise TaskOpError(
            "content_unsafe",
            f"the new line would not be read as a task at line {line_number} of {path}{why}. Nothing was written.",
        )
    back = parse_line(scanned.text, path, settings, registry)
    if back is None or _comparable(back) != _comparable(intended):
        raise TaskOpError(
            "invalid_argument",
            "the description would be read back differently by the Tasks plugin (it ends with something "
            "the plugin reads as a field or tag, such as a date or priority emoji); put dates and "
            "priority in their own arguments. Nothing was written.",
        )
    if _toggles_comment(scanned.text) % 2:
        raise TaskOpError(
            "invalid_argument",
            "the new line holds an unpaired `%%`, which opens an Obsidian comment that hides the rest "
            "of the note. Nothing was written.",
        )
    if not allow_field_text and "field_in_description" in back.warnings:
        raise TaskOpError(
            "invalid_argument",
            "the description contains a Tasks field emoji (📅 ⏳ 🛫 ➕ ✅ ❌ 🔁 🏁 🆔 ⛔ or a priority) that "
            "the plugin would read as plain text; use the field's own argument. Nothing was written.",
        )


def verify_other_lines(old_text: str, new_text: str, line_number: int, delta: int, path: str) -> None:
    """Every other line must be read exactly as before the write.

    The bytes of the other lines are copied, but their *meaning* can still
    move: a description holding `%%` opens an Obsidian comment that hides
    everything below it, a stray fence or `<div>` turns later tasks into code.
    So the note is scanned before and after, and every line other than the
    written one must keep its kind, reason and text, at its shifted number.

    `delta` is +1 when a line was inserted at `line_number`, -1 when the line
    at `line_number` was removed, 0 when it was replaced.
    """
    rows_old, unmatched_old = _scanned(old_text)
    rows_new, unmatched_new = _scanned(new_text)
    if delta == 0:
        before = _project(rows_old, skip=line_number)
        after = _project(rows_new, skip=line_number)
    elif delta == 1:
        before = _project(rows_old, shift_from=line_number, shift=1)
        after = _project(rows_new, skip=line_number)
    else:
        before = _project(rows_old, skip=line_number, shift_from=line_number + 1, shift=-1)
        after = _project(rows_new)
    if before != after or unmatched_old != unmatched_new:
        raise TaskOpError(
            "content_unsafe",
            f"this change would alter how other lines of {path} are read (for example a `%%` that "
            "opens an Obsidian comment, or a code fence), so it was refused. Nothing was written.",
        )


def _scanned(text: str) -> tuple[list[tuple], int]:
    scan = scan_note(text)
    return [(s.line_number, s.kind, s.reason, s.text) for s in scan.lines], len(scan.unmatched_fence_lines)


def _project(rows: list[tuple], *, skip: int | None = None, shift_from: int | None = None, shift: int = 0) -> set:
    """`rows` without line `skip`, lines from `shift_from` on moved by `shift`."""
    out = set()
    for number, kind, reason, text in rows:
        if number == skip:
            continue
        if shift_from is not None and number >= shift_from:
            number += shift
        out.add((number, kind, reason, text))
    return out


def _comparable(task: tf.Task) -> tuple:
    # A scheduled date taken from the file name is not on the line, and it
    # comes and goes with the other dates (the plugin applies it only to a
    # task with no start, scheduled or due date), so it is not compared.
    scheduled = None if task.scheduled_inferred else task.scheduled
    return tuple(getattr(task, f) for f in (
        "indentation", "list_marker", "description", "priority", "created", "start", "due",
        "done", "cancelled", "recurrence", "on_completion", "id", "depends_on", "block_link",
    )) + (scheduled, task.status.symbol)
