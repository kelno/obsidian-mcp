"""The `area_not_opened:` nudge: a note write inside an area that was not opened with `open_area` (ledger F89).

Offline: the area index is a test double (`_area_index` patched), and the
plugin's hooks are called directly with texts that need no database.
"""
import pytest

from src import plugins
from src.auth.session import current_principal
from src.plugins import kelno_vault
from src.plugins.kelno_vault import area_opened
from src.plugins.kelno_vault import open_area as open_area_module
from src.plugins.kelno_vault.area_result import OpenAreaResult
from src.services.fork_tasks.query import AreaIndex

MAIN = "Areas/Chronic symptoms/Chronic symptoms.md"
NOTE = "Areas/Chronic symptoms/Dehumidifier.md"


@pytest.fixture(autouse=True)
def areas(monkeypatch):
    area_opened.clear()
    rows = [(MAIN, {"area_tag": "area/chronic-symptoms"}), ("Areas/Dog/Dog.md", {"area_tag": "area/dog"})]

    async def index(uid):
        return AreaIndex.from_rows(rows)

    monkeypatch.setattr(area_opened, "_area_index", index)
    token = current_principal.set(("oauth", "grant-1"))
    yield
    current_principal.reset(token)
    area_opened.clear()


async def report_for(path, old="a\n", new="a\nb\n"):
    rewrite = await kelno_vault._rewrite(plugins.NoteWrite("edit_note", path, old, new, False, None))
    return rewrite.report(0, False)


async def test_a_write_in_an_area_not_opened_gets_the_nudge():
    text = await report_for(NOTE)
    assert "area_not_opened:" in text
    assert "Chronic symptoms (#area/chronic-symptoms)" in text
    assert 'open_area(area="chronic-symptoms")' in text
    assert "tell the owner" in text


async def test_a_dry_run_is_not_told_the_write_went_ahead():
    rewrite = await kelno_vault._rewrite(plugins.NoteWrite("edit_note", NOTE, "a\n", "a\nb\n", True, None))
    text = rewrite.report(0, True)
    assert "area_not_opened:" in text and "dry run" in text and "went ahead" not in text


async def test_no_nudge_once_the_area_was_opened():
    area_opened.record("area/chronic-symptoms", None)
    assert "area_not_opened" not in await report_for(NOTE)
    assert "area_not_opened" not in await report_for(MAIN)


async def test_opening_another_area_or_with_another_credential_does_not_count():
    area_opened.record("area/dog", None)
    assert "area_not_opened" in await report_for(NOTE)
    other = current_principal.set(("oauth", "grant-2"))
    try:
        area_opened.record("area/chronic-symptoms", None)
    finally:
        current_principal.reset(other)
    assert "area_not_opened" in await report_for(NOTE)


async def test_an_old_open_area_call_no_longer_counts(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(area_opened, "_now", lambda: now[0])
    area_opened.record("area/chronic-symptoms", None)
    now[0] += area_opened.WINDOW_SECONDS + 1
    assert "area_not_opened" in await report_for(NOTE)


async def test_no_nudge_outside_an_area_or_when_the_index_fails(monkeypatch):
    assert "area_not_opened" not in await report_for("Journal/2026-11-14.md")

    async def broken(uid):
        raise RuntimeError("database down")

    monkeypatch.setattr(area_opened, "_area_index", broken)
    assert "area_not_opened" not in await report_for(NOTE)


async def test_a_new_note_in_an_area_gets_the_nudge():
    rewrite = await kelno_vault._rewrite(plugins.NoteWrite("create_note", NOTE, None, "a\n", False, None))
    assert "area_not_opened:" in rewrite.report(0, False)


async def test_a_move_into_an_area_gets_the_nudge_and_a_move_elsewhere_does_not():
    check = await kelno_vault._check_name(plugins.NoteName("move_note", NOTE, "Inbox/Dehumidifier.md", None))
    assert check is not None and "area_not_opened:" in check.note
    assert await kelno_vault._check_name(plugins.NoteName("move_note", "Inbox/X.md", NOTE, None)) is None
    # A new note gets it from the write's report, not twice.
    created = await kelno_vault._check_name(plugins.NoteName("create_note", NOTE, None, None))
    assert created is None or "area_not_opened" not in created.note


async def test_open_area_records_the_area_it_opened(monkeypatch):
    async def impl(area=None, folder=None):
        return OpenAreaResult(found=True, area="area/chronic-symptoms")

    monkeypatch.setattr(open_area_module, "open_area_impl", impl)
    await open_area_module.open_area(area="chronic-symptoms")
    assert "area_not_opened" not in await report_for(NOTE)


async def test_open_area_that_finds_nothing_records_nothing(monkeypatch):
    async def impl(area=None, folder=None):
        return OpenAreaResult(found=False)

    monkeypatch.setattr(open_area_module, "open_area_impl", impl)
    await open_area_module.open_area(folder="D:\\nowhere")
    assert area_opened._opened == {}


def test_the_map_stays_bounded(monkeypatch):
    monkeypatch.setattr(area_opened, "MAX_ENTRIES", 3)
    for i in range(10):
        area_opened.record(f"area/a{i}", None)
    assert len(area_opened._opened) <= 3
    assert area_opened.opened("area/a9", None)


async def test_after_a_restart_the_nudge_says_earlier_calls_were_forgotten(monkeypatch):
    """Ledger F112: the memory of opened areas does not survive a restart, so the nudge says so for an hour."""
    now = [100_000.0]
    monkeypatch.setattr(area_opened, "_now", lambda: now[0])
    monkeypatch.setattr(area_opened, "_STARTED", now[0] - 17 * 60)
    text = await report_for(NOTE)
    assert "area_not_opened:" in text and "restarted 17 minutes ago" in text
    assert text.index("restarted") < text.index("Call open_area")
    monkeypatch.setattr(area_opened, "_STARTED", now[0] - 20)
    assert "restarted less than a minute ago" in await report_for(NOTE)
    monkeypatch.setattr(area_opened, "_STARTED", now[0] - area_opened.WINDOW_SECONDS - 1)
    assert "restarted" not in await report_for(NOTE)
