"""A `<` that Obsidian would read as the start of an HTML tag gets a backslash, in what a write adds.

Obsidian reads `<` followed by a letter, `/` or `!` as HTML. An agent's
placeholder such as `<name>` then breaks the note: in Live Preview everything
after it shows as raw Markdown, to the end of the note, and in Reading view the
word silently disappears (tested on Obsidian 1.15.1, 2026-11-27; ledger F127,
C164). `\\<name>` shows as `<name>` in both views. So on the lines a write adds
or changes, the note writers (`create_note`, `edit_note`) put a backslash in
front of every such `<`, except where it is not read as HTML or is meant:

- inside code: fenced blocks at any indentation (in list items, quotes and
  callouts too), inline code spans (backtick runs paired by length, across the
  lines of a paragraph), indented code blocks (in list items too);
- in the frontmatter, in math (`$…$`, `$$…$$`), in Obsidian comments
  (`%%…%%`) and inside wikilinks (`[[Note|<name>]]`, whose display text
  Obsidian doesn't read as Markdown);
- an autolink (`<https://…>`, `<user@host>`) or a link destination written in
  angle brackets (`[text](<My note.md>)`, `[ref]: <My note.md>`);
- a `<` already escaped;
- a tag the note already held, unescaped and outside code, before the write:
  the owner's own `<br>`, on a line the write changes, stays as it was;
- a checkbox line: a task's line is changed by the task tools only, as the
  link rewrite also leaves it;
- the whole write when the caller passes `allow_html=true`: escaping is the
  default, and a write that means HTML says so (owner, 2026-11-27).

A backslash inside code shows as written, which changes the code an agent
wrote; a `<` left bare only misses an escape. So wherever code detection is
unsure, it counts the text as code. It is this module's own, not the link
index's masker, which sees fences only up to three spaces in and single
backticks only (adversarial review, 2026-11-27).

Guessing which HTML is deliberate (a known tag with its closing tag later)
was weighed and dropped: "has its closing tag" has no clear meaning across
nesting and separate writes, and some HTML is valid without one. The default
plus `allow_html` is predictable, and the result names each escape, so a wrong
one is seen and undone. `write_file` is the raw route and is left alone. The
write is never refused for this.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from src.services.note_lines import changed_lines, frontmatter_lines

#: The tools whose writes are escaped; `write_file` writes the bytes it is given.
TOOLS = frozenset({"create_note", "edit_note"})

#: A `<` Obsidian reads as the start of a tag: followed by a letter, a slash or `!` (a comment, a doctype).
_TAG_START_RE = re.compile(r"<(?=[A-Za-z/!])")

#: An autolink (CommonMark): a scheme of 2 to 32 characters, a colon, no space or angle bracket; or an email.
_AUTOLINK_RE = re.compile(r"<(?:[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^\s<>]*|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~\-]+@[A-Za-z0-9.\-]+)>")

#: The tag as the report shows it, and as it is looked up in the old note: up to its `>` on the line, else its name.
_TOKEN_RE = re.compile(r"<[^<>\n]*>|<[!/]?[\w\-]*")

#: Quote markers at a line's start (`>`, `> >`), with the one space each may take; plain indentation stays.
_QUOTE_RE = re.compile(r"^(?: {0,3}>[ \t]?)*")

#: Quote markers after any indentation, as in a quote or callout inside a list item (`\t> ~~~`); plain
#: indentation with no marker after it stays. Code is looked for under both readings (`_code_lines`).
_NESTED_QUOTE_RE = re.compile(r"^(?:[ \t]*>[ \t]?)*")

#: A link reference definition up to its destination's `<` (`[ref]: <My note.md>`), after quote and list markers.
_LINK_DEFINITION_RE = re.compile(r"(?:[ \t]*(?:>[ \t]?|(?:[-*+]|[0-9]{1,9}[.)])[ \t]+))*[ \t]*\[[^\]]+\]:[ \t]*")

#: A line that may start a block of its own, after quote markers: a list item with content, a heading, a table row.
_BLOCK_START_RE = re.compile(
    r"(?P<indent>[ \t]*)(?:(?P<bullet>[-*+])[ \t]+\S|(?P<number>[0-9]{1,9})[.)][ \t]+\S|(?P<heading>#{1,6})(?:[ \t]|$)|\|)")

#: A checkbox list item, quoted or not (as in `link_paths`).
_CHECKBOX_RE = re.compile(r"^[ \t]*(?:>[ \t]?)*[ \t]*(?:[-*+]|[0-9]+[.)])[ \t]+\[.\]")

#: A list item after its quote markers: indentation, marker, then the spaces before its content (or nothing).
_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|[0-9]{1,9}[.)])(?P<gap>[ \t]+|$)")

#: A fence opener or closer at any indentation, after quote markers: three or more backticks or tildes.
_FENCE_RE = re.compile(r"^[ \t]*(?P<run>`{3,}|~{3,})(?P<rest>.*)$")

#: An ATX heading: indented code may follow one directly, as it may a blank line.
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")

#: Obsidian comments, inline or across lines.
_COMMENT_RE = re.compile(r"%%.*?%%", re.DOTALL)

#: Display math across lines, then inline math: `$` with no space inside either end (`$5 and $10` is no math).
_DISPLAY_MATH_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_INLINE_MATH_RE = re.compile(r"(?<![\\$])\$(?=[^\s$])[^$\n]*?(?<=[^\s\\$])\$(?!\$)")

#: A wikilink or embed on one line.
_WIKILINK_RE = re.compile(r"\[\[[^\]\n]*\]\]")


@dataclass
class EscapeReport:
    """What a write had escaped: `(0-based line, the tag as written)`, in note order."""

    escaped: list[tuple[int, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.escaped)


def _width(text: str) -> int:
    """The columns `text`'s leading whitespace takes, a tab counting to the next multiple of four."""
    width = 0
    for c in text:
        if c == " ":
            width += 1
        elif c == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _code_lines(lines: list[str]) -> set[int]:
    """The lines of fenced and indented code blocks, erring towards code.

    Quote markers after indentation (a quote or callout in a list item, which
    Obsidian indents with a tab) are either that or indented code (`    > x`
    after a blank line), so the lines are read both ways and a line is code
    when either reading says so.
    """
    return _code_lines_read(lines, _QUOTE_RE) | _code_lines_read(lines, _NESTED_QUOTE_RE)


def _code_lines_read(lines: list[str], quote_re: re.Pattern[str]) -> set[int]:
    """The code lines of `lines`, each measured after the quote markers `quote_re` takes off.

    A fence opens at any indentation (a fence in a list item sits four or more
    columns in) and runs to a closing run of its kind at least as long, at the
    opener's quote depth, or to the end of the note: a quoted closer inside an
    unquoted fence is the code's own text, as when showing code in a callout.
    An indented code block is a line four columns past the content of the list
    item it sits in (or past the margin outside a list), after a blank line, a
    heading, a fence or another such line: a paragraph line can't start one.
    """
    out: set[int] = set()
    fence: str | None = None
    fence_depth = 0                  # quote markers before the open fence's opener
    lists: list[int] = []            # content columns of the open list items, innermost last
    open_to_code = True              # whether an indented line here would start or continue code
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r")
        quote = quote_re.match(line).group(0)
        rest = line[len(quote):]
        depth = quote.count(">")
        if fence is not None and depth < fence_depth and rest.strip():
            fence = None                                 # the quote holding the fence has ended, and the fence with it
        if fence is not None:
            out.add(i)
            m = _FENCE_RE.match(rest)
            if (m and depth == fence_depth and m.group("run")[0] == fence[0] and len(m.group("run")) >= len(fence)
                    and not m.group("rest").strip()):
                fence, open_to_code = None, True
            continue
        if not rest.strip():
            open_to_code = True
            continue
        width = _width(rest)
        if _opens_fence(rest):
            fence, fence_depth = _FENCE_RE.match(rest).group("run"), depth
            out.add(i)
            continue
        while lists and width < lists[-1] and open_to_code:
            lists.pop()                                  # after a blank line, less indented: that item has ended
        margin = lists[-1] if lists else 0
        if open_to_code and width >= margin + 4:
            out.add(i)
            continue
        heading = bool(_HEADING_RE.match(rest))
        if heading:
            while lists and width < lists[-1]:
                lists.pop()                              # a heading at the margin ends the list, blank line or not
        item = _LIST_ITEM_RE.match(rest)
        if item:
            while lists and _width(item.group("indent")) < lists[-1]:
                lists.pop()
        column = 0
        while item:
            # An item's content may itself open an item (`- - x`) or a fence (`- ```bash`).
            marker_end = column + _width(item.group("indent")) + len(item.group("marker"))
            content = marker_end
            for c in item.group("gap"):
                content += 1 if c == " " else 4 - content % 4
            if content - marker_end > 4:
                # Content five or more columns past the marker is indented code in the item, from its first line;
                # the item's content column is then one past the marker.
                lists.append(marker_end + 1)
                out.add(i)
                break
            lists.append(content if content > marker_end else marker_end + 1)
            rest, column = rest[item.end():], content
            if _opens_fence(rest):
                fence, fence_depth = _FENCE_RE.match(rest).group("run"), depth
                out.add(i)
                break
            item = _LIST_ITEM_RE.match(rest) if item.group("gap") else None
        if i in out:
            open_to_code = True
            continue
        open_to_code = heading
    return out


def _opens_fence(rest: str) -> bool:
    """Whether `rest` opens a fence: a backtick fence's info string may hold no backtick (that is an inline span)."""
    m = _FENCE_RE.match(rest)
    return bool(m) and not (m.group("run")[0] == "`" and "`" in m.group("rest"))


def _blank_matches(text: str, pattern: re.Pattern[str]) -> str:
    """`text` with each match of `pattern` turned to spaces, its line breaks kept."""
    return pattern.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _blank_all_code_spans(text: str) -> str:
    """`text` with its inline code spans blanked, a character counting as code when either of two readings says so.

    A span may run across the lines of a paragraph but not past a blank line,
    nor into a line that always starts a block of its own (`_block_starts`).
    Whether it may cross into a table row, a line of another quote depth or a
    list item that may be paragraph text depends on lazy continuation and
    context that a line-based reading can't settle; a lone backtick paired too
    far leaves the next real span's content bare, so both readings are taken
    there. Taking both readings everywhere would hide every later line of a
    list holding one lone backtick (third review, 2026-11-27).
    """
    blank_lines = {m.start() for m in re.finditer(r"\n[ \t\r]*(?=\n|$)", text)}
    hard, unsure = _block_starts(text)
    certain_ends = sorted(blank_lines | hard)
    by_certain = _blank_code_spans(text, certain_ends)
    if not unsure - blank_lines - hard:
        return by_certain
    by_block = _blank_code_spans(text, sorted(blank_lines | hard | unsure))
    if by_certain == by_block:
        return by_certain
    return "".join(a if a == b else " " for a, b in zip(by_certain, by_block, strict=True))


def _block_starts(text: str) -> tuple[set[int], set[int]]:
    """The offsets of the lines after the first that start a block: those that surely do, and those that may.

    Sure, as CommonMark has it: a heading (and the line after it) and a bullet
    item with content, each indented less than four columns or inside a list,
    and a numbered item inside a list or numbered 1 (only those interrupt a
    paragraph). Maybe: a table row and the line after it, a change of quote
    depth (a lazy continuation keeps the paragraph), any other list item or heading.
    """
    sure: set[int] = set()
    maybe: set[int] = set()
    offset = 0
    depth = None
    in_list = False                  # whether a list item came since the last blank line
    after_heading = after_row = False
    for line in text.split("\n"):
        quote = _QUOTE_RE.match(line).group(0)
        start = _BLOCK_START_RE.match(line, len(quote))
        if not line[len(quote):].strip():
            in_list = after_heading = after_row = False
        elif depth is not None:
            if after_heading or (start and _surely_starts(start, in_list)):
                sure.add(offset)
            elif start or after_row or quote.count(">") != depth:
                maybe.add(offset)
        if start and (start.group("bullet") or start.group("number")):
            in_list = True
        after_heading = bool(start and start.group("heading"))
        after_row = bool(start) and not (start.group("heading") or start.group("bullet") or start.group("number"))
        depth = quote.count(">")
        offset += len(line) + 1
    return sure, maybe


def _surely_starts(start: re.Match[str], in_list: bool) -> bool:
    """Whether a `_BLOCK_START_RE` match surely starts a block, even right after a paragraph line."""
    shallow = _width(start.group("indent")) < 4
    if start.group("heading") or start.group("bullet"):
        return shallow or in_list
    if start.group("number"):
        return in_list or (shallow and int(start.group("number")) == 1)
    return False                     # a table row: only one if a table is there


def _blank_code_spans(text: str, ends: list[int]) -> str:
    """`text` with its inline code spans blanked: a backtick run closes at the next run of the same length.

    A span stops at the next offset of `ends` (sorted). A run with no partner is plain text.
    """
    runs = [(m.start(), m.end()) for m in re.finditer(r"`+", text)]
    chars = list(text)
    i = 0
    while i < len(runs):
        start, end = runs[i]
        if _escaped(text, start):
            # A backslash escapes the run's first backtick only; the rest of the run may still open a span.
            if end - start == 1:
                i += 1
                continue
            start += 1
            runs[i] = (start, end)
        k = bisect.bisect_right(ends, start)
        paragraph_end = ends[k] if k < len(ends) else len(text)
        partner = None
        for j in range(i + 1, len(runs)):
            if runs[j][0] >= paragraph_end:
                break
            if runs[j][1] - runs[j][0] == end - start:
                partner = j
                break
        if partner is None:
            i += 1
            continue
        for at in range(start, runs[partner][1]):
            if chars[at] != "\n":
                chars[at] = " "
        i = partner + 1
    return "".join(chars)


def _masked_lines(text: str) -> list[str]:
    """`text`'s lines with everything a `<` is not read as HTML in turned to spaces, columns kept.

    The frontmatter and code blocks go first, so a backtick or `$` inside them
    pairs with nothing; then code spans, so `$` and `%%` in code are ignored;
    then comments, math and wikilinks.
    """
    lines = text.split("\n")
    hidden = _code_lines(lines) | set(range(frontmatter_lines(lines)))
    masked = "\n".join(" " * len(line) if i in hidden else line for i, line in enumerate(lines))
    masked = _blank_all_code_spans(masked)
    for pattern in (_COMMENT_RE, _DISPLAY_MATH_RE, _INLINE_MATH_RE, _WIKILINK_RE):
        masked = _blank_matches(masked, pattern)
    return masked.split("\n")


def _escaped(line: str, at: int) -> bool:
    """Whether the character at `at` follows an odd run of backslashes."""
    run = 0
    while at - run - 1 >= 0 and line[at - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _tag_starts(line: str, visible: str) -> list[int]:
    """Where `line` has a `<` Obsidian reads as a tag: visible after masking, not escaped, no autolink or link."""
    return [m.start() for m in _TAG_START_RE.finditer(visible)
            if line[m.start()] == "<" and not _escaped(line, m.start())
            and not _AUTOLINK_RE.match(line, m.start()) and not line[:m.start()].endswith("](")
            and not _LINK_DEFINITION_RE.fullmatch(line, 0, m.start())]


def _held_tags(text: str | None) -> set[str]:
    """The tags `text` holds where Obsidian reads them as HTML, as `_TOKEN_RE` cuts them."""
    if not text or "<" not in text:
        return set()
    lines = text.split("\n")
    return {_TOKEN_RE.match(line, at).group(0)
            for line, visible in zip(lines, _masked_lines(text), strict=True)
            for at in _tag_starts(line, visible)}


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
    changed = {i for i in changed_lines(old, new) if "<" in lines[i] and not _CHECKBOX_RE.match(lines[i])}
    if not changed:
        return new, report
    masked = _masked_lines(new)
    held = _held_tags(old)
    for i in sorted(changed):
        line = lines[i]
        inserts = []
        for at in _tag_starts(line, masked[i]):
            token = _TOKEN_RE.match(line, at).group(0)
            if token in held:
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
            f"the note's view, so these {verb} a backslash in front (`\\<` displays as `<`):\n{listed}\n"
            "To keep one as HTML instead, edit it back in place with `allow_html=true`: `find` the escaped text "
            "(`\\<tag>`) and give the tag as content, or resend a full or section replace. "
            "Not for the owner: say nothing of it unless they ask.")


GUIDE = (
    "## Angle brackets (this server)\n\n"
    "Obsidian reads a `<` before a letter, `/` or `!` as the start of an HTML tag: a placeholder such as `<name>` "
    "breaks the rest of the note in Live Preview and vanishes in Reading view. So `create_note` and `edit_note` "
    "put a backslash in front of such a `<` on the lines a write adds or changes (`\\<name>` shows as `<name>`), "
    "and list each one under `angle_brackets_escaped:`; the reported `content_hash` is of what was written. Left "
    "alone: code (fenced at any indentation, inline, indented), the frontmatter, math, `%%` comments, wikilinks, "
    "autolinks (`<https://…>`), link destinations in angle brackets (`[text](<a b.md>)`), a `<` already escaped, a "
    "tag the note already held as HTML before the write (the same text, such as `<br>`, anywhere in it), and "
    "checkbox lines. To write HTML on purpose (`<br>`, `<details>`, `<kbd>`, an `<iframe>`), pass "
    "`allow_html=true`: nothing in that write is escaped, placeholders included, so escape those by hand there "
    "(`\\<name>`). A write is never refused for it, and `write_file` writes bytes as sent."
)
