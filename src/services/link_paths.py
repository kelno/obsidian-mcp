"""Bare-name wikilinks written through the note tools get the note's full path.

The vault's rule: a wikilink names its note by full vault path, with a
display name (`[[Areas/Areas|Areas]]`), never a bare name (`[[Areas]]`): a
bare name resolves to whichever note of that name Obsidian picks, and an old
note with the same name has taken links that way. So the note writers
(`edit_note`, `create_note`, `write_file` on a `.md`) rewrite, on the lines a
write adds or changes only:

- a wikilink or note embed whose target is a bare name (no `/`) naming
  exactly one note (case-insensitively, as Obsidian matches): the target
  becomes the note's path without `.md`, anchors (`#Heading`, `#^block`) and
  display text kept; a link with no display text gets the one Obsidian showed
  (`Areas`, or `Areas > Heading`), so it reads the same. An embed gets no
  display text (on an embed it would mean something else);
- nothing else. A bare name naming several notes, or none, is flagged (the
  latter may be a note still to create), and so is a full path naming no note;
  an embed of a file that is not a note (`![[photo.png]]`) is left alone. A
  path in code formatting right after a link (`` [[X/X|X]] (`X/X.md`) ``)
  repeats what the link holds and is flagged, never changed.

Code spans, fenced blocks (inside quotes and callouts too), the frontmatter
and checkbox lines (a task's line is the task tools' to change) are skipped.
Lines are compared as the task check compares them: line endings and
surrounding spaces don't make a line new.
In a table the pipe before the display text is escaped (`\\|`); a link there
keeps or gets `\\|`. Lines the write leaves as they were are never touched.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field

from src.services.links import FULL_NOTE, mask_code

#: A wikilink or embed: the move rewrite's grammar, target first.
_WIKILINK_RE = re.compile(
    r"(?P<embed>!)?\[\[(?P<target>[^\[\]\|#\n]++)"
    r"(?:#(?P<anchor>[^\[\]\|\n]*+))?"
    r"(?:\|(?P<alias>[^\[\]\n]*+))?\]\]"
)
#: Right after a link: optional spaces and an opening bracket or a colon or dash, then a code span.
_PATH_AFTER_RE = re.compile(r"[ \t]*(?:[(:—–-][ \t]*)?`(?P<code>[^`\n]+)`")
#: Rewrites and flags listed per write; past it, a count.
MAX_LISTED = 30


@dataclass
class LinkReport:
    rewritten: list[tuple[int, str, str]] = field(default_factory=list)   # (line, old, new)
    ambiguous: list[tuple[int, str, list[str]]] = field(default_factory=list)  # (line, link, candidate paths)
    unresolved: list[tuple[int, str]] = field(default_factory=list)
    path_after: list[tuple[int, str]] = field(default_factory=list)       # (line, code span)

    def __bool__(self) -> bool:
        return bool(self.rewritten or self.ambiguous or self.unresolved or self.path_after)


class NoteIndex:
    """The vault's notes, by path and by name, matched without regard to case.

    Lower-cased as JavaScript's `toLowerCase` does (Python's `lower`, not
    `casefold`, which would also match `Strasse` to `Straße`). Two paths
    differing only by case are both kept, so a name they share is ambiguous.
    """

    def __init__(self, paths):
        notes = sorted({p for p in paths if p.lower().endswith(".md")})
        self.paths = {p.lower() for p in notes}
        self.by_name: dict[str, list[str]] = {}
        for p in notes:
            self.by_name.setdefault(os.path.splitext(os.path.basename(p))[0].lower(), []).append(p)

    def named(self, name: str) -> list[str]:
        return sorted(self.by_name.get(name.lower(), []))

    def has_path(self, target: str) -> bool:
        t = target.lower()
        return (t if t.endswith(".md") else t + ".md") in self.paths


def _same(line: str) -> str:
    """A line as compared between old and new: its line ending, a BOM and surrounding spaces don't count."""
    return line.rstrip("\r").lstrip("﻿").strip()


def _changed_lines(old: str | None, new: str) -> set[int]:
    """0-based indexes of `new`'s lines that `old` does not hold (compared as the task check compares)."""
    left = Counter(_same(line) for line in old.split("\n")) if old is not None else Counter()
    out = set()
    for i, line in enumerate(new.split("\n")):
        key = _same(line)
        if left[key] > 0:
            left[key] -= 1
        else:
            out.add(i)
    return out


def _frontmatter_lines(lines: list[str]) -> int:
    """How many leading lines a YAML frontmatter block takes (links there are left alone)."""
    if not lines or lines[0].lstrip("﻿").rstrip("\r") != "---":
        return 0
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r") in ("---", "..."):
            return i + 1
    return 0


_QUOTE_PREFIX_RE = re.compile(r"^[ \t]*(?:>[ \t]?)+")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


def _quoted_code_lines(lines: list[str]) -> set[int]:
    """Lines inside a fenced block within a quote or callout, which the link masker does not see."""
    out, fence = set(), None
    for i, line in enumerate(lines):
        prefix = _QUOTE_PREFIX_RE.match(line)
        if not prefix:
            fence = None
            continue
        rest = line[prefix.end():].rstrip("\r")
        m = _FENCE_RE.match(rest)
        if fence is None:
            if m:
                fence = m.group(1)
                out.add(i)
        else:
            out.add(i)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = None
    return out


_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def _in_table(line: str, start: int, end: int) -> bool:
    """Whether a link sits in a table row: a pipe outside it, or a row that opens with one (quotes stripped)."""
    other = line[:start] + line[end:]
    body = line[_QUOTE_PREFIX_RE.match(line).end():] if _QUOTE_PREFIX_RE.match(line) else line
    return bool(_UNESCAPED_PIPE_RE.search(other)) or body.lstrip().startswith("|")


def _is_note_target(name: str, index: NoteIndex) -> bool:
    """Whether a bare target means a note: no extension, `.md`, or a note's own name with a dot in it."""
    base, ext = os.path.splitext(name)
    return not ext or ext.lower() == ".md" or bool(index.named(name))


def needs_check(old: str | None, new: str) -> bool:
    """Whether any line the write adds or changes holds `[[` (else no note list is needed)."""
    if "[[" not in new:
        return False
    lines = new.split("\n")
    return any("[[" in lines[i] for i in _changed_lines(old, new))


#: A checkbox list item, quoted or not: a task's line is changed by the task tools only.
_CHECKBOX_RE = re.compile(r"^[ \t]*(?:>[ \t]?)*[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]+\[.\]")


def rewrite(old: str | None, new: str, index: NoteIndex) -> tuple[str, LinkReport]:
    """`new` with its bare-name links given full paths, on changed lines only, and the report.

    Checkbox lines are never rewritten: a task's line is changed by the task
    tools only, and the task check compares lines the same way.
    """
    report = LinkReport()
    if "[[" not in new:
        return new, report
    lines = new.split("\n")
    changed = _changed_lines(old, new) - set(range(_frontmatter_lines(lines))) - _quoted_code_lines(lines)
    masked = mask_code(new, context=FULL_NOTE)
    # The masker blanks a fenced block whole, its line breaks included: put
    # them back so masked lines align with the note's.
    masked = "".join("\n" if c == "\n" else m for c, m in zip(new, masked))
    masked_lines = masked.split("\n")
    for i in sorted(changed):
        line, masked = lines[i], masked_lines[i]
        if "[[" not in masked or _CHECKBOX_RE.match(line):
            continue
        pieces, last = [], 0
        for m in _WIKILINK_RE.finditer(masked):
            start, end = m.span()
            raw = line[start:end]
            if raw != masked[start:end]:
                continue                              # code inside or across the link: not touched
            target = m.group("target")
            anchor, alias = m.group("anchor"), m.group("alias")
            escaped = False                           # `[[X\|Y]]` in a table: the pipe is escaped
            if alias is not None and anchor is not None and anchor.endswith("\\"):
                anchor, escaped = anchor[:-1], True
            elif alias is not None and target.endswith("\\"):
                target, escaped = target[:-1], True
            name = target.strip()
            embed = bool(m.group("embed"))
            after = _PATH_AFTER_RE.match(line, end)
            if after and ("/" in after.group("code") or after.group("code").lower().endswith(".md")):
                report.path_after.append((i + 1, f"`{after.group('code')}`"))
            if "/" in name:
                if _is_note_target(os.path.basename(name), index) and not index.has_path(name):
                    report.unresolved.append((i + 1, raw))
                continue
            if not _is_note_target(name, index):
                continue                              # an attachment: `![[photo.png]]`
            bare = name[:-3] if name.lower().endswith(".md") else name
            candidates = index.named(bare)
            if len(candidates) != 1:
                if candidates:
                    report.ambiguous.append((i + 1, raw, candidates))
                else:
                    report.unresolved.append((i + 1, raw))
                continue
            path = candidates[0][:-3]
            pipe = "\\|" if escaped or _in_table(line, start, end) else "|"
            link = ("!" if embed else "") + "[[" + path + (f"#{anchor}" if anchor is not None else "")
            if alias is not None:
                link += pipe + alias
            elif not embed:
                # The text Obsidian showed: the name as written, then each anchor part.
                shown = name + "".join(f" > {part}" for part in (anchor or "").split("#") if part)
                link += pipe + shown
            link += "]]"
            pieces.append(line[last:start] + link)
            last = end
            report.rewritten.append((i + 1, raw, link))
        if pieces:
            lines[i] = "".join(pieces) + line[last:]
    return "\n".join(lines), report


def shifted(report: LinkReport, by: int) -> LinkReport:
    """The report with its line numbers moved by `by` (lines the agent-edit stamp added above)."""
    if not by:
        return report
    return LinkReport(
        rewritten=[(n + by, a, b) for n, a, b in report.rewritten],
        ambiguous=[(n + by, a, c) for n, a, c in report.ambiguous],
        unresolved=[(n + by, a) for n, a in report.unresolved],
        path_after=[(n + by, a) for n, a in report.path_after],
    )


def render(report: LinkReport, *, dry_run: bool = False) -> str:
    """The result's blocks, in the style of the ledger-ref blocks; "" when there is nothing."""
    parts = []

    def listed(items, fmt):
        out = [fmt(item) for item in items[:MAX_LISTED]]
        if len(items) > MAX_LISTED:
            out.append(f"- and {len(items) - MAX_LISTED} more")
        return "\n".join(out)

    if report.rewritten:
        verb = "would get" if dry_run else "got"
        parts.append(
            f"links_rewritten: these bare-name links {verb} the note's full path, as the vault's link rule asks "
            "(they show the same text in Obsidian):\n"
            + listed(report.rewritten, lambda r: f"- line {r[0]}: {r[1]} → {r[2]}"))
    flags = ([("ambiguous", r) for r in report.ambiguous] + [("unresolved", r) for r in report.unresolved]
             + [("path_after", r) for r in report.path_after])
    if flags:
        def flag(item):
            kind, r = item
            if kind == "ambiguous":
                return (f"- line {r[0]}: {r[1]} names several notes ({', '.join(r[2][:5])}"
                        + (", …" if len(r[2]) > 5 else "") + "): write the full path of the one you mean.")
            if kind == "unresolved":
                return f"- line {r[0]}: {r[1]} names no note (fine if it is a note still to create)."
            return (f"- line {r[0]}: {r[1]}, in code formatting after a link, repeats a path the link already "
                    "holds: drop it.")
        parts.append("links_flagged: left as written, check each:\n" + listed(flags, flag))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


#: The line comparison and frontmatter span, shared with `title_heading` and
#: the plugin runner (`src/plugins`) so all pick a write's changed lines the
#: same way.
changed_lines = _changed_lines
frontmatter_lines = _frontmatter_lines
