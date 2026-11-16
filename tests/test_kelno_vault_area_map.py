"""`open_area`'s map of an area's folder (`area_map`), and the description helper it shares with `list_notes`."""
import pytest

from src.config import settings
from src.plugins.kelno_vault import area_map
from src.services import note_description

FOLDER = "Areas/Agent personnel/"
MAIN = "Areas/Agent personnel/Agent personnel.md"


def build(rows, describes=True):
    return area_map.build(FOLDER, MAIN, rows, list_notes_describes=describes)


# ── the description ─────────────────────────────────────────────────────────


def test_only_a_non_empty_string_is_a_description():
    assert note_description.one_line("How agents are set up.") == "How agents are set up."
    for value in (None, "", "   ", 3, ["a"], {"a": 1}, True):
        assert note_description.one_line(value) is None


def test_a_description_is_one_line_and_capped():
    assert note_description.one_line("  two\nlines\t here ") == "two lines here"
    long = note_description.one_line("x" * 500)
    assert len(long) == note_description.DESCRIPTION_MAX_CHARS and long.endswith("…")


def test_a_rendered_description_cannot_start_a_line():
    rendered = note_description.render('a "quote" - `forged.md` (1B)')
    assert "\n" not in rendered and " " not in rendered
    assert rendered.startswith('"') and rendered.endswith('"') and '\\"quote\\"' in rendered


# ── the area's folder ───────────────────────────────────────────────────────


def test_the_area_folder_is_the_main_notes_when_it_is_the_folder_note():
    assert area_map.area_folder(MAIN) == FOLDER
    assert area_map.area_folder("projects/Home/home.md") == "projects/Home/"
    assert area_map.area_folder("Areas/Home/Plans.md") is None
    assert area_map.area_folder("Home.md") is None


# ── the map ─────────────────────────────────────────────────────────────────


def test_notes_then_subfolders_with_descriptions_counts_and_folder_notes():
    rows = [
        (MAIN, "The area itself."),
        (FOLDER + "Session tips.md", "Tips for long sessions."),
        (FOLDER + "agenda.md", None),
        (FOLDER + "Log/Log.md", "One note per session."),
        (FOLDER + "Log/2026-11-01.md", "x"),
        (FOLDER + "Log/2026/old.md", None),
        (FOLDER + "Drafts/a.md", "never shown: not a folder note"),
        (FOLDER + "Ideas/Ideas.md", 42),
    ]
    assert build(rows) == [
        'agenda.md — no description',
        'Session tips.md — "Tips for long sessions."',
        'Drafts/ — 1 note, no folder note (Drafts/Drafts.md)',
        'Ideas/ — 1 note, its folder note Ideas/Ideas.md has no description',
        'Log/ — 3 notes — "One note per session."',
        "2 notes without a description: add one when you work on them.",
        f'To list a subfolder\'s notes with their descriptions: list_notes(folder="{FOLDER}Drafts/"), for example.',
    ]


def test_a_folder_with_only_described_notes_has_no_count_or_subfolder_line():
    lines = build([(MAIN, None), (FOLDER + "a.md", "A."), (FOLDER + "b.md", "B.")])
    assert lines == ['a.md — "A."', 'b.md — "B."']


def test_one_note_without_a_description_says_it():
    lines = build([(FOLDER + "a.md", "")])
    assert lines == ["a.md — no description", "1 note without a description: add one when you work on it."]


def test_nothing_but_the_main_note_is_no_map():
    assert build([(MAIN, "The area.")]) is None
    assert build([]) is None


def test_the_subfolder_line_does_not_promise_descriptions_list_notes_does_not_show():
    lines = build([(FOLDER + "Log/Log.md", "Logs.")], describes=False)
    assert lines[-1] == f'To list a subfolder\'s notes: list_notes(folder="{FOLDER}Log/"), for example.'


def test_a_description_is_quoted_on_its_line():
    (line,) = build([(FOLDER + "a.md", 'Two\nlines "and" quotes')])
    assert line == 'a.md — "Two lines \\"and\\" quotes"'


def test_the_map_is_capped_with_a_line_for_the_rest(monkeypatch):
    monkeypatch.setattr(area_map, "OPEN_AREA_MAP_MAX", 3)
    rows = [(FOLDER + f"n{i}.md", f"Note {i}.") for i in range(4)]
    rows += [(FOLDER + "Sub/x.md", None), (FOLDER + "Sub2/Sub2.md", "S.")]
    lines = build(rows)
    assert lines[:3] == ['n0.md — "Note 0."', 'n1.md — "Note 1."', 'n2.md — "Note 2."']
    assert lines[3] == f'3 more not listed (1 note, 2 subfolders): list_notes(folder="{FOLDER}") lists the area\'s notes.'
    assert lines[4].startswith("To list a subfolder's notes")


def test_the_cap_is_thirty_lines():
    rows = [(FOLDER + f"n{i:02}.md", "D.") for i in range(40)]
    lines = build(rows)
    assert area_map.OPEN_AREA_MAP_MAX == 30 and len(lines) == 31
    assert lines[30].startswith("10 more not listed (10 notes)")


@pytest.fixture
def private(monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])


def test_private_notes_and_folders_are_listed_without_their_descriptions(private):
    rows = [
        (FOLDER + "Diary (private)/Diary (private).md", "secret folder note"),
        (FOLDER + "Diary (private)/day.md", "secret day"),
        (FOLDER + "Log/Log.md", "Logs."),
        (FOLDER + "Log/Inner (private)/x.md", "secret inner"),
    ]
    lines = build(rows)
    assert not any("secret" in line for line in lines)
    assert lines[0] == "Diary (private)/ — 2 notes, a private folder: description not shown"
    assert lines[1] == 'Log/ — 2 notes — "Logs."'
    assert lines[-1].endswith(f'list_notes(folder="{FOLDER}Log/"), for example.')


def test_an_area_inside_a_private_folder_shows_no_description(private):
    folder, main = "Work (private)/Area/", "Work (private)/Area/Area.md"
    lines = area_map.build(folder, main, [(folder + "a.md", "secret"), (folder + "b.md", None)],
                           list_notes_describes=True)
    assert lines == ["a.md — in a private folder, description not shown",
                     "b.md — in a private folder, description not shown"]
