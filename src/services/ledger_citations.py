"""How text cites agent-ledger entries: `[Q14]`, `[P35, P48]`, or a bare Q14.

Agents cite entries in vault tasks, notes and rules as a bracketed list of
refs: `[F12]`, `[Q14]`, or several kinds mixed in one bracket, comma-separated
(`[P35, P48]`). In ledger text (an entry's title, body and own fields) a bare
ref such as Q14 counts as well, since nothing else there looks like one.

Matching rules, shared by every tool that reads citations:

- A ref is a capital Q, F or P and 1 to 6 digits; leading zeros don't matter
  (`[Q01]` cites Q1). Lower-case letters are not refs.
- A bracket holds refs only, separated by commas, with optional spaces or
  tabs (ASCII only, so the SQL prefilters below can promise to be a superset).
- Not a citation: a Markdown link `[F12](…)`, a reference-style link
  `[F12][x]` or `[x][F12]`, a reference definition `[F12]: …` at the start of
  a line, an image `![F12]`, a wikilink `[[F12]]`, a footnote `[^F12]`, and
  anything inside inline code or a fenced code block, where refs are shown as
  examples rather than cited.
- A bare ref is a whole word not preceded by `#`, `/`, `[` or `^`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: One ref inside a bracket or bare: kind letter, then the number without its leading zeros.
_REF = r"([QFP])0*([0-9]{1,6})(?![0-9A-Za-z_])"
#: A bracket of refs: `[Q14]`, `[P35, P48]`.
_BRACKET_RE = re.compile(
    r"\[[ \t]*(?:[QFP][0-9]{1,6}[ \t]*,[ \t]*)*[QFP][0-9]{1,6}[ \t]*\]"
)
_REF_IN_BRACKET_RE = re.compile(r"([QFP])0*([0-9]{1,6})")
#: A bare ref: a whole word, not a tag, a path part, a bracket's content or a footnote.
_BARE_RE = re.compile(r"(?<![\w#/\[^])" + _REF)
#: Inline code: a run of backticks, then the shortest text up to the same run.
_CODE_SPAN_RE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")
_FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
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
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        match = _FENCE_RE.match(stripped)
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            out.append(" " * len(stripped) + line[len(stripped):])
            continue
        if match:
            fence = match.group(1)
            out.append(" " * len(stripped) + line[len(stripped):])
            continue
        out.append(_CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), line))
    return "".join(out)


def _is_link_or_definition(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before in ("[", "]", "!") or after in ("(", "[", "]"):
        return True
    if after == ":":
        line_start = text.rfind("\n", 0, start) + 1
        if _REF_DEFINITION_LEAD_RE.match(text[line_start:start]):
            return True
    return False


def _bracket_spans(masked: str) -> list[tuple[int, int]]:
    return [m.span() for m in _BRACKET_RE.finditer(masked)
            if not _is_link_or_definition(masked, *m.span())]


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
    return [number for number, line in enumerate(_masked(text).splitlines(), start=1)
            if any(r == ref for _, r in _bracketed_found(line))]


def sql_ref_pattern(ref: Ref) -> str:
    """A POSIX regex for PostgreSQL: text that may name `ref`, bare or bracketed (a superset)."""
    return f"{ref.letter}0*{ref.number}([^0-9]|$)"
