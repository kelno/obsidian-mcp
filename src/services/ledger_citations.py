"""How text cites agent-ledger entries: `[Q14]`, `[P35, P48]`, or a bare Q14.

Agents cite entries in vault tasks, notes and rules as a bracketed list of
refs: `[F12]`, `[Q14]`, or several kinds mixed in one bracket, comma-separated
(`[P35, P48]`). In ledger text (an entry's title, body and own fields) a bare
ref such as Q14 counts as well, since nothing else there looks like one.

Matching rules, shared by every tool that reads citations:

- A ref is a capital Q, F or P and a number of 1 to 6 digits, after any
  leading zeros (`[Q01]` cites Q1). Lower-case letters are not refs.
- A bracket holds refs only, separated by commas, with optional spaces or
  tabs (ASCII only, so the SQL prefilters below can promise to be a superset).
- Not a citation: a Markdown link `[F12](…)`, a reference-style link
  `[F12][x]` or `[x][F12]`, a reference definition `[F12]: …` at the start of
  a line, an image `![F12]`, an escaped bracket, a wikilink `[[F12]]`, a
  footnote `[^F12]`, and anything inside inline code or a fenced code block
  (at any indentation), where refs are shown as examples rather than cited.
  Two ref brackets side by side (`[P35][P48]`) and a checkbox right before
  one (`- [x][Q14]`) are citations.
- Lines are split on newlines only, as Obsidian numbers them.
- A bare ref is a whole word not preceded by `#`, `/`, `[` or `^`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: One ref inside a bracket or bare: kind letter, then the number without its leading zeros.
_REF = r"([QFP])0*([0-9]{1,6})(?![0-9A-Za-z_])"
#: A bracket of refs: `[Q14]`, `[P35, P48]`; leading zeros allowed (`[Q0014]`).
_BRACKET_RE = re.compile(
    r"\[[ \t]*(?:[QFP]0*[0-9]{1,6}[ \t]*,[ \t]*)*[QFP]0*[0-9]{1,6}[ \t]*\]"
)
_REF_IN_BRACKET_RE = re.compile(r"([QFP])0*([0-9]{1,6})")
#: A bare ref: a whole word, not a tag, a path part, a bracket's content or a footnote.
_BARE_RE = re.compile(r"(?<![\w#/\[^])" + _REF)
#: Inline code: a run of backticks, then the shortest text up to the same run.
_CODE_SPAN_RE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
#: A fence line, at any indentation (fences inside nested list items are indented),
#: with what follows the fence: a closing fence has nothing but spaces after it.
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
#: A list item's checkbox right before a bracket: `- [x][Q14]`.
_CHECKBOX_LEAD_RE = re.compile(r"[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]+\[.\]\Z")
#: Lines as Obsidian numbers them: split on newline only, ends kept.
_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+\Z")
_REF_DEFINITION_LEAD_RE = re.compile(r"[ ]{0,3}\Z")

#: A POSIX regex PostgreSQL can apply to keep only lines that may hold a
#: bracketed citation: a superset of `_BRACKET_RE`, written without classes
#: whose meaning depends on the database's locale.
SQL_BRACKET_PREFILTER = r"\[[ \t]*[QFP][0-9]"


@dataclass(frozen=True)
class Ref:
    letter: str
    number: int

    def __str__(self) -> str:
        return f"{self.letter}{self.number}"


def _masked(text: str) -> str:
    """`text` with code (fenced blocks, inline spans) blanked out, same length."""
    out = []
    fence: str | None = None
    for line in _LINE_RE.findall(text):
        stripped = line.rstrip("\r\n")
        match = _FENCE_RE.match(stripped)
        if fence is not None:
            if (match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence)
                    and not match.group(2).strip()):
                fence = None
            out.append(" " * len(stripped) + line[len(stripped):])
            continue
        if match:
            fence = match.group(1)
            out.append(" " * len(stripped) + line[len(stripped):])
            continue
        out.append(_CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), line))
    return "".join(out)


#: A list item's lead before its checkbox is short; a longer prefix is not one.
_CHECKBOX_LEAD_MAX = 40


def _is_link_or_definition(text: str, start: int, end: int, line_start: int, bracket_ends: set[int]) -> bool:
    """Whether the bracket at `start:end` is Markdown syntax rather than a citation.

    Next to another bracket it is a link (`[x][F12]`, `[F12][x]`) unless that
    bracket is a ref list too (`[P35][P48]`) or a list item's checkbox
    (`- [x][Q14]`). Constant work per bracket (`bracket_ends` and
    `line_start` come from one pass), so a line of thousands of brackets
    stays linear.
    """
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before in ("[", "!", "\\") or after in ("(", "]"):
        return True
    if before == "]" and start not in bracket_ends:
        lead = text[line_start:start] if start - line_start <= _CHECKBOX_LEAD_MAX else ""
        if not (lead and _CHECKBOX_LEAD_RE.match(lead)):
            return True
    if after == "[" and not _BRACKET_RE.match(text, end):
        return True
    if after == ":" and start - line_start <= 3 and _REF_DEFINITION_LEAD_RE.match(text[line_start:start]):
        return True
    return False


def _bracket_spans(masked: str) -> list[tuple[int, int]]:
    from bisect import bisect_right

    matches = [m.span() for m in _BRACKET_RE.finditer(masked)]
    if not matches:
        return []
    ends = {end for _, end in matches}
    newlines = [i for i, c in enumerate(masked) if c == "\n"]

    def line_start(pos: int) -> int:
        k = bisect_right(newlines, pos - 1)
        return newlines[k - 1] + 1 if k else 0

    return [(start, end) for start, end in matches
            if not _is_link_or_definition(masked, start, end, line_start(start), ends)]


def _unique(refs) -> list[Ref]:
    return list(dict.fromkeys(refs))


def _bracketed_found(masked: str) -> list[tuple[int, Ref]]:
    found = []
    for start, end in _bracket_spans(masked):
        found += [(m.start(), Ref(m.group(1), int(m.group(2))))
                  for m in _REF_IN_BRACKET_RE.finditer(masked, start, end)]
    return found


def bracketed(text: str | None) -> list[Ref]:
    """The refs `text` cites in brackets, in order, once each."""
    return _unique(r for _, r in _bracketed_found(_masked(text or "")))


def bare(text: str | None) -> list[Ref]:
    """The refs `text` names as bare words, outside any bracketed citation and code."""
    masked = _masked(text or "")
    spans = _bracket_spans(masked)
    return _unique(Ref(m.group(1), int(m.group(2))) for m in _BARE_RE.finditer(masked)
                   if not any(start <= m.start() < end for start, end in spans))


def in_ledger_text(text: str | None) -> list[Ref]:
    """Every ref ledger text cites, bracketed or bare, in order, once each."""
    masked = _masked(text or "")
    found = _bracketed_found(masked) + [(m.start(), Ref(m.group(1), int(m.group(2))))
                                        for m in _BARE_RE.finditer(masked)]
    return _unique(r for _, r in sorted(found, key=lambda item: item[0]))


def lines_citing(text: str, ref: Ref) -> list[int]:
    """The 1-based line numbers of `text` that cite `ref` in brackets."""
    return [number for number, line in enumerate(_masked(text).split("\n"), start=1)
            if any(r == ref for _, r in _bracketed_found(line))]


def changed_line_refs(old: str | None, new: str | None) -> tuple[list[Ref], list[Ref]]:
    """`(on_changed_lines, added)` for a note write from `old` to `new` text.

    - `on_changed_lines`: refs cited in brackets on the lines of `old` that
      `new` no longer holds as they were (removed or rewritten). A line only
      moved elsewhere unchanged is not one. Lines are compared whole, as a
      multiset, so the cost stays linear in the note's length.
    - `added`: refs cited in brackets in `new` that `old` did not cite anywhere.

    Each line is read in its note's context, so a ref inside a fenced block is
    an example, not a citation. `None` stands for no note (a create, a delete).
    """
    from collections import Counter

    def lines_and_refs(text: str | None) -> list[tuple[str, tuple[Ref, ...]]]:
        if text is None:
            return []
        # Any line ending counts here, a lone CR included: the comparison is
        # about lines, and a CR-only note would otherwise be one long line.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        masked = _masked(text)
        return [(line, tuple(r for _, r in _bracketed_found(m)))
                for line, m in zip(text.split("\n"), masked.split("\n"))]

    old_rows, new_rows = lines_and_refs(old), lines_and_refs(new)
    # A line is kept when the same text cites the same refs in the new note:
    # one the edit put inside a code fence (its refs gone) counts as changed.
    kept = Counter(new_rows)
    changed = []
    for row in old_rows:
        if kept[row] > 0:
            kept[row] -= 1
            continue
        changed.extend(row[1])
    old_refs = [refs for _, refs in old_rows]
    new_refs = [refs for _, refs in new_rows]
    before = {r for refs in old_refs for r in refs}
    added = [r for refs in new_refs for r in refs if r not in before]
    return _unique(changed), _unique(added)


def sql_ref_pattern(ref: Ref) -> str:
    """A POSIX regex for PostgreSQL: text that may name `ref`, bare or bracketed (a superset)."""
    return f"{ref.letter}0*{ref.number}([^0-9]|$)"


def rule_lines_unlogged(old: str | None, new: str, *, skip_leading: int = 0) -> list[tuple[int, str]]:
    """The lines a rule edit adds or changes, when none of them gains a proposal ref.

    `[(line number, text)]` of the non-blank lines `new` holds that `old` did
    not (compared as the other note checks compare: line endings and
    surrounding spaces ignored), past the first `skip_leading` lines (the
    frontmatter). Empty when there are none, or when at least one of them
    cites in brackets a proposal (`P`) the lines the write removed did not
    cite: the change carries its proposal. Refs are read in the note's own
    fence context, as everywhere else.
    """
    from collections import Counter

    def rows(text: str | None):
        if text is None:
            return []
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        masked = _masked(text).split("\n")
        return [(i, line, {r for _, r in _bracketed_found(m) if r.letter == "P"})
                for i, (line, m) in enumerate(zip(text.split("\n"), masked))]

    def same(line: str) -> str:
        return line.lstrip("﻿").strip()

    def bare(line: str) -> str:
        """The line without its bracketed refs: a change that only drops a ref is no rule change."""
        return " ".join(_BRACKET_RE.sub(" ", same(line)).split())

    old_rows, new_rows = rows(old), rows(new)
    left = Counter(same(line) for _, line, _refs in old_rows)
    added = []
    for i, line, refs in new_rows:
        key = same(line)
        if left[key] > 0:
            left[key] -= 1
        elif i >= skip_leading and key:
            added.append((i, line, refs))
    if not added:
        return []
    kept = Counter(same(line) for _, line, _refs in new_rows)
    removed_refs, removed_bare = set(), Counter()
    for _, line, refs in old_rows:
        key = same(line)
        if kept[key] > 0:
            kept[key] -= 1
        else:
            removed_refs |= refs
            removed_bare[bare(line)] += 1
    if any(refs - removed_refs for _, _, refs in added):
        return []
    out = []
    for i, line, _refs in added:
        if removed_bare[bare(line)] > 0 and not (_refs - removed_refs):
            removed_bare[bare(line)] -= 1       # only refs dropped from a line the write kept otherwise
            continue
        out.append((i + 1, line.strip()))
    return out
