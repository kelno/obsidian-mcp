"""Bare-name wikilinks get the note's full path on the lines a write adds or changes."""
import re

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
    ("[[a]][[Areas]]", "[[a]][[Areas/Areas|Areas]]"),
])
def test_a_bare_name_naming_one_note_gets_its_full_path(written, expected):
    text, report = run(written)
    assert text == expected
    assert report.rewritten and report.rewritten[0][0] == 1


TABLE = "| h | h |\n| --- | :-: |\n"


@pytest.mark.parametrize("written,expected", [
    (TABLE + "| a | [[Areas]] |", TABLE + "| a | [[Areas/Areas\\|Areas]] |"),
    (TABLE + "| a | [[Areas\\|A]] |", TABLE + "| a | [[Areas/Areas\\|A]] |"),
    (TABLE + "| [[Areas#H\\|Y]] | b |", TABLE + "| [[Areas/Areas#H\\|Y]] | b |"),   # after an anchor
    (TABLE + "| [[Areas|A]] | b |", TABLE + "| [[Areas/Areas\\|A]] | b |"),        # a pipe the row needs
    ("> | h | h |\n> |---|---|\n> | [[Areas]] | x |",
     "> | h | h |\n> |---|---|\n> | [[Areas/Areas\\|Areas]] | x |"),                # a table in a callout
    ("h | h\n- | -\na | [[Areas]]", "h | h\n- | -\na | [[Areas/Areas\\|Areas]]"),   # rows without leading pipes
    (TABLE + "[[Areas\\|f]]", TABLE + "[[Areas/Areas\\|f]]"),                      # a body row with no pipe of its own
    ("| [[Areas]] | h |\n|---|---|", "| [[Areas/Areas\\|Areas]] | h |\n|---|---|"),  # the header row
])
def test_in_a_table_the_pipe_is_escaped(written, expected):
    text, report = run(written)
    assert text == expected
    assert report.rewritten


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


@pytest.mark.parametrize("written,expected", [
    # The pipe of another display-named link on the line is not a table's.
    ("See [[Areas/Areas|Areas]] and [[2026-11-04 Call|the call]].",
     "See [[Areas/Areas|Areas]] and [[Log/2026-11-04 Call|the call]]."),
    ("[[2026-11-04 Call|the call]] then [[Areas/Areas|Areas]]",
     "[[Log/2026-11-04 Call|the call]] then [[Areas/Areas|Areas]]"),
    ("[[Areas]] and [[Areas]]", "[[Areas/Areas|Areas]] and [[Areas/Areas|Areas]]"),
    # Nor is a pipe in code formatting.
    ("`a | b` then [[Areas]]", "`a | b` then [[Areas/Areas|Areas]]"),
    # An escaped pipe outside a table would break the link: it comes back plain.
    ("See [[Areas\\|A]] here", "See [[Areas/Areas|A]] here"),
    # A pipe in prose, in inline math or in a link's text makes no table without a delimiter row.
    ("opts: a | b, see [[Areas]]", "opts: a | b, see [[Areas/Areas|Areas]]"),
    ("norm $|x|$ see [[Areas]]", "norm $|x|$ see [[Areas/Areas|Areas]]"),
    ("See [[Q/X|F [d]]] and [[Areas]]", "See [[Q/X|F [d]]] and [[Areas/Areas|Areas]]"),
    ("| a | [[Areas]] |", "| a | [[Areas/Areas|Areas]] |"),
    # A blank line ends a table.
    (TABLE + "| a |\n\nSee [[Areas]]", TABLE + "| a |\n\nSee [[Areas/Areas|Areas]]"),
    # In a table, a row's other links leave it a table.
    (TABLE + "| [[Areas/Areas\\|Areas]] | [[2026-11-04 Call]] |",
     TABLE + "| [[Areas/Areas\\|Areas]] | [[Log/2026-11-04 Call\\|2026-11-04 Call]] |"),
])
def test_only_a_table_row_escapes_the_pipe(written, expected):
    text, report = run(written)
    assert text == expected
    assert report.rewritten


@pytest.mark.parametrize("written", [
    "[[Recent|Recent]]",                                                  # a root-level note's path is its name
    "See [[CLAUDE|the vault's CLAUDE.md]], and [[Areas/Areas|Areas]].",  # beside another display-named link
    TABLE + "| [[Recent\\|Recent]] | x |",
])
def test_a_link_already_at_its_full_path_is_neither_changed_nor_reported(written):
    text, report = run(written, notes=NOTES + ["Recent.md", "CLAUDE.md"])
    assert text == written
    assert not report.rewritten


# --- only the notes the links may name are read (speed, 2026-11-19) ------------------

#: Names Python's `lower` treats unusually: case, LIKE's own characters, accents, the Kelvin sign, `İ`.
TRICKY = NOTES + [
    "Recent.md", "Ref/Under_score.md", "Ref/UnderXscore.md", "Ref/100%.md", "Ref/1000.md", "Ref/ÉTÉ.md",
    "Old/été.md", "Ref/Kelvin.md", "Ref/kelvin.md", "Ref/İstanbul.md", "Ref/UPPER.MD", "Ref/..md",
    "Ref/a.b.md", "Ref/back\\slash.md", "Ref/photo.png.md", "Ref/Areas.md.md", "Ref/tab\there.md",
]


def like(pattern: str, value: str) -> bool:
    """PostgreSQL's LIKE with `\\` as the escape character."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            out += re.escape(pattern[i + 1])
            i += 2
            continue
        out += ".*" if c == "%" else "." if c == "_" else re.escape(c)
        i += 1
    return re.fullmatch(out, value, re.S) is not None


def narrowed(old, new, notes):
    """The notes the database would return for `lookups`' patterns."""
    patterns = lp.lookups(lp.prepare(old, new))
    if patterns is None:
        return list(notes)
    fold = str.maketrans(lp.FOLD_FROM, lp.FOLD_TO)
    return [p for p in notes if any(like(pat, p.translate(fold)) for pat in patterns)]


@pytest.mark.parametrize("written", [
    "See [[Areas]] and [[areas#Rows]] and ![[Areas]] and [[Areas.md]] and [[Champignons]] and [[Not yet]]",
    "[[Areas/Gone|g]] [[areas/areas]] [[AREAS/AREAS.MD]] [[Reference/v1.2 notes]] [[v1.2 notes]]",
    "[[under_score]] [[UNDERXSCORE]] [[100%]] [[1000]] [[été]] [[ÉTÉ]] [[kelvin]] [[KELVIN]]",
    "[[İstanbul]] [[i̇stanbul]] [[upper]] [[Ref/upper]] [[..md]] [[..md.md]] [[a.b]] [[A.B.md]]",
    "[[back\\slash]] [[photo.png]] ![[photo.png]] [[photo.png.md]] [[Areas.md.md]] [[tab\there]]",
    TABLE + "| [[Areas\\|A]] | [[Recent]] |",
    "`[[Areas]]` and [[Recent]]",
    "- [ ] task [[Areas]]",
])
def test_the_narrowed_note_list_gives_the_same_rewrite(written):
    full_text, full_report = lp.rewrite(None, written, lp.NoteIndex(TRICKY))
    notes = narrowed(None, written, TRICKY)
    assert lp.rewrite(None, written, lp.NoteIndex(notes)) == (full_text, full_report)


def test_the_narrowed_note_list_is_the_named_notes():
    assert narrowed(None, "See [[Areas]] and [[log/2026-11-04 call]]", TRICKY) == [
        "Areas/Areas.md", "Log/2026-11-04 Call.md"]
    assert narrowed("old [[Areas]]", "old [[Areas]]\nnew", TRICKY) == []      # an unchanged line looks nothing up
    assert lp.lookups(lp.prepare(None, "[[i̇stanbul]]")) is None        # `İ` lowers to two characters
    assert lp.lookups(lp.prepare(None, " ".join(f"[[n{i}]]" for i in range(120)))) is None  # too many
