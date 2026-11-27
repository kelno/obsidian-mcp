"""A `<` that Obsidian would read as the start of an HTML tag gets a backslash, in what a write adds.

Obsidian reads `<` followed by a letter, `/` or `!` as HTML. An agent's
placeholder such as `<name>` then breaks the note: in Live Preview everything
after it shows as raw Markdown, to the end of the note, and in Reading view the
word silently disappears (tested on Obsidian 1.15.1, 2026-11-27; ledger F127,
C164). `\\<name>` shows as `<name>` in both views. So on the lines a write adds
or changes, the note writers (`create_note`, `edit_note`) put a backslash in
front of every such `<`, except where it is not read as HTML or is meant:

- inside code: fenced blocks (in quotes and callouts too), inline code spans,
  indented code blocks;
- in the frontmatter, in math (`$…$`, `$$…$$`) and in Obsidian comments
  (`%%…%%`);
- an autolink (`<https://…>`, `<user@host>`) or a link destination written in
  angle brackets (`[text](<My note.md>)`);
- a `<` already escaped;
- a tag the note already held, unescaped, before the write: the owner's own
  `<br>`, sent back by a full or section replace, stays as it was;
- a checkbox line: a task's line is changed by the task tools only, as the
  link rewrite also leaves it;
- the whole write when the caller passes `allow_html=true`: escaping is the
  default, and a write that means HTML says so (owner, 2026-11-27).

Guessing which HTML is deliberate (a known tag with its closing tag later)
was weighed and dropped: "has its closing tag" has no clear meaning across
nesting and separate writes, and some HTML is valid without one. The default
plus `allow_html` is predictable, and the result names each escape, so a wrong
one is seen and the write redone. `write_file` is the raw route and is left
alone. The write is never refused for this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.plugins.kelno_vault.link_paths import quoted_code_lines
from src.services.links import FULL_NOTE, mask_code
from src.services.note_lines import changed_lines, frontmatter_lines

#: The tools whose writes are escaped; `write_file` writes the bytes it is given.
TOOLS = frozenset({"create_note", "edit_note"})

#: A `<` Obsidian reads as the start of a tag: followed by a letter, a slash or `!` (a comment, a doctype).
_TAG_START_RE = re.compile(r"<(?=[A-Za-z/!])")

#: An autolink (CommonMark): a scheme of 2 to 32 characters, a colon, no space or angle bracket; or an email.
_AUTOLINK_RE = re.compile(r"<(?:[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\s<>]*|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9.\-]+)>")

#: The tag as the report shows it, and as it is looked up in the old note: up to its `>` on the line, else its name.
_TOKEN_RE = re.compile(r"<[^<>\n]*>|<[!/]?[\w\-]*")

#: A checkbox list item, quoted or not (as in `link_paths`).
_CHECKBOX_RE = re.compile(r"^[ \t]*(?:>[ \t]?)*[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]+\[.\]")

#: A list item's marker, after any indentation.
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*+]|[0-9]+[.)])(?:[ \t]|$)")

#: Obsidian comments, inline or across lines.
_COMMENT_RE = re.compile(r"%%.*?%%", re.DOTALL)

#: Display math across lines, then inline math: `$` with no space inside either end (`$5 and $10` is no math).
_DISPLAY_MATH_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"(?<![\\$])\$(?=[^\s$])[^$\n]*?(?<=[^\s\\$])\$(?!\$)")


@dataclass
class EscapeReport:
    """What a write had escaped: `(0-based line, the tag as written)`, in note order."""

    escaped: list[tuple[int, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.escaped)


def _blank(text: str, pattern: re.Pattern[str]) -> str:
    """`text` with each match of `pattern` turned to spaces, its line breaks kept."""
    return pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _indent(line: str) -> int:
    """A line's indentation in columns, a tab counting to the next multiple of four."""
    width = 0
    for c in line:
        if c == " ":
            width += 1
        elif c == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _indented_code_lines(lines: list[str], masked_lines: list[str]) -> set[int]:
    """Lines of indented code blocks: four columns in, after a blank line, and not inside a list.

    A line four columns in continues a list item when a list item comes before
    it with nothing less indented in between; otherwise, after a blank line,
    it is code. The masked lines are used so a fenced block counts as blank.
    """
    out: set[int] = set()
    in_list = False
    previous_blank = True
    for i, line in enumerate(masked_lines):
        if not line.strip():
            previous_blank = True
            continue
        width = _indent(line)
        if width < 4:
            in_list = bool(_LIST_ITEM_RE.match(line)) or (in_list and not previous_blank)
        elif not in_list and (previous_blank or i - 1 in out):
            out.add(i)
        previous_blank = False
    return out


def _escaped(line: str, at: int) -> bool:
    """Whether the character at `at` follows an odd run of backslashes."""
    run = 0
    while at - run - 1 >= 0 and line[at - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _unescaped_tokens(text: str | None) -> set[str]:
    """The tags `text` holds unescaped outside its frontmatter, as `_TOKEN_RE` cuts them."""
    if not text:
        return set()
    out = set()
    for line in text.split("\n"):
        for m in _TAG_START_RE.finditer(line):
            if not _escaped(line, m.start()):
                out.add(_TOKEN_RE.match(line, m.start()).group(0))
    return out


def escape(old: str | None, new: str, *, allow_html: bool = False) -> tuple[str, EscapeReport]:
    """`new` with a backslash before each `<` Obsidian would read as a tag, on changed lines only, and the report.

    Args:
        old: the note's text before the write; None for a new note.
        new: the text about to be written.
        allow_html: the caller's `allow_html`: when true, nothing is escaped.

    Returns:
        The text to write and what was escaped in it.
    """
    report = EscapeReport()
    if allow_html or "<" not in new:
        return new, report
    if "\n" not in new and "\r" in new:
        # A note with lone-CR line endings: work on LF and give its CRs back, as the other rewrites do.
        text, report = escape(old.replace("\r", "\n") if old is not None else None, new.replace("\r", "\n"))
        return text.replace("\n", "\r"), report
    lines = new.split("\n")
    changed = changed_lines(old, new) - set(range(frontmatter_lines(lines))) - quoted_code_lines(lines)
    changed = {i for i in changed if "<" in lines[i] and not _CHECKBOX_RE.match(lines[i])}
    if not changed:
        return new, report
    masked = mask_code(new, context=FULL_NOTE)
    # The masker blanks a fenced block whole, its line breaks included: put them back so lines align.
    masked = "".join("\n" if c == "\n" else m for c, m in zip(new, masked))
    masked = _blank(_blank(_blank(masked, _COMMENT_RE), _DISPLAY_MATH_RE), _INLINE_MATH_RE)
    masked_lines = masked.split("\n")
    changed -= _indented_code_lines(lines, masked_lines)
    kept = _unescaped_tokens(old)
    for i in sorted(changed):
        line, visible = lines[i], masked_lines[i]
        inserts = []
        for m in _TAG_START_RE.finditer(visible):
            at = m.start()
            if line[at] != "<" or _escaped(line, at) or _AUTOLINK_RE.match(line, at) or line[:at].endswith("]("):
                continue
            token = _TOKEN_RE.match(line, at).group(0)
            if token in kept:
                continue
            inserts.append(at)
            report.escaped.append((i, token))
        for at in reversed(inserts):
            line = line[:at] + "\\" + line[at:]
        lines[i] = line
    return "\n".join(lines), report


def shifted(report: EscapeReport, by: int) -> EscapeReport:
    """The report with its line numbers moved down by `by` (the stamp's lines above the body)."""
    return EscapeReport([(i + by, token) for i, token in report.escaped])


def render(report: EscapeReport, *, dry_run: bool = False) -> str:
    """The result's block, in the style of `links_rewritten:`; "" when nothing was escaped."""
    if not report:
        return ""
    verb = "would get" if dry_run else "got"
    listed = "\n".join(f"- line {i + 1}: `{token}`" for i, token in report.escaped)
    return (f"\n\nangle_brackets_escaped: Obsidian reads `<` before a letter, `/` or `!` as HTML, which breaks "
            f"the note's view, so these {verb} a backslash in front (shown as written: `\\<` displays as `<`):\n"
            f"{listed}\nIf one was meant as HTML, write it again with `allow_html=true`. "
            "Not for the owner: say nothing of it unless they ask.")


#: `create_note`'s `allow_html` description line; `edit_note`'s one line of plugin text says the same.
ALLOW_HTML_TEXT = "Else a `<` read as HTML is escaped (`angle_brackets_escaped:`)."

GUIDE = (
    "## Angle brackets (this server)\n\n"
    "Obsidian reads a `<` before a letter, `/` or `!` as the start of an HTML tag: a placeholder such as `<name>` "
    "breaks the rest of the note in Live Preview and vanishes in Reading view. So `create_note` and `edit_note` "
    "put a backslash in front of such a `<` on the lines a write adds or changes (`\\<name>` shows as `<name>`), "
    "and list each one under `angle_brackets_escaped:`; the reported `content_hash` is of what was written. Left "
    "alone: code (fenced, inline, indented), the frontmatter, math, `%%` comments, autolinks (`<https://…>`), "
    "link destinations in angle brackets (`[text](<a b.md>)`), a `<` already escaped, a tag the note already held "
    "before the write, and checkbox lines. To write HTML on purpose (`<br>`, `<details>`, `<kbd>`, an `<iframe>`), "
    "pass `allow_html=true` and nothing is escaped. A write is never refused for it, and `write_file` writes "
    "bytes as sent."
)
