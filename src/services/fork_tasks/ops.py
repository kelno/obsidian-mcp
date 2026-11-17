"""What the task write tools do to a note's text, without any I/O.

`create_task`, `edit_task` and `set_task_status` each compute the note's new
text here from the bytes they read, then publish it through the repo's one
anchored write path (`tools.py`). Keeping the text work pure makes every rule
below testable on strings:

- **Only that line changes, or one line is added, or (`🏁 delete`) that line
  is removed, or (a recurring task ticked done) that line becomes two: its
  next occurrence and itself.** Every other byte, terminators included, is
  copied.
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

from src.services.fork_tasks import format as tf
from src.services.fork_tasks import recurrence as _recurrence
from src.services.fork_tasks.query import QueryError, resolve_date
from src.services.fork_tasks.scan import _toggles_comment, scan_note, split_lines
from src.services.fork_tasks.settings import TasksSettings


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
CLEARABLE = ("priority", "on_completion", "recurrence", "id", "depends_on", *EDIT_DATE_FIELDS)


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


def _recurrence_arg(value: str) -> str:
    try:
        return _recurrence.canonical(value)
    except _recurrence.RecurrenceUnsupported as exc:
        raise TaskOpError(
            "invalid_argument",
            f"recurrence {value!r} is not a rule the task tools can follow ({exc}). Supported: "
            f"{_recurrence.SUPPORTED}.",
        ) from None


def recurring_unsupported(rule: str, reason: str, *, action: str) -> TaskOpError:
    """The refusal for a task whose own `🔁` rule the tools cannot follow."""
    return TaskOpError(
        "recurring_task",
        f"this task repeats (🔁 {rule}), and that rule is not one the task tools can follow ({reason}); "
        f"{action} it in Obsidian, so the Tasks plugin handles its next occurrence, or, if the owner agrees "
        "(it changes their rule), first replace the rule with edit_task(…, recurrence=…) or remove it with "
        "edit_task(…, clear=[\"recurrence\"]). Supported rules: "
        f"{_recurrence.SUPPORTED}. Nothing was written.",
    )


def recurring_dates_unsupported(rule: str, reason: str) -> TaskOpError:
    """The refusal for a tick the rule allows but the task's dates don't."""
    return TaskOpError(
        "recurring_task",
        f"this task repeats (🔁 {rule}), a rule the task tools follow, but they cannot move its dates on "
        f"({reason}); tick it in Obsidian, so the Tasks plugin handles its next occurrence. Nothing was written.",
    )


def _id_arg(value: str) -> str:
    if not tf.TASK_ID_RE.match(value) or not value:
        raise TaskOpError(
            "invalid_argument",
            f"a task id is one or more letters, digits, '-' or '_', as the Tasks plugin reads 🆔 (got {value!r})",
        )
    return value


def _depends_on_arg(values: list[str]) -> tuple[str, ...]:
    if not values:
        raise TaskOpError("invalid_argument", "depends_on needs at least one task id; to remove them, use clear")
    out: list[str] = []
    for value in values:
        if _id_arg(value) not in out:
            out.append(value)
    return tuple(out)


def apply_fields(task: tf.Task, *, today: date, description: str | None = None, priority: str | None = None,
                 on_completion: str | None = None, dates: dict[str, str | None] | None = None,
                 clear: list[str] | None = None, recurrence: str | None = None,
                 id: str | None = None, depends_on: list[str] | None = None,
                 settings: TasksSettings | None = None) -> tf.Task:
    """The dialog's `applyEdits` for the fields the tools expose.

    A `🔁` rule, given or already on the task, is written in the plugin's
    wording, as the dialog saves it; the dialog refuses a repeating task with
    no due, scheduled or start date, and so do the tools. With `settings`,
    a write that sets the rule or a date also refuses dates the rule cannot
    be followed from (yearly from February 29), so the problem shows when
    the task is written rather than when it is ticked.

    `id` (🆔) and `depends_on` (⛔) are checked for their characters and
    against each other here; whether other tasks in the vault carry them is
    `check_task_ids`' question, which needs the index.
    """
    changes: dict = {}
    if description is not None:
        changes["description"] = description
    if priority is not None:
        changes["priority"] = _priority_arg(priority)
    if on_completion is not None:
        changes["on_completion"] = _on_completion_arg(on_completion)
    if recurrence is not None:
        changes["recurrence"] = _recurrence_arg(recurrence)
    if id is not None:
        changes["id"] = _id_arg(id)
    if depends_on is not None:
        changes["depends_on"] = _depends_on_arg(depends_on)
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
        changes[name] = {"priority": tf.Priority.NONE, "on_completion": "", "id": "", "depends_on": ()}.get(name)
        if name == "scheduled":
            changes["scheduled_inferred"] = False
    if "recurrence" not in changes and task.recurrence is not None:
        try:
            changes["recurrence"] = _recurrence.canonical(task.recurrence)
        except _recurrence.RecurrenceUnsupported as exc:
            raise recurring_unsupported(task.recurrence, str(exc), action="edit") from None
    updated = replace(task, **changes)
    if "description" in changes:
        updated = replace(updated, tags=tf.extract_hashtags(updated.description))
    if updated.id and updated.id in updated.depends_on:
        raise TaskOpError("invalid_argument", f"a task cannot depend on itself (🆔 {updated.id} is in its depends_on)")
    if updated.recurrence is not None and all(
        d is None for d in (updated.start, None if updated.scheduled_inferred else updated.scheduled, updated.due)
    ):
        # `EditableTask.parseAndValidateRecurrence`: "due, scheduled or start date required".
        raise TaskOpError(
            "invalid_argument",
            "a repeating task needs a due, scheduled or start date, which its next occurrence is counted "
            "from (the Tasks plugin's dialog refuses one without); give one. Nothing was written.",
        )
    touches_dates = recurrence is not None or any(v is not None for v in (dates or {}).values()) or any(
        name in EDIT_DATE_FIELDS for name in (clear or ()))
    if updated.recurrence is not None and settings is not None and touches_dates:
        try:
            _recurrence.next_occurrence(updated, _recurrence.parse(updated.recurrence), updated.status, today,
                                        settings)
        except _recurrence.RecurrenceUnsupported as exc:
            when = ", ".join(f"{name} {d.text}" for name in ("start", "scheduled", "due")
                             if (d := getattr(updated, name)) is not None)
            raise TaskOpError(
                "invalid_argument",
                f"the 🔁 rule {updated.recurrence!r} cannot be followed from this task's dates ({when}: {exc}), so "
                "ticking it would be refused; change the date or the rule. Nothing was written.",
            ) from None
    return updated


#: A task with an id or dependencies, somewhere in the vault:
#: `("path:line", id or "", depends_on, done)`, `done` being the plugin's
#: `isDone` (DONE, CANCELLED or NON_TASK), which never waits for anything.
TaskLink = tuple[str, str, tuple[str, ...], bool]


def task_links_in_note(text: str, path: str, settings: TasksSettings, registry: tf.StatusRegistry,
                       *, except_line: int | None = None) -> list[TaskLink]:
    """The tasks of one note that carry 🆔 or ⛔, as its text reads now."""
    out: list[TaskLink] = []
    for scanned in scan_note(text).lines:
        if scanned.kind != "task" or scanned.line_number == except_line:
            continue
        if "🆔" not in scanned.text and "⛔" not in scanned.text:
            continue
        task = parse_line(scanned.text, path, settings, registry)
        if task is not None and (task.id or task.depends_on):
            out.append((f"{path}:{scanned.line_number}", task.id, task.depends_on, task.is_done))
    return out


def _refs(refs: list[str]) -> str:
    return ", ".join(refs[:5]) + (f" and {len(refs) - 5} more" if len(refs) > 5 else "")


def check_task_ids(before: tf.Task | None, after: tf.Task, links: list[TaskLink]) -> list[str]:
    """Refuse a write that would leave tasks wrongly chained; the notes to give otherwise.

    `links` are the vault's other tasks with an id or dependencies (the task
    being written left out). The plugin checks none of these: ids are
    "intended to be unique across the whole vault", a dependency on an id no
    task has blocks nothing, and two tasks waiting for each other are blocked
    for ever. Each is almost always a mistake that would silently unchain or
    lock the tasks, so the tools refuse rather than write it:

    - a new id another task already has (any task, done or not: the plugin
      blocks on any open task with the id, so a reused one could re-block);
    - a changed or removed id that open tasks wait for (they would be
      unblocked without anyone asking);
    - a new dependency on an id no task has;
    - a dependency that leads, through open tasks waiting for one another,
      back to this one (a cycle, however long), checked for every dependency
      when the task takes a new id, since open tasks may already name it.

    A finished task waits for nothing and blocks nobody (`Task.isBlocked`
    looks only for open carriers, `isBlocking` is false when done): a done
    waiter neither stops a rename nor closes a cycle, and a done task being
    written is free to change its id, can't close a cycle and blocks no one.
    A dependency the task already had is otherwise kept unchecked. The one
    note: open tasks that already named the new id, now blocked by this task.
    """
    carriers: dict[str, list[str]] = {}
    for ref, task_id, _deps, _done in links:
        if task_id:
            carriers.setdefault(task_id, []).append(ref)
    open_links = [(ref, task_id, deps) for ref, task_id, deps, done in links if not done]
    new_id = bool(after.id) and (before is None or after.id != before.id)
    if new_id and after.id in carriers:
        raise TaskOpError(
            "invalid_argument",
            f"another task already has 🆔 {after.id} ({_refs(carriers[after.id])}); task ids are unique across "
            "the vault, so the tasks that depend on it would be ambiguous. Choose another id. Nothing was written.",
        )
    if before is not None and before.id and after.id != before.id and not before.is_done:
        waiting = [ref for ref, _id, deps in open_links if before.id in deps]
        if waiting:
            raise TaskOpError(
                "invalid_argument",
                f"other tasks wait for this one's 🆔 {before.id} ({_refs(waiting)}); changing or removing it would "
                "unblock them without anyone asking. To rename it: give those tasks their depends_on without it "
                "(clear=[\"depends_on\"] only where it is the only one), rename it here, then add the new id to "
                "their depends_on; otherwise keep the id. Nothing was written.",
            )
    had = set(before.depends_on) if before is not None else set()
    new = [i for i in after.depends_on if i not in had]
    missing = [i for i in new if i not in carriers]
    if missing:
        raise TaskOpError(
            "invalid_argument",
            f"no task in the vault has 🆔 {', '.join(missing)}, so depending on it would block nothing. Give "
            "the task this one waits for an id first (edit_task(…, new_id=…)), then name it here. A task written to "
            "a new note in the last minutes may not count yet. Nothing was written.",
        )
    notes: list[str] = []
    if after.id and not after.is_done:
        cycle = _cycle_path(after.id, set(after.depends_on) if new_id else set(new), open_links)
        if cycle:
            raise TaskOpError(
                "invalid_argument",
                f"the tasks this one would wait for already wait for it, through {_refs(cycle)} (🆔 {after.id}); "
                "they would all stay blocked for ever. Nothing was written.",
            )
        if new_id:
            named = [ref for ref, _id, deps in open_links if after.id in deps]
            if named:
                notes.append(f"Open tasks already waited for 🆔 {after.id} ({_refs(named)}): they are blocked by "
                             "this task now, until it is done.")
    return notes


def _cycle_path(target: str, start: set[str], open_links: list[tuple[str, str, tuple[str, ...]]]) -> list[str]:
    """The refs of open tasks leading from the ids in `start` back to `target`, or [].

    A walk over the open tasks carrying each id, following their dependencies;
    the first chain that names `target` is the cycle a write would close.
    """
    by_id: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    for ref, task_id, deps in open_links:
        if task_id:
            by_id.setdefault(task_id, []).append((ref, deps))
    queue = [(task_id, []) for task_id in sorted(start)]
    seen: set[str] = set()
    while queue:
        task_id, path = queue.pop(0)
        if task_id in seen:
            continue
        seen.add(task_id)
        for ref, deps in by_id.get(task_id, ()):
            if target in deps:
                return path + [ref]
            queue.extend((dep, path + [ref]) for dep in deps if dep not in seen)
    return []


def dropped_dependency_notes(before: tf.Task | None, after: tf.Task) -> list[str]:
    """`depends_on` replaces the whole list: say which ids a replacement left out."""
    if before is None or not after.depends_on:
        return []
    dropped = [i for i in before.depends_on if i not in after.depends_on]
    if not dropped:
        return []
    return [f"depends_on was replaced: this task no longer waits for 🆔 {', '.join(dropped)}. To add a dependency, "
            "pass every id it should keep waiting for too."]


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


#: The prefix of an area tag, as `create_task`'s `areas` writes it (`#area/<slug>`).
AREA_TAG_PREFIX = "#area/"


def _has_word(description: str, word: str) -> bool:
    return re.search("(^|" + tf._WS + ")" + re.escape(word) + "($|" + tf._WS + ")", description) is not None


def area_tags_of(description: str) -> list[str]:
    """The area tags (`#area/…`) in `description`, in order, each once."""
    out: list[str] = []
    for tag in tf.extract_hashtags(description):
        if tag.startswith(AREA_TAG_PREFIX) and tag not in out:
            out.append(tag)
    return out


def with_area_tags(task: tf.Task, description: str | None, area_tags: list[str] | None) -> str | None:
    """The edited description with the task's area tags kept or set; None if unchanged.

    An area tag says which area the task belongs to, not what it says, so a
    new description that leaves one out keeps it (added at the end, as
    `create_task` adds them): an agent rewording a task should not move it
    out of its area by accident (F108). `area_tags` (`area/<slug>`, already
    resolved) replaces them all instead, `[]` removing every one.
    """
    if description is None and area_tags is None:
        return None
    text = task.description if description is None else description
    if area_tags is None:
        wanted = area_tags_of(task.description)
    else:
        wanted = ["#" + t for t in area_tags]
        for tag in area_tags_of(text):
            if tag not in wanted:
                pattern = re.compile("(?:^|" + tf._WS + ")" + re.escape(tag) + "(?=$|" + tf._WS + ")")
                text = tf.js_trim(pattern.sub("", text))
    for tag in wanted:
        if not _has_word(text, tag):
            text = f"{text} {tag}"
    if not tf.js_trim(text):
        raise TaskOpError("invalid_argument", "a task needs a description besides its area tags")
    return text


def dropped_tag_notes(before: tf.Task, after: tf.Task, settings: TasksSettings) -> list[str]:
    """Say which tags (other than area tags and the global filter) an edit removed."""
    kept = set(after.tags)
    dropped = [t for t in dict.fromkeys(before.tags)
               if t not in kept and not t.startswith(AREA_TAG_PREFIX) and t != settings.global_filter]
    if not dropped:
        return []
    return [f"The new description drops the tag(s) {' '.join(dropped)} the task had. If that wasn't meant, "
            "edit it again with them in the description."]


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
             dates: dict[str, str | None], recurrence: str | None = None,
             id: str | None = None, depends_on: list[str] | None = None) -> tf.Task:
    """`createTaskLineModal`: the dialog's new task, as it would be saved."""
    text = _description_arg(description)
    if settings.global_filter:
        text = _without_global_filter(text, settings.global_filter)
    for tag in area_tags:
        if not _has_word(text, "#" + tag):
            text = f"{text} #{tag}"
    if settings.global_filter:
        text = f"{settings.global_filter} {text}"
    todo = registry.by_symbol_or_create(" ")
    created = tf.TaskDate.of(today) if settings.set_created_date else None
    base = tf.Task(indentation="", list_marker="-", status=todo, description=text, created=created,
                   tags=tf.extract_hashtags(text))
    task = apply_fields(base, today=today, priority=priority, on_completion=on_completion, dates=dates,
                        recurrence=recurrence, id=id, depends_on=depends_on, settings=settings)
    if status is not None:
        if task.recurrence is not None and status.type == "DONE":
            raise TaskOpError(
                "invalid_argument",
                "a new repeating task cannot start done: ticking it would also write its next occurrence. "
                "Create it, then tick it with set_task_status. Nothing was written.",
            )
        result = tf.apply_status(task, status, today, settings, registry)
        if not result:
            raise TaskOpError(
                "invalid_argument",
                "a new task marked done with 🏁 delete is removed at once by the Tasks plugin, so there "
                "is nothing to write",
            )
        [task] = result
    return task


@dataclass(frozen=True)
class StatusChange:
    """What a status change writes in place of the task's line."""

    tasks: list[tf.Task]            # in the order written; [] when `🏁 delete` removed it
    #: Index in `tasks` of a recurring task's new occurrence, None when there is none.
    next_index: int | None = None


def new_status_tasks(task: tf.Task, status: tf.Status, today: date, settings: TasksSettings,
                     registry: tf.StatusRegistry) -> StatusChange | None:
    """The replacement tasks, None when nothing changes."""
    try:
        result = tf.apply_status(task, status, today, settings, registry)
    except _recurrence.RecurrenceDatesUnsupported as exc:
        raise recurring_dates_unsupported(task.recurrence, str(exc)) from None
    except tf.RecurrenceUnsupported as exc:
        raise recurring_unsupported(task.recurrence, str(exc), action="tick") from None
    if result == [task]:
        return None
    completes = task.recurrence is not None and status.type == "DONE" and task.status.type != "DONE"
    next_index = next((i for i, t in enumerate(result) if t.status.type != "DONE"), None) if completes else None
    return StatusChange(result, next_index)


def splice(lines: list[tuple[str, str]], index: int, new_lines: list[str]) -> str:
    """The note with line `index` replaced by `new_lines`.

    The last new line takes the replaced line's terminator, so a note with no
    final terminator still has none; the lines before it take the replaced
    line's terminator, or the note's usual one when it had none. Removing the
    last line of a note that has no final terminator also removes the
    terminator before it — the plugin's `lines.splice(…).join('\\n')`.
    """
    out = list(lines)
    content, terminator = out[index]
    if new_lines:
        between = terminator or _dominant_terminator(lines)
        out[index:index + 1] = [(line, between) for line in new_lines[:-1]] + [(new_lines[-1], terminator)]
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


def verify_other_lines(old_text: str, new_text: str, line_number: int, delta: int, path: str, *,
                       replaced: bool = False) -> None:
    """Every other line must be read exactly as before the write.

    The bytes of the other lines are copied, but their *meaning* can still
    move: a description holding `%%` opens an Obsidian comment that hides
    everything below it, a stray fence or `<div>` turns later tasks into code.
    So the note is scanned before and after, and every line other than the
    written one must keep its kind, reason and text, at its shifted number.

    `delta` is +1 when a line was inserted at `line_number`, -1 when the line
    at `line_number` was removed, 0 when it was replaced, and +1 with
    `replaced=True` when it was replaced by two lines (a recurring task and
    its next occurrence).
    """
    rows_old, unmatched_old = _scanned(old_text)
    rows_new, unmatched_new = _scanned(new_text)
    if delta == 0:
        before = _project(rows_old, skip=line_number)
        after = _project(rows_new, skip=line_number)
    elif delta == 1 and replaced:
        before = _project(rows_old, skip=line_number, shift_from=line_number + 1, shift=1)
        after = _project(rows_new, skip=line_number, skip_also=line_number + 1)
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


def _project(rows: list[tuple], *, skip: int | None = None, skip_also: int | None = None,
             shift_from: int | None = None, shift: int = 0) -> set:
    """`rows` without lines `skip` and `skip_also`, lines from `shift_from` on moved by `shift`."""
    out = set()
    for number, kind, reason, text in rows:
        if number in (skip, skip_also):
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
