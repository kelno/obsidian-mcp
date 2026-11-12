"""Private folders: the one definition every tool uses (fork, owner's decision P98).

A vault may keep some folders private: their notes stay out of what agents
come across, yet an agent can reach them when asked. `PRIVATE_FOLDER_NAMES`
lists the folder names that make a folder private, as globs over one folder
name (`*` any run of characters, `?` one character, everything else literal),
matched without regard to case against every folder in a note's path, at any
depth; everything under a matching folder is private. A note's own file name
never counts: `Plans (private).md` at the root is not in a private folder.

Empty (the default), nothing is private, and the tools show no
`include_private` argument (`src/mcp_server/server.py`).

The same pattern is applied in Python (`is_private`) and in PostgreSQL
(`sql_predicate`, a case-insensitive regular expression on
`notes_metadata.file_path`), so a search can leave private notes out inside
its own query and still fill its limit. The regex uses only constructs both
engines read the same way: letters and digits as they are, every other
character escaped with a backslash (literal in both), `[^/]`, plain groups
and alternation.
"""

from __future__ import annotations

import re
from functools import lru_cache

from src.config import settings

#: The result line a search ends with when it left private notes out.
HIDDEN_LINE = "{n} {matches} in private folders not shown."


def _fragment(glob: str) -> str:
    out = []
    for ch in glob:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch.isalnum():
            out.append(ch)
        else:
            # A backslash before a character that is not a letter or digit is
            # that character, literally, in Python's `re` and PostgreSQL's ARE.
            out.append("\\" + ch)
    return "".join(out)


@lru_cache(maxsize=8)
def _regex_for(names: tuple[str, ...]) -> str | None:
    names = tuple(n for n in names if n)
    if not names:
        return None
    alternatives = "|".join(_fragment(n) for n in names)
    # A folder is a path component followed by `/`: the file name never is.
    return f"(^|/)({alternatives})/"


def patterns() -> tuple[str, ...]:
    """The configured folder-name globs; empty when the feature is off."""
    return tuple(settings.private_folder_names or ())


def enabled() -> bool:
    """Whether this deployment has private folders at all."""
    return bool(patterns())


def path_regex() -> str | None:
    """The regular expression a private note's vault-relative path matches."""
    return _regex_for(patterns())


@lru_cache(maxsize=8)
def _compiled(regex: str) -> re.Pattern:
    return re.compile(regex, re.IGNORECASE)


def is_private(path: str) -> bool:
    """A note (or file) under a private folder. Always False when the feature is off."""
    regex = path_regex()
    if regex is None:
        return False
    return _compiled(regex).search(path.replace("\\", "/")) is not None


def sql_predicate(column):
    """`column ~* regex`: the rows under a private folder. Only call when `enabled()`."""
    regex = path_regex()
    if regex is None:
        raise RuntimeError("private folders are not configured")
    return column.op("~*")(regex)


def hidden_line(count: int) -> str:
    """The line a result ends with: "2 matches in private folders not shown."."""
    return HIDDEN_LINE.format(n=count, matches="match" if count == 1 else "matches")
