"""Dated note names, checked where the vault says so (`note_names` on folder notes).

The vault's rule: a dated note's name starts with its date, `YYYY-MM-DD Title`;
an unknown part is `X` (`2025-10-XX`, `2024-XX-XX`), never `00`; one date
only, at the start. Notes that describe the present carry no date.

**Where it applies is declared in the vault, never in code.** A folder note
(`<folder>/<folder>.md`) may carry the frontmatter `note_names: dated` or
`note_names: undated` (read from the note on disk, so a change counts at
once). For a note at `<dir>/<name>.md`, walking up from
`<dir>` (itself included) towards the vault root, the first of these decides,
level by level:

1. the folder's own folder note, when it sets `note_names`;
2. else, when the folder mirrors the top level, the setting the mirrored
   top-level path resolves to (by these same rules). Areas and the archive
   copy the vault's top-level layout: `Areas/<area>/<rest>` mirrors `<rest>`,
   and `Archive/<rest>` mirrors `<rest>` (so `Archive/Areas/<area>/Log`
   mirrors `Areas/<area>/Log`, which mirrors `Log`).

So a nearer folder note always wins over a mirror further up, and a mirror
wins over a farther folder note. No setting anywhere: no check. Any value
other than `dated` or `undated` counts as none.

Not checked: a folder note itself (`Log/Log.md`), and a name starting with
"Untitled" (Obsidian's default, to be named by whoever finds it).
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from dataclasses import dataclass
from typing import Callable

from src import plugins
from src.config import MAX_NOTE_BYTES

logger = logging.getLogger(__name__)

#: The refusal code (listed in `src/services/refusals.py`).
CODE = "note_name_date"

DATED, UNDATED = "dated", "undated"
FIELD = "note_names"
#: The mirror roots: `Areas/<area>/…` and `Archive/…` copy the vault's top level.
AREAS, ARCHIVE = "Areas", "Archive"

#: A date written right: `2025-03-28`, `2025-10-XX`, `2024-XX-XX`, `XXXX-XX-XX` (year unknown too).
_DATE = r"(?P<y>[0-9]{4}|XXXX)-(?P<m>[0-9]{2}|XX)-(?P<d>[0-9]{2}|XX)"
_GOOD_START_RE = re.compile(rf"^{_DATE}(?:(?P<sep> )(?P<rest>.+))?$")
#: A second date right after the first, written strictly: `2025-03-28 Title`.
_STRICT_DATE_START_RE = re.compile(rf"^{_DATE}(?: (?P<rest>.+))?$")
#: A date in a loose form at the start: `2025_03_28`, `2025.03.28`, `20250328`, `2025-03-28-Title`, lower `xx`.
_LOOSE_START_RE = re.compile(
    r"^(?P<y>[0-9]{4})[-_.]?(?P<m>[0-9]{2}|[Xx]{2})[-_.]?(?P<d>[0-9]{2}|[Xx]{2})(?:[ _\-.]+(?P<rest>.*))?$")
#: A date at the very end: `Title 2025-03-28`.
_TRAILING_RE = re.compile(rf"^(?P<title>.+?)[ _-]+{_DATE}$")
#: Any date-like start, for the undated check: a year, a month and a day in range.
_ANY_DATE_START_RE = re.compile(r"^[0-9]{4}[-_.](?:0[1-9]|1[0-2]|XX)[-_.](?:0[1-9]|[12][0-9]|3[01]|XX)(?![0-9])")


@dataclass(frozen=True)
class Setting:
    value: str          # dated or undated
    source: str         # the folder note that sets it
    mirrored: str | None = None   # the top-level path the note's folder mirrors, when it came that way


@dataclass(frozen=True)
class Problem:
    message: str
    corrected: str | None = None   # the name as it should be, when it can be derived


def folder_note(folder: str) -> str:
    return f"{folder}/{posixpath.basename(folder)}.md"


class Rules:
    """`note_names` as the vault's folder notes declare it.

    `lookup(folder_note_path)` returns that folder note's `note_names` value,
    or None when the note is absent or sets none (the tools read the note
    from disk; the tests pass a dict's `get`). Each folder note is looked up
    at most once per `Rules`.
    """

    def __init__(self, lookup: Callable[[str], object]):
        self._lookup = lookup
        self._seen: dict[str, str | None] = {}

    def _own(self, folder: str) -> str | None:
        path = folder_note(folder)
        if path not in self._seen:
            value = self._lookup(path)
            self._seen[path] = value if value in (DATED, UNDATED) else None
        return self._seen[path]

    def _at(self, level: str, _depth: int = 0) -> Setting | None:
        """The setting decided at exactly `level`: its folder note, else its mirror at the same level."""
        own = self._own(level)
        if own:
            return Setting(own, folder_note(level))
        mirrored = _mirror(level)
        if mirrored and _depth < 8:
            found = self._at(mirrored, _depth + 1)
            if found:
                return Setting(found.value, found.source, found.mirrored or mirrored)
        return None

    def resolve(self, folder: str) -> Setting | None:
        """Walking up from `folder`, the first level that decides (see the module's docstring)."""
        level = folder.strip("/")
        while level:
            found = self._at(level)
            if found:
                return found
            level = posixpath.dirname(level)
        return None


def _mirror(folder: str) -> str | None:
    """The top-level path `folder` mirrors, or None: `Areas/<area>/<rest>` → `<rest>`, `Archive/<rest>` → `<rest>`."""
    parts = folder.split("/")
    if parts[0] == AREAS and len(parts) >= 3:
        return "/".join(parts[2:])
    if parts[0] == ARCHIVE and len(parts) >= 2:
        return "/".join(parts[1:])
    return None


def exempt(path: str) -> bool:
    """A folder note, or an "Untitled" note."""
    folder, name = posixpath.split(path)
    stem = name[:-3] if name.lower().endswith(".md") else name
    return bool(folder) and stem == posixpath.basename(folder) or stem.lower().startswith("untitled")


def archived_unchanged(from_path: str, to_path: str) -> bool:
    """A move into `Archive/` that keeps the note's name: archiving a record as it was, not naming one."""
    return to_path.startswith(ARCHIVE + "/") and posixpath.basename(from_path) == posixpath.basename(to_path)


def _valid(y: str, m: str, d: str) -> str | None:
    """Why a date's parts are wrong, or None."""
    if y == "XXXX" and (m != "XX" or d != "XX"):
        return "a known month or day with an unknown year"
    if m == "XX" and d != "XX":
        return "a known day with an unknown month"
    if m != "XX" and not 1 <= int(m) <= 12:
        return f"month {m} does not exist"
    if d != "XX":
        import calendar

        days = calendar.monthrange(int(y) if y != "XXXX" else 2000, int(m))[1] if m != "XX" else 31
        if not 1 <= int(d) <= days:
            return f"day {d} does not exist" + (f" in {y}-{m}" if m != "XX" else "")
    return None


def _fix_parts(y: str, m: str, d: str) -> tuple[str, str, str]:
    m, d = m.upper(), d.upper()
    return y, "XX" if m == "00" else m, "XX" if d == "00" else d


def check_dated(stem: str) -> Problem | None:
    """What is wrong with a name in a dated folder, or None when it is right.

    A corrected name is offered only when it can be derived without changing
    what the name says, and only when it passes this check itself.
    """
    problem = _check(stem)
    if problem is not None and problem.corrected is not None and _check(problem.corrected) is not None:
        return Problem(problem.message)
    return problem


def _check(stem: str) -> Problem | None:
    pattern = ("`YYYY-MM-DD Title` (an unknown part as XX, as in `2025-10-XX Title`, `2024-XX-XX Title` or "
               "`XXXX-XX-XX Title`; never 00)")
    good = _GOOD_START_RE.match(stem)
    if good:
        y, m, d, rest = good.group("y"), good.group("m"), good.group("d"), good.group("rest")
        if m == "00" or d == "00":
            fy, fm, fd = _fix_parts(y, m, d)
            why = _valid(fy, fm, fd)
            fixed = f"{fy}-{fm}-{fd}" + (f" {rest}" if rest else "")
            return Problem("an unknown month or day is written XX, never 00"
                           + (f"; and {why}." if why else "."), None if why else fixed)
        if why := _valid(y, m, d):
            return Problem(f"the date is not a real one: {why}.")
        second = _STRICT_DATE_START_RE.match(rest) if rest else None
        if second and _valid(*_fix_parts(second.group("y"), second.group("m"), second.group("d"))) is None:
            tail = second.group("rest")
            same = (second.group("y"), second.group("m"), second.group("d")) == (y, m, d)
            return Problem(
                "the name carries a second date right after the first: one date only, at the start"
                + ("." if same else " (for a span of days, name it by its first day and put the end in the title "
                                     "or the note)."),
                (f"{y}-{m}-{d} {tail}" if tail else f"{y}-{m}-{d}") if same else None)
        return None
    loose = _LOOSE_START_RE.match(stem)
    if loose:
        y, m, d = _fix_parts(loose.group("y"), loose.group("m"), loose.group("d"))
        rest = (loose.group("rest") or "").strip()
        fixed = f"{y}-{m}-{d}" + (f" {rest}" if rest else "")
        if _valid(y, m, d) is None and fixed != stem:
            return Problem(f"the date at the start is not written {pattern}.", fixed)
    trailing = _TRAILING_RE.match(stem)
    if trailing and not _TRAILING_RE.match(trailing.group("title")):
        y, m, d = _fix_parts(trailing.group("y"), trailing.group("m"), trailing.group("d"))
        if _valid(y, m, d) is None:
            return Problem("the date is at the end: it goes at the start.",
                           f"{y}-{m}-{d} {trailing.group('title').strip()}")
    return Problem(f"the name does not start with its date: it should read {pattern}.")


def starts_with_date(stem: str) -> bool:
    return bool(_ANY_DATE_START_RE.match(stem))


async def check_name(note: plugins.NoteName) -> plugins.NameCheck | None:
    """The `check_name` hook: a refusal in a dated folder, a flag in an undated one, else None.

    The setting comes from the folder notes' `note_names` frontmatter, read
    from disk walking up the path (a few small, bounded reads). A folder note
    that cannot be read counts as setting nothing (logged at debug). Not
    checked: a move into the archive that keeps the note's name (that records
    it as it was).
    """
    from src.services.vault import parse_frontmatter_diagnose, read_bytes

    rel, tool = note.path, note.tool
    if not rel.lower().endswith(".md") or exempt(rel):
        return None
    if note.from_path is not None and archived_unchanged(note.from_path, rel):
        return None

    def lookup(path: str):
        try:
            raw = read_bytes(path, note.user_id, MAX_NOTE_BYTES).decode("utf-8")
        except FileNotFoundError:
            return None
        except (ValueError, OSError, UnicodeDecodeError):
            # debug, not warning: a caller drives this path (the D18 permit rule).
            logger.debug("%s: folder note %s could not be read for note_names", tool, path, exc_info=True)
            return None
        frontmatter, _body, _diagnosis = parse_frontmatter_diagnose(raw)
        return (frontmatter or {}).get(FIELD) if isinstance(frontmatter, dict) else None

    folder, name = posixpath.split(rel)
    setting = await asyncio.to_thread(Rules(lookup).resolve, folder)
    if setting is None:
        return None
    stem = name[:-3]
    where = (f"`{FIELD}: {setting.value}` in {setting.source}"
             + (f", which {folder} follows as a mirror of {setting.mirrored}/" if setting.mirrored else ""))
    if setting.value == DATED:
        problem = check_dated(stem)
        if problem is None:
            return None
        fix = (f" Call again with `{posixpath.join(folder, problem.corrected)}.md`." if problem.corrected else
               " Start the name with the date the note is about (the day it happened); a note with no date of its"
               " own, kept current, belongs in an undated folder (`Reference/`, say).")
        return plugins.NameCheck(refusal=plugins.Refusal(CODE, (
            f"{tool}: notes in {folder}/ are named with their date first ({where}), and in `{name}` "
            f"{problem.message}{fix} Nothing was written.")))
    if starts_with_date(stem):
        return plugins.NameCheck(note=(
            f"\n\nnote_name: the note was written, but `{name}` starts with a date, and notes in "
            f"{folder}/ carry none ({where}): they describe the present. Rename it without the date "
            "(move_note) unless the date is part of what it is."))
    return None


TOOL_TEXT = (
    "**Dated note names.** Where a folder note (`<folder>/<folder>.md`) sets `note_names: dated` (for that folder "
    "and the folders below it at any depth, and for a folder under an area or the archive named like a top-level "
    "one, unless a nearer folder note says otherwise), a new note's name must start with the date it is about, "
    "`YYYY-MM-DD Title` (an unknown part as XX, never 00; one date only): otherwise the call is refused with "
    "`note_name_date`, writing nothing, naming the folder note and, when it can, the corrected name. Where it sets "
    "`note_names: undated`, a name starting with a date is written and flagged (`note_name:`). No setting above: "
    "no check."
)

MOVE_TEXT = (
    "The new name is checked against its folder's `note_names` setting, as in `create_note`: in a dated folder a "
    "name without its date first is refused with `note_name_date` (the corrected name given when it can be "
    "derived), before anything moves; in an undated one a name starting with a date is moved and flagged "
    "(`note_name:`). A move into `Archive/` that keeps the note's name is not checked."
)

WRITE_FILE_TEXT = "A new `.md` note's name is checked like `create_note`'s (`note_name_date`)."

GUIDE = "## Note names (this server)\n\nA folder note (`<folder>/<folder>.md`) can declare how its notes are named, in its frontmatter: `note_names: dated` or `note_names: undated`. It holds for that folder and the folders below it, at any depth. A folder under an area (`Areas/<area>/Log`) or anywhere under `Archive/` (`Archive/Areas/<area>/Log` too) whose name is a top-level folder's follows that top-level folder's setting, with no copy of its folder note needed, unless a nearer folder note sets its own. Where no folder note above sets anything, names are not checked. In a dated folder, `create_note`, `move_note` and `write_file` (a new `.md`) refuse a name that doesn't start with its date (the date the note is about), `YYYY-MM-DD Title` (an unknown part as `XX`, never `00`; one date only), with `note_name_date`, naming the folder note and the corrected name when it can be derived; nothing is written, so call again with the right name. A note with no date of its own belongs in an undated folder. In an undated folder, a name starting with a date is written and flagged (`note_name:`), not refused."
