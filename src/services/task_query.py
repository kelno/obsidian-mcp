"""`list_tasks`' filters and sorting, each one a line of the Tasks query language.

The rule (the owner's parity tenet): the tool must return the same tasks as a
Tasks query block in Obsidian asking the same question. So the tool does not
have filters of its own. Each parameter is turned into the Tasks query line it
stands for (`query_lines`), and every line — the tool's and the plugin's
global query alike — goes through one parser (`parse_filter_line`) that
implements that line with the plugin's semantics, from its 8.6.0 source:

| query line | plugin code | semantics |
| --- | --- | --- |
| `done` / `not done` | `StatusField` | status *type* DONE, CANCELLED or NON_TASK / the others |
| `status.type is[ not] T` | `StatusTypeField` | the status type from the owner's status settings |
| `path [does not ]include[s] S` | `PathField` / `TextField` | case-insensitive substring of the vault path, `.md` included |
| `tags [do not ]include S` | `TagsField` / `MultiTextField` | case-insensitive substring of any tag on the line (`#` kept) |
| `description [does not ]include[s] S` | `DescriptionField` | substring, the global filter removed first |
| `due/scheduled/starts/happens [before/after/on/on or before/on or after] D` | `DateField` | a missing date matches only for `starts`; `happens` = any of start, scheduled, due |
| `has/no due/scheduled/start/happens date` | `DateField` | the date is present (even if invalid) / absent |
| `priority is [above/below/not ]P` | `PriorityField` | ordered highest → lowest, none between medium and low |
| `(A) OR (B) …` | boolean combination | any of the parts |

A line outside that set (a natural-language date, `sort by`, `group by`,
`limit`, regex matching, …) is a `QueryError`, never ignored: a global query
the tool cannot apply would make it answer a different question from the
plugin's while looking right.

Sorting is the plugin's `Sort.by`: the caller's sorter, then the default
sorters (`task_format.default_sort_key`). `random` is the server's own shuffle
and by nature cannot match the plugin's.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import PurePosixPath
from typing import Callable

from src.services import task_format as tf


class QueryError(ValueError):
    """A filter, date or area the tools cannot apply as the plugin would."""


@dataclass(frozen=True)
class Entry:
    path: str
    line_number: int
    task: tf.Task
    text: str


@dataclass(frozen=True)
class Filter:
    line: str
    test: Callable[[Entry], bool]


SORTS = ("default", "due", "priority", "random")
STATUS_CHOICES = ("not_done", "done", "any")

_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_DATE_WORDS = {"today": 0, "tomorrow": 1, "yesterday": -1}


def resolve_date(text: str, today: date) -> date:
    """An ISO date or `today` / `tomorrow` / `yesterday` (the owner's local day).

    The plugin accepts any natural-language date through chrono; the tools
    accept only these, so an agent never depends on a parser it cannot see.
    """
    word = text.strip().lower()
    if word in _DATE_WORDS:
        return today + timedelta(days=_DATE_WORDS[word])
    if _ISO.match(word):
        try:
            return date.fromisoformat(word)
        except ValueError:
            pass
    raise QueryError(
        f"{text!r} is not a date the task tools understand: use YYYY-MM-DD, "
        "'today', 'tomorrow' or 'yesterday'"
    )


# --- one parser for every filter line ----------------------------------------

_DATE_LINE = re.compile(
    r"(due|scheduled|starts|happens)\s+(?:(on or before|in or before|on or after|in or after|before|after|on|in)\s+)?(.+)\Z",
    re.I,
)
_HAS_DATE = re.compile(r"(has|no) (due|scheduled|start|happens) date\Z", re.I)
_PRIORITY_LINE = re.compile(r"priority(?:\s+is)?(?:\s+(above|below|not))?\s+(lowest|low|none|medium|high|highest)\Z", re.I)
_TEXT_LINE = re.compile(
    r"(path|description|tags|tag)\s+(includes|include|does not include|do not include)\s(.*)\Z",
    re.I,
)
_STATUS_TYPE = re.compile(r"status\.type\s+(is|is not)\s+(\S+)\Z", re.I)
_OR_LINE = re.compile(r"\((.+)\)\Z")

_DATE_ATTR = {"due": "due", "scheduled": "scheduled", "starts": "start", "start": "start"}


def _date_of(entry: Entry, field: str) -> tf.TaskDate | None:
    return getattr(entry.task, _DATE_ATTR[field])


def parse_filter_line(line: str, today: date, *, global_filter: str = "") -> Filter:
    text = line.strip()
    lowered = text.lower()

    if lowered == "not done":
        return Filter(text, lambda e: not e.task.is_done)
    if lowered == "done":
        return Filter(text, lambda e: e.task.is_done)

    if (m := _OR_LINE.match(text)) and ") OR (" in text:
        parts = [parse_filter_line(p, today, global_filter=global_filter) for p in m.group(1).split(") OR (")]
        return Filter(text, lambda e: any(p.test(e) for p in parts))

    if m := _STATUS_TYPE.match(text):
        wanted = m.group(2).upper()
        if wanted not in tf.STATUS_TYPES:
            raise QueryError(f"unknown status type {m.group(2)!r} in {text!r}; use one of {', '.join(tf.STATUS_TYPES)}")
        negate = m.group(1).lower() == "is not"
        return Filter(text, lambda e: (e.task.status.type == wanted) != negate)

    if m := _TEXT_LINE.match(text):
        field, operator, needle = m.group(1).lower(), m.group(2).lower(), m.group(3)
        negate = "not" in operator
        lowered_needle = needle.lower()
        if field == "path":
            def value(e: Entry) -> list[str]:
                return [e.path]
        elif field == "description":
            def value(e: Entry) -> list[str]:
                return [tf.js_trim(e.task.description.replace(global_filter, "", 1))]
        else:
            def value(e: Entry) -> list[str]:
                return list(e.task.tags)
        return Filter(text, lambda e: any(lowered_needle in v.lower() for v in value(e)) != negate)

    if m := _HAS_DATE.match(text):
        present = m.group(1).lower() == "has"
        field = m.group(2).lower()
        if field == "happens":
            return Filter(text, lambda e: any(d is not None for d in e.task.happens_dates()) == present)
        return Filter(text, lambda e: (_date_of(e, field) is not None) == present)

    if m := _PRIORITY_LINE.match(text):
        relation = (m.group(1) or "is").lower()
        wanted = tf.PRIORITY_NAMES[m.group(2).lower()]
        tests = {
            "is": lambda p: p == wanted,
            "above": lambda p: p < wanted,
            "below": lambda p: p > wanted,
            "not": lambda p: p != wanted,
        }
        check = tests[relation]
        return Filter(text, lambda e: check(e.task.priority))

    if m := _DATE_LINE.match(text):
        field = m.group(1).lower()
        keyword = (m.group(2) or "on").lower().replace("in or", "on or")
        if keyword == "in":
            keyword = "on"
        when = resolve_date(m.group(3), today)
        missing = field == "starts"     # `filterResultIfFieldMissing`

        def matches(d: tf.TaskDate | None) -> bool:
            if d is None:
                return missing
            if d.value is None:         # an invalid moment compares false
                return False
            return {
                "before": d.value < when,
                "after": d.value > when,
                "on or before": d.value <= when,
                "on or after": d.value >= when,
                "on": d.value == when,
            }[keyword]

        if field == "happens":
            # `HappensDateField.getFilter`: any of the three dates, where a
            # missing one never matches.
            return Filter(text, lambda e: any(d is not None and matches(d) for d in e.task.happens_dates()))
        return Filter(text, lambda e: matches(_date_of(e, field)))

    raise QueryError(
        f"the task tools cannot apply the Tasks query line {text!r}. Supported: done, not done, "
        "status.type is …, path/description includes …, tags include …, due/scheduled/starts/happens "
        "before|after|on|on or before|on or after <date>, has/no … date, priority is [above|below|not] …"
    )


def parse_global_query(query: str, today: date, *, global_filter: str = "") -> list[Filter]:
    """The plugin's global query, line by line; `#` comment lines skipped."""
    filters = []
    for raw in query.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            filters.append(parse_filter_line(line, today, global_filter=global_filter))
        except QueryError as exc:
            raise QueryError(f"the Tasks plugin's global query cannot be applied: {exc}") from None
    return filters


def query_lines(
    *,
    path: str | None,
    status: str,
    status_types: list[str] | None,
    tags: list[str] | None,
    exclude_tags: list[str] | None,
    area_tag: str | None,
    due: str | None,
    scheduled: str | None,
    starts: str | None,
    happens: str | None,
    priority: str | None,
    text: str | None,
) -> list[str]:
    """The Tasks query lines `list_tasks`' parameters stand for, in this order."""
    lines: list[str] = []
    if status == "not_done":
        lines.append("not done")
    elif status == "done":
        lines.append("done")
    elif status != "any":
        raise QueryError(f"status must be one of {', '.join(STATUS_CHOICES)} (got {status!r})")
    if status_types:
        parts = [f"status.type is {t.upper()}" for t in status_types]
        lines.append(parts[0] if len(parts) == 1 else " OR ".join(f"({p})" for p in parts))
    if path:
        lines.append(f"path includes {path}")
    if area_tag:
        lines.append(f"tags include {area_tag}")
    for tag in tags or ():
        lines.append(f"tags include {tag}")
    for tag in exclude_tags or ():
        lines.append(f"tags do not include {tag}")
    for name, value in (("due", due), ("scheduled", scheduled), ("starts", starts), ("happens", happens)):
        if value:
            lowered = value.strip().lower()
            if lowered in ("has", "any"):
                lines.append(f"has {'start' if name == 'starts' else name} date")
            elif lowered in ("none", "no"):
                lines.append(f"no {'start' if name == 'starts' else name} date")
            else:
                lines.append(f"{name} {value.strip()}")
    if priority:
        lines.append(f"priority is {priority.strip()}")
    if text:
        lines.append(f"description includes {text}")
    return lines


def run_query(
    entries: list[Entry],
    filters: list[Filter],
    *,
    sort: str,
    limit: int | None,
    today: date,
    rng: random.Random,
) -> tuple[list[Entry], int]:
    """Filter, sort as the plugin does, then limit. Returns `(page, total)`."""
    matched = [e for e in entries if all(f.test(e) for f in filters)]

    def default(e: Entry) -> tuple:
        return tf.default_sort_key(e.task, e.path, e.line_number, today)

    if sort == "default":
        matched.sort(key=default)
    elif sort == "due":
        matched.sort(key=lambda e: (tf.due_sort_key(e.task), default(e)))
    elif sort == "priority":
        matched.sort(key=lambda e: (int(e.task.priority), default(e)))
    elif sort == "random":
        rng.shuffle(matched)
    else:
        raise QueryError(f"sort must be one of {', '.join(SORTS)} (got {sort!r})")
    total = len(matched)
    return (matched if limit is None else matched[:limit]), total


# --- areas -------------------------------------------------------------------

AREA_PREFIX = "area/"
#: An area's `description` frontmatter is read up to this many characters.
AREA_DESCRIPTION_MAX = 300
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


@dataclass(frozen=True)
class _Declaration:
    path: str
    tag: str            # "area/<slug>", or the raw value when invalid
    valid: bool
    #: The main note's one-line `description` frontmatter, or None.
    description: str | None = None

    @property
    def folder(self) -> str:
        parent = str(PurePosixPath(self.path).parent)
        return "" if parent == "." else parent

    @property
    def stem(self) -> str:
        return PurePosixPath(self.path).stem


class AreaIndex:
    """The areas the vault declares: each main note's `area_tag` frontmatter.

    The tag's single home is the area's main note (owner's decision); its
    folder is the folder that note sits in. Resolution never invents a tag and
    never guesses between two declarations.
    """

    def __init__(self, declarations: list[_Declaration]):
        self._declarations = declarations

    @classmethod
    def from_rows(cls, rows) -> "AreaIndex":
        out = []
        for path, frontmatter in rows:
            value = (frontmatter or {}).get("area_tag")
            if value is None:
                continue
            valid = isinstance(value, str) and value.startswith(AREA_PREFIX) and bool(_SLUG.match(value[len(AREA_PREFIX):]))
            description = (frontmatter or {}).get("description")
            description = description.strip().splitlines()[0][:AREA_DESCRIPTION_MAX] if (
                isinstance(description, str) and description.strip()) else None
            out.append(_Declaration(path, value if isinstance(value, str) else repr(value), valid, description))
        return cls(out)

    def known(self) -> list[str]:
        return sorted({d.tag for d in self._declarations if d.valid})

    def described(self) -> list[tuple[str, str, str | None]]:
        """`(tag, main note path, description)` for every valid declaration, by tag."""
        return sorted((d.tag, d.path, d.description) for d in self._declarations if d.valid)

    def lookup(self, name: str) -> str:
        """`area/<slug>` for a slug, a tag (`#` optional) or a main note's name."""
        wanted = name.strip().lstrip("#")
        lowered = wanted.casefold()
        tag = wanted if wanted.startswith(AREA_PREFIX) else AREA_PREFIX + wanted
        hits = [d for d in self._declarations if d.tag == tag or d.stem.casefold() == lowered]
        invalid = [d for d in hits if not d.valid]
        if invalid:
            raise QueryError(
                f"{invalid[0].path} declares area_tag {invalid[0].tag!r}, which is not of the form "
                "area/<slug> (lowercase letters, digits and hyphens)"
            )
        tags = {d.tag for d in hits}
        if not tags:
            known = ", ".join(self.known()) or "none declared"
            raise QueryError(f"unknown area {name!r}; areas declared by an area_tag: {known}")
        if len(tags) > 1 or len(hits) > 1:
            where = ", ".join(sorted(d.path for d in hits))
            raise QueryError(f"area {name!r} is ambiguous: declared by {where}")
        return tags.pop()

    def overlapping(self, tag: str) -> list[str]:
        """Other declared area tags that `tags include #<tag>` also matches.

        The plugin's tag filter is a substring match, so `#area/agent` also
        selects `#area/agent-personnel`'s tasks. Parity keeps that meaning;
        this names the overlap so the caller is told.
        """
        needle = ("#" + tag).casefold()
        return sorted(d.tag for d in self._declarations
                      if d.valid and d.tag != tag and needle in ("#" + d.tag).casefold())

    def area_for_path(self, path: str) -> str | None:
        """The area whose folder holds `path` (deepest folder wins), or None.

        A main note at the vault root has no folder of its own and claims
        nothing. Two declarations for the same folder are refused.
        """
        best: list[_Declaration] = []
        best_depth = -1
        for d in self._declarations:
            folder = d.folder
            if not folder or not path.startswith(folder + "/"):
                continue
            depth = folder.count("/")
            if depth > best_depth:
                best, best_depth = [d], depth
            elif depth == best_depth:
                best.append(d)
        if not best:
            return None
        if len({d.tag for d in best}) > 1 or len(best) > 1:
            where = ", ".join(sorted(d.path for d in best))
            raise QueryError(f"{path} is in a folder claimed by more than one area_tag ({where}); name the areas explicitly")
        if not best[0].valid:
            raise QueryError(
                f"{best[0].path} declares area_tag {best[0].tag!r}, which is not of the form area/<slug>; "
                "fix it or name the areas explicitly"
            )
        return best[0].tag
