"""Which lines of a note are task lines, and which only look like one.

The Tasks plugin reads only the list items that Obsidian's own Markdown parser
reports as tasks (`FileParser.parseLine`: `listItem.task`), then applies its
line pattern. Obsidian's parser is not open source, so this scanner rebuilds
the parts of its decision that matter, one auditable clause per rule:

- **Frontmatter** — the repo's one partition (`parse_frontmatter_diagnose`).
- **Fenced code** — the repo's one fence grammar (`links.scan_fences`, full-note
  context). Its inline-code masking is not used: a task line may carry inline
  code. Lines below an indented opener with no closer are not code under that
  grammar; the scan reports those openers so the write tools can refuse such a
  note, as `edit_note(section=…)` does.
- **The plugin's line pattern** — `task_format.TASK_RE`.
- Everything that is certainly not a task (code, frontmatter, a line that is
  not list-item shaped) is skipped. Everything that might or might not be one
  becomes a **problem** row with a reason, so a caller always hears about it:
  - `inside_comment` — in an Obsidian `%% … %%` comment;
  - `inside_html_block` — in a CommonMark HTML block (types 1–6);
  - `indented_outside_list` — indented four or more columns outside a list,
    which CommonMark reads as code or paragraph text;
  - `no_space_after_checkbox` — `- [ ]text`: the plugin's pattern accepts it,
    but a Markdown task item needs whitespace after the box;
  - near misses: `whitespace_after_marker` (a tab or other non-space between
    the marker and the box), `no_space_before_checkbox` (`-[ ]`),
    `empty_checkbox` (`- []`);
  - `inside_frontmatter` — in a line-1 block whose fences close but whose
    YAML does not load: still frontmatter to Obsidian;
  - `inside_math_block` — in an Obsidian `$$ … $$` block;
  - `unusual_indentation` — indented with NBSP or another non-space, non-tab
    whitespace the plugin's pattern accepts and CommonMark does not;
  - `line_separator_in_line` — a U+2028 / U+2029 in the line: the plugin
    reads only the text before it, and a rewrite would drop the rest;
  - `line_too_long`, `rows_capped` — bounds, below.

Whether Obsidian itself lists the first four as tasks was not observable from
here; the parity notes in `docs/architecture/tasks.md` list them to check once
in Obsidian. An escaped `- \\[ ] text` (the vault's converted legacy lines)
matches nothing and is not a problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.services.links import FULL_NOTE, scan_fences, split_lines as _offsets
from src.services.fork_tasks.format import INDENTATION, LIST_MARKER, TASK_RE, JS_WHITESPACE

#: A task line longer than this is stored as a problem, its text truncated.
MAX_TASK_LINE_CHARS = 10_000
#: Rows stored per note; past it, one `rows_capped` problem row says so.
MAX_TASK_ROWS_PER_NOTE = 10_000
#: How much of a problem line's text is kept.
PROBLEM_TEXT_CHARS = 500

_WS_CLASS = "[" + re.escape(JS_WHITESPACE) + "]"
_NEAR_WHITESPACE = re.compile("^" + INDENTATION + LIST_MARKER + "(" + _WS_CLASS + r"+)\[[^\]]\]")
_NEAR_GLUED = re.compile("^" + INDENTATION + LIST_MARKER + r"\[[^\]]\](?:[ \t]|$)")
_NEAR_EMPTY = re.compile("^" + INDENTATION + LIST_MARKER + r" +\[\]")
_LIST_ITEM = re.compile(r"^[ \t]*([-*+]|[0-9]{1,9}[.)])(?:[ \t]|$)")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_INLINE_CODE = re.compile(r"`[^`\r\n]*`")

# CommonMark HTML block start conditions 1–6 and their end conditions. Type 7
# (any complete tag alone on a line) is not recognised: it cannot interrupt a
# paragraph and is rare in notes; declared in tasks.md.
_HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|"
    "dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h1|h2|h3|h4|h5|h6|"
    "head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|"
    "p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul"
)
_HTML_STARTS = (
    (re.compile(r"^ {0,3}<(?:script|pre|style|textarea)(?:[ \t>]|$)", re.I), re.compile(r"</(?:script|pre|style|textarea)>", re.I)),
    (re.compile(r"^ {0,3}<!--"), re.compile(r"-->")),
    (re.compile(r"^ {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r"^ {0,3}<![A-Za-z]"), re.compile(r">")),
    (re.compile(r"^ {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
    (re.compile(r"^ {0,3}</?(?:" + _HTML_BLOCK_TAGS + r")(?:[ \t]|/?>|$)", re.I), None),
)


@dataclass(frozen=True)
class ScannedLine:
    line_number: int          # 1-based, over the whole file, frontmatter included
    kind: str                 # "task" or "problem"
    text: str                 # the exact line, without its terminator
    reason: str | None = None


@dataclass(frozen=True)
class NoteScan:
    lines: tuple[ScannedLine, ...]
    #: 1-based lines holding an indented fence opener with no closer.
    unmatched_fence_lines: tuple[int, ...]


def split_lines(text: str) -> list[tuple[str, str]]:
    """`(content, terminator)` per line; joining them gives `text` back."""
    return [(text[s:c], text[c:e]) for s, c, e in _offsets(text)]


def _indent_width(line: str) -> int:
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _toggles_comment(line: str) -> int:
    return _INLINE_CODE.sub("", line).count("%%")


def _frontmatter_length(text: str) -> tuple[int, bool]:
    """`(length, defective)` of the line-1 frontmatter region.

    A valid block is the partition's own span. A block whose fences close but
    whose YAML does not load (`yaml_error`, `not_a_mapping`) is still
    frontmatter to Obsidian, which only flags its properties as invalid, so its
    span — found with the partition's own closing-fence pattern — is returned
    with `defective` set: lines in it are problems, never tasks. An unclosed
    fence is not frontmatter to anyone.
    """
    from src.services.vault import (
        _FRONTMATTER_CLOSING_RE, _LINE_BREAK_RE, parse_frontmatter_diagnose,
    )

    _fm, _body, diagnosis = parse_frontmatter_diagnose(text)
    if diagnosis.valid:
        return len(diagnosis.block), False
    if diagnosis.defect not in ("yaml_error", "not_a_mapping"):
        return 0, False
    first_break = _LINE_BREAK_RE.search(text)
    closing = _FRONTMATTER_CLOSING_RE.search(text, first_break.end()) if first_break else None
    if closing is None:
        return 0, False
    return closing.end(), True


def scan_note(text: str) -> NoteScan:
    """Classify every line of a note (raw text, frontmatter included)."""
    offsets = _offsets(text)
    frontmatter_end, frontmatter_defective = _frontmatter_length(text)
    fences = scan_fences(text, context=FULL_NOTE)
    spans = fences.spans
    unmatched = tuple(sorted(o.line for o in fences.unmatched_indented_openers))

    out: list[ScannedLine] = []
    span_index = 0
    in_comment = False
    html_end: re.Pattern | None = None
    in_html = False
    in_list = False
    in_math = False
    prev_blank = True

    def add(number: int, kind: str, line: str, reason: str | None = None) -> bool:
        if len(out) >= MAX_TASK_ROWS_PER_NOTE:
            out.append(ScannedLine(number, "problem", "", "rows_capped"))
            return False
        if kind == "problem":
            line = line[:PROBLEM_TEXT_CHARS]
        out.append(ScannedLine(number, kind, line, reason))
        return True

    for index, (start, content_end, _end) in enumerate(offsets):
        number = index + 1
        line = text[start:content_end]
        if start < frontmatter_end:
            if frontmatter_defective and TASK_RE.match(line) and not add(number, "problem", line, "inside_frontmatter"):
                break
            continue
        while span_index < len(spans) and spans[span_index][1] <= start:
            span_index += 1
        if span_index < len(spans) and spans[span_index][0] <= start < spans[span_index][1]:
            continue

        blank = line.strip(" \t") == ""
        # Obsidian's `$$` math block: its lines are LaTeX, not Markdown.
        stripped = line.strip(" \t")
        if not in_comment and not in_html and stripped.startswith("$$") and stripped.count("$$") % 2:
            in_math = not in_math
            continue
        if in_math:
            if TASK_RE.match(line) and not add(number, "problem", line, "inside_math_block"):
                break
            continue
        comment_at_start = in_comment
        if _toggles_comment(line) % 2:
            in_comment = not in_comment

        if in_html:
            if html_end is None and blank:
                in_html = False
            else:
                if html_end is not None and html_end.search(line):
                    in_html = False
                if TASK_RE.match(line) and not add(number, "problem", line, "inside_html_block"):
                    break
                prev_blank = blank
                continue
        if not comment_at_start:
            for start_re, end_re in _HTML_STARTS:
                if start_re.match(line):
                    in_html = not (end_re is not None and end_re.search(line))
                    html_end = end_re
                    break
            if in_html:
                prev_blank = False
                continue

        if blank:
            prev_blank = True
            continue

        verdict = _classify(line, in_list=in_list, in_comment=comment_at_start)
        if verdict is not None:
            kind, reason = verdict
            if not add(number, kind, line, reason):
                break

        if not comment_at_start:
            indent = _indent_width(line)
            if _LIST_ITEM.match(line) and (indent < 4 or in_list):
                in_list = True
            elif _HEADING.match(line):
                in_list = False
            elif prev_blank and indent < 2 and not line.lstrip(" \t").startswith(">"):
                in_list = False
        prev_blank = False
    return NoteScan(tuple(out), unmatched)


def _classify(line: str, *, in_list: bool, in_comment: bool) -> tuple[str, str | None] | None:
    match = TASK_RE.match(line)
    if match is None:
        for pattern, reason in ((_NEAR_WHITESPACE, "whitespace_after_marker"),
                                (_NEAR_GLUED, "no_space_before_checkbox"),
                                (_NEAR_EMPTY, "empty_checkbox")):
            near = pattern.match(line)
            # A run of spaces only is the real pattern, already refused above
            # for another reason; only report a run holding something else.
            if near is not None and not (reason == "whitespace_after_marker" and set(near.group(3)) == {" "}):
                return ("problem", "inside_comment") if in_comment else ("problem", reason)
        return None
    if in_comment:
        return ("problem", "inside_comment")
    indentation = match.group(1)
    if match.end() != len(line):
        # `_DOT` stops at U+2028 / U+2029: the plugin's pattern would read only
        # the text before it, and a rewrite would drop the rest of the line.
        return ("problem", "line_separator_in_line")
    if any(ch not in " \t>" for ch in indentation):
        # NBSP, U+3000 and the like are whitespace to the plugin's pattern but
        # not indentation to CommonMark, which makes the line a paragraph.
        return ("problem", "unusual_indentation")
    if ">" not in indentation and _indent_width(line) >= 4 and not in_list:
        return ("problem", "indented_outside_list")
    after_box = match.end(3) + 1
    if after_box < len(line) and line[after_box] not in JS_WHITESPACE:
        return ("problem", "no_space_after_checkbox")
    if len(line) > MAX_TASK_LINE_CHARS:
        return ("problem", "line_too_long")
    return ("task", None)
