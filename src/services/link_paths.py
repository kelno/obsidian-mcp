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

Code spans, fenced blocks and the frontmatter are skipped (the same masker as
the link index).
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
    """The vault's notes, by path and by name, matched case-insensitively."""

    def __init__(self, paths):
        self.paths = {p.casefold(): p for p in paths if p.lower().endswith(".md")}
        self.by_name: dict[str, list[str]] = {}
        for p in self.paths.values():
            self.by_name.setdefault(os.path.splitext(os.path.basename(p))[0].casefold(), []).append(p)

    def named(self, name: str) -> list[str]:
        return sorted(self.by_name.get(name.casefold(), []))

    def has_path(self, target: str) -> bool:
        t = target.casefold()
        return (t if t.endswith(".md") else t + ".md") in self.paths


def _changed_lines(old: str | None, new: str) -> set[int]:
    """0-based indexes of `new`'s lines that `old` does not hold as they are."""
    left = Counter((old or "").split("\n")) if old is not None else Counter()
    out = set()
    for i, line in enumerate(new.split("\n")):
        if left[line] > 0:
            left[line] -= 1
        else:
            out.add(i)
    return out


def _frontmatter_lines(lines: list[str]) -> int:
    """How many leading lines a YAML frontmatter block takes (links there are left alone)."""
    if not lines or lines[0].rstrip("\r") != "---":
        return 0
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r") in ("---", "..."):
            return i + 1
    return 0


def _is_note_target(name: str, index: NoteIndex) -> bool:
    """Whether a bare target means a note: no extension, `.md`, or a note's own name with a dot in it."""
    base, ext = os.path.splitext(name)
    return not ext or ext.lower() == ".md" or bool(index.named(name))


def rewrite(old: str | None, new: str, index: NoteIndex) -> tuple[str, LinkReport]:
    """`new` with its bare-name links given full paths, on changed lines only, and the report."""
    report = LinkReport()
    if "[[" not in new:
        return new, report
    changed = _changed_lines(old, new)
    masked = mask_code(new, context=FULL_NOTE)
    # The masker blanks a fenced block whole, its line breaks included: put
    # them back so masked lines align with the note's.
    masked = "".join("\n" if c == "\n" else m for c, m in zip(new, masked))
    masked_lines = masked.split("\n")
    lines = new.split("\n")
    changed -= set(range(_frontmatter_lines(lines)))
    for i in sorted(changed):
        line, masked = lines[i], masked_lines[i]
        if "[[" not in masked:
            continue
        pieces, last = [], 0
        for m in _WIKILINK_RE.finditer(masked):
            start, end = m.span()
            raw = line[start:end]
            target = m.group("target")
            escaped = target.endswith("\\")          # `[[X\|Y]]` in a table: the pipe is escaped
            name = target[:-1] if escaped else target
            name = name.strip()
            embed = bool(m.group("embed"))
            anchor, alias = m.group("anchor"), m.group("alias")
            after = _PATH_AFTER_RE.match(line, end)
            if after and ("/" in after.group("code") or after.group("code").lower().endswith(".md")):
                report.path_after.append((i + 1, after.group(0).strip()))
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
            pipe = "\\|" if escaped or line.lstrip().startswith("|") else "|"
            link = ("!" if embed else "") + "[[" + path + (f"#{anchor}" if anchor is not None else "")
            if alias is not None:
                link += pipe + alias
            elif not embed:
                link += pipe + bare + (f" > {anchor}" if anchor else "")
            link += "]]"
            pieces.append(line[last:start] + link)
            last = end
            report.rewritten.append((i + 1, raw, link))
        if pieces:
            lines[i] = "".join(pieces) + line[last:]
    return "\n".join(lines), report


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
            return (f"- line {r[0]}: {r[1]} after a link repeats a path in code formatting: the link already "
                    "holds it, so drop it.")
        parts.append("links_flagged: left as written, check each:\n" + listed(flags, flag))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""
