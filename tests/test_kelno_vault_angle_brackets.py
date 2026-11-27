"""The vault plugin's `<` escape: what it escapes, what it leaves alone, and how it reports."""

import pytest

from src import plugins
from src.plugins import kelno_vault
from src.plugins.kelno_vault import angle_brackets


def esc(new, old=None, **kw):
    text, report = angle_brackets.escape(old, new, **kw)
    return text, [token for _, token in report.escaped]


@pytest.mark.parametrize("before, after", [
    ("Put <name> here.", "Put \\<name> here."),
    ("<name>", "\\<name>"),
    ("- the <name> item", "- the \\<name> item"),
    ("## Angle <note> in a heading", "## Angle \\<note> in a heading"),
    ("<div> starts this line", "\\<div> starts this line"),
    ("a stray <!-- here", "a stray \\<!-- here"),
    ("closing </b> alone", "closing \\</b> alone"),
    ("Compare x<y here", "Compare x\\<y here"),
    ("two <a> and <b>", "two \\<a> and \\<b>"),
    ("> quoted <name>", "> quoted \\<name>"),
    ("| cell <name> | x |", "| cell \\<name> | x |"),
])
def test_escapes_what_obsidian_reads_as_a_tag(before, after):
    assert esc(before)[0] == after


@pytest.mark.parametrize("text", [
    "a < b, x <= y, I <3 this, a <- b, <% tp.date %>, 5<6",
    "already \\<name> escaped",
    "inline `<name>` code",
    "``code with ` and <name>``",
    "```\n<name>\n```",
    "~~~js\nif (a <b) {}\n~~~",
    "> ```\n> <name>\n> ```",
    "para\n\n    <indented> code",
    "math $a<b$ inline",
    "$$\nx <y\n$$",
    "%% a comment <name> %%",
    "%%\n<name>\n%%",
    "an autolink <https://example.com/x>",
    "a mail <someone@example.com>",
    "[link](<My note.md>)",
    "- [ ] a checkbox <name>",
])
def test_leaves_alone(text):
    assert esc(text) == (text, [])


def test_list_continuation_four_columns_in_is_text_not_code():
    text = "- item\n\n    continued <name>"
    assert esc(text)[0] == "- item\n\n    continued \\<name>"


def test_frontmatter_is_left_alone():
    text = "---\ntitle: a <name>\n---\nbody <name>"
    assert esc(text)[0] == "---\ntitle: a <name>\n---\nbody \\<name>"


def test_allow_html_escapes_nothing():
    assert esc("<details><summary>x</summary>y</details>", allow_html=True) == (
        "<details><summary>x</summary>y</details>", [])


def test_only_lines_the_write_adds_or_changes():
    old = "keep <br> here\n"
    new = "keep <br> here\nnew <name>\n"
    assert esc(new, old)[0] == "keep <br> here\nnew \\<name>\n"


def test_a_tag_the_note_already_held_stays_on_a_changed_line():
    old = "a<br>b\n"
    new = "a<br>b, now changed <name>\n"
    assert esc(new, old) == ("a<br>b, now changed \\<name>\n", ["<name>"])


def test_escaped_tag_in_old_does_not_shield_a_new_bare_one():
    old = "was \\<name>\n"
    assert esc("now <name>\n", old)[0] == "now \\<name>\n"


def test_crlf_and_lone_cr_notes():
    assert esc("a\r\n<name>\r\n")[0] == "a\r\n\\<name>\r\n"
    assert esc("a\r<name>\r")[0] == "a\r\\<name>\r"


def test_report_names_lines_and_tags():
    _text, report = angle_brackets.escape(None, "x\nPut <name> and <job x>.\n")
    assert report.escaped == [(1, "<name>"), (1, "<job x>")]
    block = angle_brackets.render(angle_brackets.shifted(report, 2))
    assert "angle_brackets_escaped:" in block
    assert "- line 4: `<name>`" in block and "- line 4: `<job x>`" in block
    assert "allow_html=true" in block
    assert "would get" in angle_brackets.render(report, dry_run=True)
    assert angle_brackets.render(angle_brackets.EscapeReport()) == ""


async def test_plugin_rewrite_escapes_for_note_tools_only():
    for tool in ("create_note", "edit_note"):
        rewrite = await kelno_vault._rewrite(plugins.NoteWrite(tool, "Inbox/x.md", None, "a <name>\n", False, None))
        assert rewrite.text == "a \\<name>\n"
        assert "angle_brackets_escaped:" in rewrite.report(0, False)
    rewrite = await kelno_vault._rewrite(plugins.NoteWrite("write_file", "Inbox/x.md", None, "a <name>\n", False, None))
    assert rewrite.text == "a <name>\n"


async def test_plugin_rewrite_honours_allow_html():
    write = plugins.NoteWrite("edit_note", "Inbox/x.md", "a\n", "a\n<kbd>x</kbd>\n", False, None, allow_html=True)
    rewrite = await kelno_vault._rewrite(write)
    assert rewrite.text == "a\n<kbd>x</kbd>\n"
    assert "angle_brackets_escaped:" not in rewrite.report(0, False)


def test_escape_stays_within_the_plugin_scope_guard():
    old = "kept <br> line\nother\n"
    new = "kept <br> line\nother <name>\n"
    text, _ = angle_brackets.escape(old, new)
    assert not plugins._out_of_scope(old, new, text)
