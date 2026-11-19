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
In a table the pipe before the display text is escaped (`\\|`), and only
there: elsewhere `\\|` breaks the link in Obsidian, so a rewritten link gets
the pipe its line calls for, whichever it was written with. A line is a table
row only inside a table block (a header, a delimiter row, the lines up to a
blank one), never for a stray pipe: the display pipe of another link on an
ordinary line once made a rewrite escape this one, and the broken link came
back on every edit of the line. A link the rewrite would
give back unchanged (a note at the vault root, whose full path is its name)
is not reported. Lines the write leaves as they were are never touched.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from src.services.links import FULL_NOTE, mask_code
from src.services.note_lines import changed_lines, frontmatter_lines

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


#: A table's delimiter row (`| --- | :-: |`, leading and trailing pipes optional); the pipe makes it not a rule.
_DELIMITER_ROW_RE = re.compile(r"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")


def _table_rows(masked_lines: list[str]) -> set[int]:
    """The lines that are rows of a table, header and delimiter included, as Obsidian renders one.

    A table, as in GitHub-flavoured Markdown, is a header line with a pipe of
    its own, then a delimiter row, then every line up to a blank one or a
    change of quote depth, pipes or not. A pipe on any other line (prose,
    inline math, a link's display pipe) makes no table, and only a row gets
    `\\|`. `masked_lines` have their code blanked; every link's span is
    blanked too, so a link's display pipe never counts as the row's.
    """
    def split(line: str) -> tuple[int, str, bool]:
        """Quote depth, the body with links blanked, and whether the body is blank (before the blanking)."""
        prefix = _QUOTE_PREFIX_RE.match(line)
        depth = prefix.group().count(">") if prefix else 0
        body = (line[prefix.end():] if prefix else line).rstrip("\r")
        return depth, _WIKILINK_RE.sub(lambda m: " " * len(m.group()), body), not body.strip()

    parts = [split(line) for line in masked_lines]
    rows: set[int] = set()
    i = 1
    while i < len(parts):
        depth, delimiter, _blank = parts[i]
        head_depth, head, _blank = parts[i - 1]
        if (i - 1 not in rows and depth == head_depth and "|" in delimiter and _DELIMITER_ROW_RE.match(delimiter)
                and _UNESCAPED_PIPE_RE.search(head)):
            rows.update((i - 1, i))
            i += 1
            while i < len(parts) and parts[i][0] == depth and not parts[i][2]:
                rows.add(i)
                i += 1
        i += 1
    return rows


def _is_note_target(name: str, index: NoteIndex) -> bool:
    """Whether a bare target means a note: no extension, `.md`, or a note's own name with a dot in it."""
    _base, ext = os.path.splitext(name)
    return not ext or ext.lower() == ".md" or bool(index.named(name))


def needs_check(old: str | None, new: str) -> bool:
    """Whether any line the write adds or changes holds `[[` (else no note list is needed)."""
    if "[[" not in new:
        return False
    lines = new.split("\n")
    return any("[[" in lines[i] for i in changed_lines(old, new))


#: A checkbox list item, quoted or not: a task's line is changed by the task tools only.
_CHECKBOX_RE = re.compile(r"^[ \t]*(?:>[ \t]?)*[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]+\[.\]")


@dataclass
class Prepared:
    """What `rewrite` reads of a write before it looks at the note list: the
    lines, the ones the write adds or changes, and the lines with code blanked."""
    lines: list[str]
    changed: set[int]
    masked_lines: list[str]


def prepare(old: str | None, new: str) -> Prepared | None:
    """The write's lines for `rewrite` and `lookups`; None when `new` holds no `[[`."""
    if "[[" not in new:
        return None
    lines = new.split("\n")
    changed = changed_lines(old, new) - set(range(frontmatter_lines(lines))) - _quoted_code_lines(lines)
    masked = mask_code(new, context=FULL_NOTE)
    # The masker blanks a fenced block whole, its line breaks included: put
    # them back so masked lines align with the note's.
    masked = "".join("\n" if c == "\n" else m for c, m in zip(new, masked))
    return Prepared(lines, changed, masked.split("\n"))


def _examined(prepared: Prepared):
    """`(line index, match, target, anchor, alias)` for every link `rewrite` examines, in order."""
    for i in sorted(prepared.changed):
        line, masked = prepared.lines[i], prepared.masked_lines[i]
        if "[[" not in masked or _CHECKBOX_RE.match(line):
            continue
        for m in _WIKILINK_RE.finditer(masked):
            start, end = m.span()
            if line[start:end] != masked[start:end]:
                continue                              # code inside or across the link: not touched
            target = m.group("target")
            anchor, alias = m.group("anchor"), m.group("alias")
            # `[[X\|Y]]`: the escaped pipe's backslash is not part of the target.
            if alias is not None and anchor is not None and anchor.endswith("\\"):
                anchor = anchor[:-1]
            elif alias is not None and target.endswith("\\"):
                target = target[:-1]
            yield i, m, target, anchor, alias


#: Past this many names to look up, the whole note list is read instead.
MAX_LOOKUPS = 100
_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
#: `FOLD_TO` is what a path's characters in `FOLD_FROM` become before the patterns of `lookups` apply:
#: ASCII capitals, and the Kelvin sign, the one other character Python's `lower` makes an ASCII letter.
FOLD_FROM = _ASCII_UPPER + "K"
FOLD_TO = _ASCII_UPPER.lower() + "k"


def _like(lowered: str) -> str | None:
    """A LIKE pattern (escape `\\`) matching, once folded, every path whose `lower()` is `lowered`; None if none can.

    Python's `lower` turns a character into an ASCII one only from an ASCII
    capital, the Kelvin sign (both folded first) or `İ` (into `i` and U+0307,
    which changes the length: no pattern then). Any other character of
    `lowered` may come from several, so it matches any one character.
    """
    if "̇" in lowered:
        return None
    out = []
    for ch in lowered:
        if ch in "%_\\":
            out.append("\\" + ch)
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)


def lookups(prepared: Prepared | None) -> list[str] | None:
    """LIKE patterns (escape `\\`) over the folded path (`FOLD_FROM` → `FOLD_TO`) that keep every note
    `rewrite(…, prepared)` may look up: `[]` when it looks none up, None when the whole list is needed.

    A superset: `rewrite` matches the names exactly against what comes back.
    The lookups are the names `NoteIndex.named` and the paths
    `NoteIndex.has_path` may be asked about, whatever the answers.
    """
    if prepared is None:
        return []
    names: set[str] = set()
    paths: set[str] = set()
    for _i, _m, target, _anchor, _alias in _examined(prepared):
        name = target.strip()
        if "/" in name:
            names.add(os.path.basename(name))
            paths.add(name)
        else:
            names.add(name)
            names.add(name[:-3] if name.lower().endswith(".md") else name)
    if len(names) + len(paths) > MAX_LOOKUPS:
        return None
    patterns: set[str] = set()
    for name in names:
        lowered = name.lower()
        like = _like(lowered)
        if like is None:
            return None
        patterns.update((like + ".md", "%/" + like + ".md"))
        if lowered.startswith("."):
            # `os.path.splitext` keeps a name of dots and `md` whole (`..md`): its stem is all of it.
            patterns.update((like, "%/" + like))
    for path in paths:
        lowered = path.lower()
        like = _like(lowered if lowered.endswith(".md") else lowered + ".md")
        if like is None:
            return None
        patterns.add(like)
    return sorted(patterns)


def rewrite(old: str | None, new: str, index: NoteIndex,
            prepared: Prepared | None = None) -> tuple[str, LinkReport]:
    """`new` with its bare-name links given full paths, on changed lines only, and the report.

    Checkbox lines are never rewritten: a task's line is changed by the task
    tools only, and the task check compares lines the same way. `prepared`
    is `prepare(old, new)` when the caller made it already.
    """
    report = LinkReport()
    if "[[" not in new:
        return new, report
    if prepared is None:
        prepared = prepare(old, new)
    lines = list(prepared.lines)
    table_rows = _table_rows(prepared.masked_lines)
    by_line: dict[int, list] = {}
    for i, m, target, anchor, alias in _examined(prepared):
        by_line.setdefault(i, []).append((m, target, anchor, alias))
    for i, links in by_line.items():
        line = lines[i]
        pipe = "\\|" if i in table_rows else "|"
        pieces, last = [], 0
        for m, target, anchor, alias in links:
            start, end = m.span()
            raw = line[start:end]
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
            link = ("!" if embed else "") + "[[" + path + (f"#{anchor}" if anchor is not None else "")
            if alias is not None:
                link += pipe + alias
            elif not embed:
                # The text Obsidian showed: the name as written, then each anchor part.
                shown = name + "".join(f" > {part}" for part in (anchor or "").split("#") if part)
                link += pipe + shown
            link += "]]"
            if link == raw:
                continue                              # already right: a root note, its pipe as the row wants
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
