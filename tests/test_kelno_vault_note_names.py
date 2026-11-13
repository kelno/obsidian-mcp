"""Dated note names: where the vault declares them (`note_names`), and what a right name is."""
import pytest

from src.plugins.kelno_vault import note_names as nn

DECLARED = {
    "Log/Log.md": "dated",
    "Journal/Journal.md": "dated",
    "Reference/Reference.md": "undated",
    "Areas/Odd/Log/Log.md": "undated",       # a nearer folder note beats the mirror
    "Notes/Notes.md": "sometimes",           # not a value: ignored
    "Areas/Areas.md": "dated",               # Areas/Areas.md is Areas' folder note
}


@pytest.mark.parametrize("folder,value,source,mirrored", [
    ("Log", "dated", "Log/Log.md", None),
    ("Log/2026", "dated", "Log/Log.md", None),                          # a folder below takes it
    ("Areas/Champignons/Log", "dated", "Log/Log.md", "Log"),             # an area's Log mirrors Log
    ("Archive/Log", "dated", "Log/Log.md", "Log"),
    ("Archive/Areas/X/Log/sub", "dated", "Log/Log.md", "Log"),
    ("Archive/Reference", "undated", "Reference/Reference.md", "Reference"),
    ("Areas/Odd/Log", "undated", "Areas/Odd/Log/Log.md", None),
    ("Areas/Odd/Log/2026", "undated", "Areas/Odd/Log/Log.md", None),     # nearer note wins below it too
    ("Archive/Areas/Odd/Log", "undated", "Areas/Odd/Log/Log.md", "Areas/Odd/Log"),
    ("Areas/Champignons", "dated", "Areas/Areas.md", None),             # no mirror for the area folder itself
])
def test_resolution(folder, value, source, mirrored):
    found = nn.Rules(DECLARED.get).resolve(folder)
    assert (found.value, found.source, found.mirrored) == (value, source, mirrored)


@pytest.mark.parametrize("folder", ["", "Notes", "Inbox", "Documents/x"])
def test_no_setting_no_check(folder):
    assert nn.Rules(DECLARED.get).resolve(folder) is None


@pytest.mark.parametrize("stem", ["2026-11-12 Call", "2025-10-XX Trip", "2024-XX-XX Old", "2026-11-12",
                                  "2024-02-29 Leap", "2026-11-12 Review of 2025-03 plan", "XXXX-XX-XX Legacy",
                                  "2026-11-12 1430-1530 Call", "2026-11-12 2025-2026 season review",
                                  "2026-11-12 0612 34 56 78 Plombier", "2026-11-12 15000000 views"])
def test_right_names(stem):
    assert nn.check_dated(stem) is None


@pytest.mark.parametrize("stem,corrected", [
    ("2025-10-00 Trip", "2025-10-XX Trip"),
    ("2024-00-00 Old", "2024-XX-XX Old"),
    ("2025-03-28 2025-03-28 Title", "2025-03-28 Title"),
    ("2025-03-28 2025-03-29 Title", None),                    # maybe a span: no guess
    ("2025-10-00 2025-10-00 Trip", None),                     # the fix would still carry two dates
    ("Call 2026-11-12 2026-11-13", None),
    ("Call with bank 2026-11-12", "2026-11-12 Call with bank"),
    ("2026_11_12 Call", "2026-11-12 Call"),
    ("20261112 Call", "2026-11-12 Call"),
    ("2026-11-12-Call", "2026-11-12 Call"),
    ("2025-10-xx Trip", "2025-10-XX Trip"),
    ("Call", None),
    ("2026-13-01 Bad month", None),
    ("2026-02-30 Bad day", None),
    ("2026-XX-12 Known day", None),
])
def test_wrong_names(stem, corrected):
    problem = nn.check_dated(stem)
    assert problem is not None and problem.corrected == corrected


def test_exempt_and_undated_check():
    assert nn.exempt("Log/Log.md") and nn.exempt("Log/Log.MD") and nn.exempt("Log/Untitled 3.md")
    assert nn.archived_unchanged("Areas/X/Log/Setup.md", "Archive/Areas/X/Log/Setup.md")
    assert not nn.archived_unchanged("Areas/X/Log/Setup.md", "Archive/Areas/X/Log/Other.md")
    assert not nn.exempt("Log/Call.md")
    assert nn.starts_with_date("2026-11-12 Server") and not nn.starts_with_date("Server 2026")
    assert not nn.starts_with_date("2024-2025 Budget") and not nn.starts_with_date("1200-1500 Tarifs")
