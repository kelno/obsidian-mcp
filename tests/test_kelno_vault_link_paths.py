"""Bare-name wikilinks get the note's full path on the lines a write adds or changes."""
import pytest

from src.plugins.kelno_vault import link_paths as lp

NOTES = [
    "Areas/Areas.md",
    "Areas/Champignons/Champignons.md",
    "Archive/Areas/Champignons/Champignons.md",
    "Reference/v1.2 notes.md",
    "Log/2026-11-04 Call.md",
]


def run(new, old=None, notes=NOTES):
    return lp.rewrite(old, new, lp.NoteIndex(notes))


@pytest.mark.parametrize("written,expected", [
    ("See [[Areas]].", "See [[Areas/Areas|Areas]]."),
    ("See [[areas]].", "See [[Areas/Areas|areas]]."),                         # Obsidian matches any case
    ("[[Areas|the list]]", "[[Areas/Areas|the list]]"),                       # display text kept
    ("[[Areas#Rows]]", "[[Areas/Areas#Rows|Areas > Rows]]"),                  # reads as Obsidian showed it
    ("[[Areas#^b1|x]]", "[[Areas/Areas#^b1|x]]"),
    ("![[Areas]]", "![[Areas/Areas]]"),                                       # an embed gets no display text
    ("![[Areas#Rows]]", "![[Areas/Areas#Rows]]"),
    ("[[Areas.md]]", "[[Areas/Areas|Areas.md]]"),                             # the name as written
    ("[[Areas#A#B]]", "[[Areas/Areas#A#B|Areas > A > B]]"),
    ("[[v1.2 notes]]", "[[Reference/v1.2 notes|v1.2 notes]]"),                # a dot in a note's name
    ("| a | [[Areas]] |", "| a | [[Areas/Areas\\|Areas]] |"),                 # table: escaped pipe
    ("| a | [[Areas\\|A]] |", "| a | [[Areas/Areas\\|A]] |"),
    ("| [[Areas#H\\|Y]] | b |", "| [[Areas/Areas#H\\|Y]] | b |"),           # escaped pipe after an anchor
    ("> | [[Areas]] | x |", "> | [[Areas/Areas\\|Areas]] | x |"),             # a table in a callout
    ("a | [[Areas]]", "a | [[Areas/Areas\\|Areas]]"),                         # a row without a leading pipe
    ("[[a]][[Areas]]", "[[a]][[Areas/Areas|Areas]]"),
])
def test_a_bare_name_naming_one_note_gets_its_full_path(written, expected):
    text, report = run(written)
    assert text == expected
    assert report.rewritten and report.rewritten[0][0] == 1


@pytest.mark.parametrize("written", [
    "[[Areas/Areas|Areas]]",           # already a path
    "![[photo.png]]",                  # an attachment
    "`[[Areas]]` in code",
    "```\n[[Areas]]\n```",
    "---\nrelated: \"[[Areas]]\"\n---\nBody",   # frontmatter is left alone
    "\ufeff---\nrelated: \"[[Areas]]\"\n---\nBody",
    "See [[Areas|`cfg`]] now",                   # code inside the link: not touched
    "[[Areas|a`]] and [[Home|`b]]",              # code across two links: not touched
    "> ```\n> [[Areas]]\n> ```",                # a fence inside a quote
    "- [ ] #task see [[Areas]]",                 # a task's line is the task tools' to change
    "> - [x] done [[Areas]]",
])
def test_left_alone(written):
    text, report = run(written)
    assert text == written and not report.rewritten


def test_ambiguous_and_unknown_names_are_flagged_not_rewritten():
    text, report = run("[[Champignons]] and [[Not yet]] and [[Areas/Gone|g]]")
    assert text == "[[Champignons]] and [[Not yet]] and [[Areas/Gone|g]]"
    assert report.ambiguous == [(1, "[[Champignons]]", ["Archive/Areas/Champignons/Champignons.md",
                                                        "Areas/Champignons/Champignons.md"])]
    assert [r[1] for r in report.unresolved] == ["[[Not yet]]", "[[Areas/Gone|g]]"]
    out = lp.render(report)
    assert "links_flagged:" in out and "names several notes" in out and "still to create" in out


def test_a_path_in_code_after_a_link_is_flagged():
    _text, report = run("[[Areas/Areas|Areas]] (`Areas/Areas.md`)")
    assert report.path_after == [(1, "`Areas/Areas.md`")]
    _text, report = run("[[Areas/Areas|Areas]] `npm test`")
    assert report.path_after == []


def test_only_lines_the_write_adds_or_changes_are_touched():
    old = "[[Areas]] kept as it was\nold line\n"
    new = "[[Areas]] kept as it was\nnew line with [[Areas]]\n"
    text, report = run(new, old)
    assert text == "[[Areas]] kept as it was\nnew line with [[Areas/Areas|Areas]]\n"
    assert report.rewritten == [(2, "[[Areas]]", "[[Areas/Areas|Areas]]")]


def test_render_says_got_or_would_get():
    _t, report = run("[[Areas]]")
    assert "these bare-name links got the note's full path" in lp.render(report)
    assert "would get" in lp.render(report, dry_run=True)
    assert "- line 1: [[Areas]] → [[Areas/Areas|Areas]]" in lp.render(report)



def test_line_endings_and_indentation_do_not_make_a_line_new():
    old = "  [[Areas]] stays\r\nother\r\n"
    new = "[[Areas]] stays\nother changed\n"
    text, report = run(new, old)
    assert text == new and not report.rewritten


def test_names_match_as_obsidian_lowercases_and_case_twins_are_ambiguous():
    _text, report = run("[[Strasse]]", notes=["S/Straße.md"])
    assert report.unresolved and not report.rewritten
    _text, report = run("[[Note]]", notes=["A/Note.md", "A/note.md"])
    assert report.ambiguous


def test_shifted_moves_line_numbers():
    _t, report = run("[[Areas]]")
    assert lp.shifted(report, 3).rewritten[0][0] == 4
