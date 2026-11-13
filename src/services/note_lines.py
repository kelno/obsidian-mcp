"""Which lines of a note a write adds or changes, and where its frontmatter ends.

Shared by everything that acts only on "the lines this write adds or
changes": the plugin runner's scope guard (`src/plugins`), the plugins' own
rewrites, and the rule-edit nudge. One comparison, so they all agree on which
lines a write touched.
"""

from __future__ import annotations

from collections import Counter


def compare_key(line: str) -> str:
    """A line as compared between old and new: its line ending, a BOM and surrounding spaces don't count."""
    return line.rstrip("\r").lstrip("﻿").strip()


def changed_lines(old: str | None, new: str) -> set[int]:
    """0-based indexes of `new`'s lines that `old` does not hold.

    Lines are split on LF and compared as a multiset by `compare_key`: a line
    moved elsewhere unchanged is not changed. `old` None (a new note) makes
    every line changed.
    """
    left = Counter(compare_key(line) for line in old.split("\n")) if old is not None else Counter()
    out = set()
    for i, line in enumerate(new.split("\n")):
        key = compare_key(line)
        if left[key] > 0:
            left[key] -= 1
        else:
            out.add(i)
    return out


def frontmatter_lines(lines: list[str]) -> int:
    """How many leading lines a YAML frontmatter block takes (0 when there is none)."""
    if not lines or lines[0].lstrip("﻿").rstrip("\r") != "---":
        return 0
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r") in ("---", "..."):
            return i + 1
    return 0
