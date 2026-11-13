"""Which lines of a note are task lines (`src/services/fork_tasks/scan.py`).

The Tasks plugin only sees list items Obsidian's own parser marks as tasks, so
code blocks, frontmatter and lines that are not list items never count. What
cannot be decided with certainty is reported as a problem, never dropped.
Each case names the Tasks query it stands for where one applies: the expected
answer of `not done` over the note.
"""

import pytest

from src.services.fork_tasks import scan as ts


def kinds(text):
    return [(s.line_number, s.kind, s.reason) for s in ts.scan_note(text).lines]


def test_plain_tasks_and_line_numbers_are_one_based_over_the_whole_file():
    text = "---\ntags: [a]\n---\n# T\n- [ ] one\n- [x] two\n"
    assert kinds(text) == [(5, "task", None), (6, "task", None)]


def test_frontmatter_is_ignored():
    text = "---\nx: |\n  - [ ] not a task\n---\n- [ ] real\n"
    assert kinds(text) == [(5, "task", None)]


@pytest.mark.parametrize("fence", ["```", "~~~", "````"])
def test_fenced_code_is_ignored(fence):
    text = f"- [ ] before\n{fence}\n- [ ] in code\n{fence}\n- [ ] after\n"
    assert kinds(text) == [(1, "task", None), (5, "task", None)]


def test_unclosed_column_zero_fence_runs_to_the_end():
    assert kinds("- [ ] a\n```\n- [ ] b\n") == [(1, "task", None)]


def test_fence_inside_a_list_item_is_ignored():
    text = "- item\n  ```\n  - [ ] code\n  ```\n  - [ ] real\n"
    assert kinds(text) == [(5, "task", None)]


def test_escaped_checkbox_is_not_a_task_and_not_a_problem():
    # The vault's legacy lines were converted to this form on purpose.
    assert kinds("- \\[ ] legacy\n- \\[x] legacy done\n") == []


def test_blockquote_and_callout_tasks():
    text = "> [!note] Title\n> - [ ] in callout\n> > - [ ] nested quote\n"
    assert kinds(text) == [(2, "task", None), (3, "task", None)]


def test_items_after_blank_lines_and_nested_items():
    text = "- [ ] a\n\n- [ ] b\n    - [ ] deep under b\n\n        - [ ] deeper after blank\n"
    assert [k for _, k, _ in kinds(text)] == ["task", "task", "task", "task"]


def test_indented_four_spaces_outside_a_list_is_a_problem():
    text = "Paragraph.\n\n    - [ ] indented code block\n"
    assert kinds(text) == [(3, "problem", "indented_outside_list")]


def test_html_block_is_a_problem():
    text = "<div>\n- [ ] inside html\n</div>\n\n- [ ] after\n"
    assert kinds(text) == [(2, "problem", "inside_html_block"), (5, "task", None)]


def test_html_comment_block_is_a_problem():
    text = "<!--\n- [ ] commented\n-->\n- [ ] after\n"
    assert kinds(text) == [(2, "problem", "inside_html_block"), (4, "task", None)]


def test_obsidian_comment_is_a_problem():
    text = "%%\n- [ ] in a comment\n%%\n- [ ] after %% inline %% comment\n"
    assert kinds(text) == [(2, "problem", "inside_comment"), (4, "task", None)]


def test_percent_in_inline_code_does_not_open_a_comment():
    assert kinds("- [ ] use `%%` here\n- [ ] next\n") == [(1, "task", None), (2, "task", None)]


@pytest.mark.parametrize("line,reason", [
    ("-\t[ ] tab after marker", "whitespace_after_marker"),
    ("- [ ] nbsp after marker", "whitespace_after_marker"),
    ("-[ ] no space", "no_space_before_checkbox"),
    ("- [] empty", "empty_checkbox"),
    ("- [ ]text glued", "no_space_after_checkbox"),
])
def test_near_misses_are_reported(line, reason):
    assert kinds(line + "\n") == [(1, "problem", reason)]


@pytest.mark.parametrize("line", [
    "- [link](https://example.org)",
    "- [[wikilink]]",
    "- plain item",
    "text with [ ] brackets",
    "- [ ]",          # a bare checkbox at end of line is a task
])
def test_ordinary_lines_are_not_problems(line):
    result = kinds(line + "\n")
    assert all(kind != "problem" for _, kind, _ in result)


def test_bare_checkbox_is_a_task():
    assert kinds("- [ ]\n") == [(1, "task", None)]


def test_crlf_and_lone_cr_lines():
    assert kinds("- [ ] a\r\n- [ ] b\r- [ ] c") == [(1, "task", None), (2, "task", None), (3, "task", None)]


def test_overlong_line_is_a_problem_with_truncated_text():
    line = "- [ ] " + "a" * (ts.MAX_TASK_LINE_CHARS + 1)
    [scanned] = ts.scan_note(line).lines
    assert scanned.kind == "problem" and scanned.reason == "line_too_long"
    assert len(scanned.text) <= ts.PROBLEM_TEXT_CHARS


def test_row_cap_is_declared(monkeypatch):
    monkeypatch.setattr(ts, "MAX_TASK_ROWS_PER_NOTE", 3)
    result = ts.scan_note("".join(f"- [ ] t{i}\n" for i in range(5)))
    assert [s.kind for s in result.lines] == ["task", "task", "task", "problem"]
    assert result.lines[-1].reason == "rows_capped"
    assert result.lines[-1].line_number == 4


def test_unmatched_indented_fence_opener_is_reported():
    text = "- item\n  ```\n- [ ] after\n"
    result = ts.scan_note(text)
    assert result.unmatched_fence_lines == (2,)


def test_lines_with_terminators_round_trip():
    text = "a\r\nb\rc\nd"
    lines = ts.split_lines(text)
    assert "".join(content + term for content, term in lines) == text
    assert [content for content, _ in lines] == ["a", "b", "c", "d"]


# --- found by the adversarial review ---------------------------------------------

def test_line_separator_inside_a_task_line_is_a_problem():
    # The plugin's pattern stops at U+2028; a rewrite would drop the rest.
    assert kinds("- [ ] call the bank 📅 2026-11-05\n") == [(1, "problem", "line_separator_in_line")]


@pytest.mark.parametrize("block", ["---\nlist:\n- [ ] x\n---\n", "---\ntags: [a\n- [ ] x\n---\n", "---\r\nlist:\r\n- [ ] x\r\n---\r\n"])
def test_a_task_line_in_a_defective_but_closed_frontmatter_block_is_a_problem(block):
    assert kinds(block + "- [ ] real\n") == [(3, "problem", "inside_frontmatter"), (5, "task", None)]


def test_an_unclosed_frontmatter_fence_is_not_frontmatter():
    assert kinds("---\n- [ ] x\n") == [(2, "task", None)]


@pytest.mark.parametrize("indent", [" ", "　"])
def test_unusual_whitespace_indentation_is_a_problem(indent):
    assert kinds(indent + "- [ ] x\n") == [(1, "problem", "unusual_indentation")]


def test_math_block_is_a_problem():
    assert kinds("$$\n- [ ] in math\n$$\n- [ ] after\n$$ x = 1 $$\n- [ ] inline math line\n") == [
        (2, "problem", "inside_math_block"), (4, "task", None), (6, "task", None),
    ]
