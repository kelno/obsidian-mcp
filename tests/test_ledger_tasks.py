"""The citation that links a vault task to agent-ledger feedback (`ledger F12`).

Pure parsing and state rules; the database side is in
`tests/integration/test_ledger_pg.py`.
"""
from types import SimpleNamespace

import pytest

from src.services import ledger_tasks as lt


@pytest.mark.parametrize("text,numbers", [
    ("Fix it (ledger F37) #area/x", [37]),
    ("Ledger f37 and LEDGER F037", [37]),
    ("(ledger F9) then (ledger F10)", [9, 10]),
    ("ledger F9, F10", [9]),                 # one citation per entry
    ("ledger   F12", [12]),
    ("ledgers F12", []),                     # whole words only
    ("theledger F12", []),
    ("ledger F12x", []),
    ("ledger Q14 and ledger P52", []),       # only feedback counts
    ("see F12", []),
    ("", []),
])
def test_cited_numbers(text, numbers):
    assert lt.cited_numbers(text) == numbers


@pytest.mark.parametrize("text,numbers", [
    ("see F12 later", [12]),
    ("(ledger F12) and F13", [13]),
    ("ledger F12", []),
    ("#F12 tag, path/F12, f12, F12x", []),
    ("F05", [5]),
])
def test_bare_numbers(text, numbers):
    assert lt.bare_numbers(text) == numbers


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


def test_link_groups_tasks_by_cited_number_and_skips_non_tasks():
    rows = [
        SimpleNamespace(file_path="a.md", line_number=3, line="- [x] one (ledger F1) (ledger F2)"),
        SimpleNamespace(file_path="b.md", line_number=1, line="- [ ] two (ledger F1)"),
        SimpleNamespace(file_path="c.md", line_number=2, line="- [ ] not a task for the plugin (ledger F1)"),
        SimpleNamespace(file_path="d.md", line_number=4, line="- [ ] mentions ledgers only"),
    ]
    status = {"x": "DONE", " ": "TODO"}

    def parse(line, path):
        if path == "c.md":
            return None
        return SimpleNamespace(status=SimpleNamespace(type=status[line[3]]))

    links = lt.link(rows, parse)
    assert {n: [t.ref for t in ts] for n, ts in links.items()} == {1: ["a.md:3", "b.md:1"], 2: ["a.md:3"]}
    assert lt.states(links) == {1: "open", 2: "done"}
    assert links[2][0].as_dict() == {"ref": "a.md:3", "status": "DONE", "text": rows[0].line}
