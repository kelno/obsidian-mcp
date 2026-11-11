"""Reading the vault's list of areas for `open_area`: the table, folders, sections."""
import pytest

from src.services import areas_index as ai

TABLE = """Intro text with a | pipe.

| Area | Related folders | Vault folder |
| --- | --- | --- |
| Voice bridge | `D:\\work\\voice-bridge` (private repo); its submodule `D:\\work\\voice-bridge\\claude-voice` (fork) | [[Areas/Voice bridge/Voice bridge\\|Voice bridge]] |
| Dog | (none) | [[Areas/Dog/Dog\\|Dog]] |
| 3D printing | `docs/3D models` on the DATA drive | [[Areas/3D Printing/3D Printing\\|3D Printing]] |
| LLM Village | `D:\\work\\llm-village` (worktrees `llm-village-2`) | no folder yet; a game |
| Old thing | `/srv/old` | [[Archive/Areas/Old thing/Old thing\\|Old thing]] |

After the table.
"""


def test_the_table_is_read_by_its_column_names():
    rows = ai.parse(TABLE)
    assert [r.name for r in rows] == ["Voice bridge", "Dog", "3D printing", "LLM Village", "Old thing"]
    assert rows[0].folders == ["D:\\work\\voice-bridge", "D:\\work\\voice-bridge\\claude-voice"]
    assert rows[0].main_note == "Areas/Voice bridge/Voice bridge.md"
    assert rows[1].folders == [] and rows[2].folders == []      # a relative path is a description, not a folder
    assert rows[3].main_note is None and rows[3].folders == ["D:\\work\\llm-village"]
    assert rows[3].vault_cell == "no folder yet; a game"


def test_a_table_without_the_columns_is_refused():
    with pytest.raises(ai.AreasIndexError):
        ai.parse("| Area | Folder |\n| --- | --- |\n| x | y |\n")


@pytest.mark.parametrize("folder,expected", [
    ("D:\\work\\voice-bridge", "D:\\work\\voice-bridge"),
    ("d:/WORK/Voice-Bridge/", "D:\\work\\voice-bridge"),                     # case and slashes
    ("D:\\work\\voice-bridge\\src\\x", "D:\\work\\voice-bridge"),            # a subfolder
    ("D:\\work\\voice-bridge\\claude-voice", "D:\\work\\voice-bridge\\claude-voice"),  # the deepest listed wins
    ("/srv/old/a", "/srv/old"),
])
def test_folders_match_case_insensitively_with_subfolders(folder, expected):
    hits = ai.rows_for_folder(ai.parse(TABLE), folder)
    assert hits and hits[0][1] == expected


@pytest.mark.parametrize("folder", ["D:\\work\\voice-bridge-2", "D:\\work", "E:\\work\\voice-bridge"])
def test_a_sibling_or_parent_folder_does_not_match(folder):
    assert ai.rows_for_folder(ai.parse(TABLE), folder) == []


def test_the_row_of_a_main_note():
    rows = ai.parse(TABLE)
    assert ai.row_for_main_note(rows, "areas/voice bridge/voice bridge.md").name == "Voice bridge"
    assert ai.row_for_main_note(rows, "Areas/Nope/Nope.md") is None


def test_the_tasks_section_is_removed_and_the_rest_kept():
    note = ("Agent rules at the top.\n\n## Current position\n\n- state\n\n## Tasks\n\n- [ ] #task one\n"
            "```\n## Not a heading\n```\n### Sub\n- [ ] #task two\n\n## Notes\n\n- kept\n")
    text, removed = ai.strip_section(note, "Tasks")
    assert removed
    assert "Agent rules at the top." in text and "## Current position" in text and "## Notes\n\n- kept" in text
    assert "#task" not in text and "Not a heading" not in text and "### Sub" not in text


def test_no_tasks_section_leaves_the_note_whole():
    note = "## Current position\n\n```\n## Tasks\n```\n"
    assert ai.strip_section(note, "Tasks") == (note, False)


def test_frontmatter_is_dropped():
    assert ai.strip_frontmatter("---\na: 1\n---\n\nBody\n") == "Body\n"
    assert ai.strip_frontmatter("Body ---\n") == "Body ---\n"
