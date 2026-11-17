"""What counts as a rule file in this vault: one definition, a note in the vault, read at each use.

The vault's rules for agents live in whole files (the notes under `_/agent/`,
the root `CLAUDE.md`, the hook scripts) and in one fixed section of any note
(`## For agents`). The list is data, in the note `RULE_FILES_PATH`, so the
connector (the rule-edit nudge, `open_area`'s `for_agents`) and the vault's
local edit guard read the same definition and nothing keeps two copies in step.

The note holds one fenced code block whose info string is `rule-files`. Each
non-blank line in it is either:

- a path pattern, vault-relative: `fnmatch` with `*` matching across `/`,
  case ignored. A matching file is a rule file as a whole;
- a level-2 heading (`## For agents`): that section, in any note, up to the
  next heading of level 1 or 2, is a rule section; only its lines are rules.

The vault's `_/agent/Outside dependencies.md` records that this plugin
depends on the note's path and the block's format.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from src.config import MAX_NOTE_BYTES
from src.services.vault import read_bytes

#: The note holding the definition.
RULE_FILES_PATH = "_/Rule files.md"
#: The info string of the code block holding it.
BLOCK_INFO = "rule-files"

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([^`\s]*)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


class RuleFilesError(Exception):
    """The note holds no usable `rule-files` block."""


@dataclass(frozen=True)
class RuleFiles:
    #: Path patterns, as written.
    patterns: tuple[str, ...]
    #: Section headings, without the `## `.
    sections: tuple[str, ...]

    def is_rule_file(self, path: str) -> bool:
        """True when the vault-relative `path` matches a pattern (case ignored)."""
        p = path.replace("\\", "/").lstrip("/").casefold()
        return any(fnmatch.fnmatchcase(p, pattern.casefold()) for pattern in self.patterns)


def parse(text: str) -> RuleFiles:
    """The definition in a `rule-files` code block of `text`; RuleFilesError when there is none or it is empty."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for i, line in enumerate(lines):
        opener = _FENCE_RE.match(line)
        if not opener or opener.group(2).casefold() != BLOCK_INFO:
            continue
        fence = opener.group(1)
        patterns, sections = [], []
        for body in lines[i + 1:]:
            stripped = body.strip()
            if stripped.startswith(fence[0] * len(fence)) and not stripped.strip(fence[0]):
                break
            if not stripped:
                continue
            heading = _HEADING_RE.match(stripped)
            if heading and len(heading.group(1)) == 2 and heading.group(2).strip():
                sections.append(heading.group(2).strip())
            elif heading:
                raise RuleFilesError(f"{stripped!r} is not a level-2 heading")
            else:
                patterns.append(stripped)
        else:
            raise RuleFilesError(f"the `{BLOCK_INFO}` code block is not closed")
        if not patterns and not sections:
            raise RuleFilesError(f"the `{BLOCK_INFO}` code block is empty")
        return RuleFiles(tuple(patterns), tuple(sections))
    raise RuleFilesError(f"it has no `{BLOCK_INFO}` code block")


def read(user_id) -> RuleFiles:
    """The definition, read now from the caller's vault.

    Raises FileNotFoundError, ValueError or OSError (the note can't be read)
    and RuleFilesError (it holds no usable block).
    """
    return parse(read_bytes(RULE_FILES_PATH, user_id, MAX_NOTE_BYTES).decode("utf-8", errors="replace"))


def section_ranges(lines: list[str], headings: tuple[str, ...], start: int = 0) -> list[tuple[int, int]]:
    """`[(first, end)]`: 0-based line ranges of every level-2 section of `lines` named in `headings`.

    `first` is the line after the heading, `end` the next heading of level 1
    or 2 (or the end). Headings inside fenced code are not headings, and
    lines before `start` (the frontmatter) are not read. Names compare
    case-insensitively.
    """
    wanted = {h.strip().casefold() for h in headings}
    out: list[tuple[int, int]] = []
    open_at, fence = None, None
    for i in range(start, len(lines)):
        line = lines[i].rstrip("\r")
        fence_match = re.match(r"\s*(`{3,}|~{3,})", line)
        if fence is None and fence_match:
            fence = fence_match.group(1)[0]
            continue
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence:
                fence = None
            continue
        match = _HEADING_RE.match(line)
        if not match or len(match.group(1)) > 2:
            continue
        if open_at is not None:
            out.append((open_at, i))
            open_at = None
        if len(match.group(1)) == 2 and match.group(2).strip().casefold() in wanted:
            open_at = i + 1
    if open_at is not None:
        out.append((open_at, len(lines)))
    return out


def split_sections(text: str, headings: tuple[str, ...]) -> tuple[str, list[str]]:
    """`(text without the sections, [each section's body])`, for the sections `section_ranges` finds."""
    lines = text.split("\n")
    ranges = section_ranges(lines, headings)
    if not ranges:
        return text, []
    bodies = ["\n".join(lines[first:end]) for first, end in ranges]
    drop = set()
    for first, end in ranges:
        drop.update(range(first - 1, end))   # the heading line too
    rest = "\n".join(line for i, line in enumerate(lines) if i not in drop)
    return rest, bodies
