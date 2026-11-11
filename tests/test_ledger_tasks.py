"""How text cites agent-ledger entries (`[F12]`, `[Q14, P3]`, a bare Q14 in
ledger text), and the task links built on it.

Pure parsing and state rules; the database side is in
`tests/integration/test_ledger_pg.py`.
"""
import re
from types import SimpleNamespace

import pytest

from src.services import ledger_citations as lc
from src.services import ledger_tasks as lt


def refs(items):
    return [str(r) for r in items]


@pytest.mark.parametrize("text,cited", [
    ("Fix it [F37] #area/x", ["F37"]),
    ("[F037] and [F37]", ["F37"]),                       # leading zeros don't matter
    ("[Q14] then [P35, P48]", ["Q14", "P35", "P48"]),
    ("[ P35 ,F2,\tQ1 ]", ["P35", "F2", "Q1"]),           # any kinds mixed, ASCII spaces
    ("(see [Q14]).", ["Q14"]),
    ("x [F12]: y", ["F12"]),                             # a colon mid-line is prose
    ("[f12] [F12x] [F] [F1234567]", []),                 # capitals, whole refs, 1-6 digits
    ("[F12, see]", []),                                  # a bracket holds refs only
    ("[F12](https://x) [F12][r] [r][F12] ![F12]", []),   # Markdown links and images
    ("[F12]: https://x", []),                            # a reference definition
    ("   [F12]: https://x", []),
    ("[[F12]] [[Note|F12]] [^F12]", []),                 # wikilinks, footnotes
    ("convert `ledger F12` to `[F12]`", []),             # inline code: an example, not a citation
    ("``a `[F1]` b`` and [F2]", ["F2"]),
    ("```\n[Q14]\n```\n[F1]", ["F1"]),                   # fenced code
    ("~~~md\n[Q14]\n~~~", []),
    ("- [ ] #task do it [F3] #area/x", ["F3"]),          # a checkbox is not a ref
    ("ledger F12", []),
    ("", []),
])
def test_bracketed(text, cited):
    assert refs(lc.bracketed(text)) == cited


@pytest.mark.parametrize("text,loose", [
    ("see F12 later", ["F12"]),
    ("[F12] and F13, Q4 and P1.", ["F13", "Q4", "P1"]),
    ("[P35, P48]", []),                                  # inside a bracket: cited, not bare
    ("ledger F12", ["F12"]),
    ("#F12 tag, path/F12, f12, F12x, [^F12], AF12", []),
    ("F05", ["F5"]),
    ("`F12` in code", []),
])
def test_bare(text, loose):
    assert refs(lc.bare(text)) == loose


def test_ledger_text_counts_bare_and_bracketed_refs_in_order():
    text = "F58 said untested (Q14); see [P35, F58] and `Q99`."
    assert refs(lc.in_ledger_text(text)) == ["F58", "Q14", "P35"]


def test_lines_citing_counts_bracketed_refs_only():
    text = "intro Q14\n- rule [P41]\n`[Q14]`\nsee [Q014, P2]\n"
    assert lc.lines_citing(text, lc.Ref("Q", 14)) == [4]
    assert lc.lines_citing(text, lc.Ref("P", 41)) == [2]


@pytest.mark.parametrize("text", ["[F12]", "[ \tQ1, P2]", "x [P35, P48] y", "[Q014]"])
def test_the_sql_prefilter_keeps_every_cited_line(text):
    # PostgreSQL's POSIX regex reads this pattern as Python does.
    assert lc.bracketed(text) and re.search(lc.SQL_BRACKET_PREFILTER, text)


@pytest.mark.parametrize("text", ["Q14", "[Q14]", "Q014.", "x Q14"])
def test_the_sql_ref_pattern_keeps_every_citing_text(text):
    assert re.search(lc.sql_ref_pattern(lc.Ref("Q", 14)), text)
    assert not re.search(lc.sql_ref_pattern(lc.Ref("Q", 14)), "Q140")


@pytest.mark.parametrize("types,state", [
    ([], None),
    (["NON_TASK"], None),
    (["TODO"], "open"),
    (["IN_PROGRESS", "DONE"], "open"),
    (["ON_HOLD"], "open"),
    (["DONE"], "done"),
    (["DONE", "CANCELLED"], "done"),
    (["CANCELLED", "CANCELLED"], "cancelled"),
    (["CANCELLED", "NON_TASK"], "cancelled"),
])
def test_task_state(types, state):
    assert lt.task_state(types) == state


def test_link_groups_tasks_by_cited_ref_of_any_kind_and_skips_non_tasks():
    rows = [
        SimpleNamespace(file_path="a.md", line_number=3, line="- [x] one [F1, Q2]"),
        SimpleNamespace(file_path="b.md", line_number=1, line="- [ ] two [F01]"),
        SimpleNamespace(file_path="c.md", line_number=2, line="- [ ] not a task for the plugin [F1]"),
        SimpleNamespace(file_path="d.md", line_number=4, line="- [ ] mentions F1 only"),
    ]
    status = {"x": "DONE", " ": "TODO"}

    def parse(line, path):
        if path == "c.md":
            return None
        return SimpleNamespace(status=SimpleNamespace(type=status[line[3]]))

    links = lt.link(rows, parse)
    assert {r: [t.ref for t in ts] for r, ts in links.items()} == {"F1": ["a.md:3", "b.md:1"], "Q2": ["a.md:3"]}
    assert lt.states(links) == {"F1": "open", "Q2": "done"}
    assert links["Q2"][0].as_dict() == {"ref": "a.md:3", "status": "DONE", "text": rows[0].line}


def test_canonical_drops_leading_zeros():
    assert lt.canonical("Q014") == "Q14" and lt.canonical("F3") == "F3"
