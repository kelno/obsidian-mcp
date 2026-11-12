"""Dated note names, checked where the vault says so (`note_names` on folder notes).

The vault's rule: a dated note's name starts with its date, `YYYY-MM-DD Title`;
an unknown part is `X` (`2025-10-XX`, `2024-XX-XX`), never `00`; one date
only, at the start. Notes that describe the present carry no date.

**Where it applies is declared in the vault, never in code.** A folder note
(`<folder>/<folder>.md`) may carry the frontmatter `note_names: dated` or
`note_names: undated`. For a note at `<dir>/<name>.md`, walking up from
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

import posixpath
import re
from dataclasses import dataclass

DATED, UNDATED = "dated", "undated"
FIELD = "note_names"
#: The mirror roots: `Areas/<area>/…` and `Archive/…` copy the vault's top level.
AREAS, ARCHIVE = "Areas", "Archive"

#: A date written right: `2025-03-28`, `2025-10-XX`, `2024-XX-XX`.
_DATE = r"(?P<y>[0-9]{4})-(?P<m>[0-9]{2}|XX)-(?P<d>[0-9]{2}|XX)"
_GOOD_START_RE = re.compile(rf"^{_DATE}(?:(?P<sep> )(?P<rest>.+))?$")
#: A date in a loose form at the start: `2025_03_28`, `2025.03.28`, `20250328`, `2025-03-28-Title`, lower `xx`.
_LOOSE_START_RE = re.compile(
    r"^(?P<y>[0-9]{4})[-_. ]?(?P<m>[0-9]{2}|[Xx]{2})[-_. ]?(?P<d>[0-9]{2}|[Xx]{2})(?:[ _\-.]+(?P<rest>.*))?$")
#: A date at the very end: `Title 2025-03-28`.
_TRAILING_RE = re.compile(rf"^(?P<title>.+?)[ _-]+{_DATE}$")
#: Any date-like start, for the undated check.
_ANY_DATE_START_RE = re.compile(r"^[0-9]{4}[-_.]?(?:[0-9]{2}|[Xx]{2})[-_.]?(?:[0-9]{2}|[Xx]{2})(?![0-9])")


@dataclass(frozen=True)
class Setting:
    value: str          # dated or undated
    source: str         # the folder note that sets it
    mirrored: str | None = None   # the top-level folder the note's folder mirrors, when it came that way


@dataclass(frozen=True)
class Problem:
    message: str
    corrected: str | None = None   # the name as it should be, when it can be derived


def folder_note(folder: str) -> str:
    return f"{folder}/{posixpath.basename(folder)}.md"


class Rules:
    """`note_names` as the vault's folder notes declare it: `{folder: (value, folder note path)}`."""

    def __init__(self, declared: dict[str, str]):
        # `declared`: folder-note path → its `note_names` value.
        self.by_folder: dict[str, tuple[str, str]] = {}
        for path, value in declared.items():
            if value not in (DATED, UNDATED):
                continue
            folder = posixpath.dirname(path)
            if folder and path == folder_note(folder):
                self.by_folder[folder.lower()] = (value, path)

    def resolve(self, folder: str, _depth: int = 0) -> Setting | None:
        folder = folder.strip("/")
        level = folder
        while level and _depth < 16:
            own = self.by_folder.get(level.lower())
            if own:
                return Setting(own[0], own[1])
            mirrored = _mirror(level)
            if mirrored:
                found = self.resolve(mirrored, _depth + 1)
                if found:
                    return Setting(found.value, found.source, found.mirrored or mirrored.split("/")[0])
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
    return bool(folder) and path == folder_note(folder) or stem.lower().startswith("untitled")


def _valid(y: str, m: str, d: str) -> str | None:
    """Why a date's parts are wrong, or None."""
    if m == "XX" and d != "XX":
        return "a known day with an unknown month"
    if m != "XX" and not 1 <= int(m) <= 12:
        return f"month {m} does not exist"
    if d != "XX":
        import calendar

        days = calendar.monthrange(int(y), int(m))[1] if m != "XX" else 31
        if not 1 <= int(d) <= days:
            return f"day {d} does not exist" + (f" in {y}-{m}" if m != "XX" else "")
    return None


def _fix_parts(y: str, m: str, d: str) -> tuple[str, str, str]:
    m, d = m.upper(), d.upper()
    return y, "XX" if m == "00" else m, "XX" if d == "00" else d


def check_dated(stem: str) -> Problem | None:
    """What is wrong with a name in a dated folder, or None when it is right."""
    pattern = "`YYYY-MM-DD Title` (an unknown part as XX, as in `2025-10-XX Title` or `2024-XX-XX Title`)"
    good = _GOOD_START_RE.match(stem)
    if good:
        y, m, d, rest = good.group("y"), good.group("m"), good.group("d"), good.group("rest")
        if m == "00" or d == "00":
            fy, fm, fd = _fix_parts(y, m, d)
            fixed = f"{fy}-{fm}-{fd}" + (f" {rest}" if rest else "")
            return Problem("an unknown month or day is written XX, never 00.", fixed if not _valid(fy, fm, fd) else None)
        if why := _valid(y, m, d):
            return Problem(f"the date is not a real one: {why}.")
        if rest:
            second = _LOOSE_START_RE.match(rest)
            if second and _GOOD_START_RE.match(f"{second.group('y')}-{second.group('m').upper()}-{second.group('d').upper()}"):
                tail = second.group("rest")
                return Problem("the name carries a second date right after the first: one date only, at the start.",
                               f"{y}-{m}-{d} {tail}" if tail else f"{y}-{m}-{d}")
        return None
    loose = _LOOSE_START_RE.match(stem)
    if loose:
        y, m, d = _fix_parts(loose.group("y"), loose.group("m"), loose.group("d"))
        rest = (loose.group("rest") or "").strip()
        fixed = f"{y}-{m}-{d}" + (f" {rest}" if rest else "")
        if _valid(y, m, d) is None and fixed != stem:
            return Problem(f"the date at the start is not written {pattern}.", fixed)
    trailing = _TRAILING_RE.match(stem)
    if trailing:
        y, m, d = _fix_parts(trailing.group("y"), trailing.group("m"), trailing.group("d"))
        if _valid(y, m, d) is None:
            return Problem("the date is at the end: it goes at the start.", f"{y}-{m}-{d} {trailing.group('title').strip()}")
    return Problem(f"the name does not start with its date: it should read {pattern}.")


def starts_with_date(stem: str) -> bool:
    return bool(_ANY_DATE_START_RE.match(stem))
