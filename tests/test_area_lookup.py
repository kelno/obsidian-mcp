"""A new area's main note is read from disk while the index lags (ledger F50)."""
import os
import time

import pytest

from src.services import vault
from src.services.fork_tasks import area_lookup, query


def _note(root, rel, text, age_seconds=0):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    then = time.time() - age_seconds
    os.utime(path, (then, then))
    return path


def test_recent_rows_reads_notes_changed_in_the_window(tmp_path):
    _note(tmp_path, "Areas/New/New.md", "---\narea_tag: area/new\n---\n")
    _note(tmp_path, "Areas/Plain/Plain.md", "---\ndescription: no tag\n---\n")
    _note(tmp_path, "Areas/New/New.sync-conflict-20261120-101010-ABCDEFG.md", "---\narea_tag: area/new\n---\n")
    _note(tmp_path, ".trash/Gone.md", "---\narea_tag: area/gone\n---\n")
    # Synced: the writing device's mtime, two hours old, but it arrived here just now (its ctime).
    _note(tmp_path, "Areas/Synced/Synced.md", "---\narea_tag: area/synced\n---\n", age_seconds=2 * 3600)
    rows = area_lookup.recent_rows(tmp_path)
    assert sorted((rel, fm["area_tag"]) for rel, fm in rows) == [
        ("Areas/New/New.md", "area/new"), ("Areas/Synced/Synced.md", "area/synced")]
    # Past the window, nothing is read.
    assert area_lookup.recent_rows(tmp_path, now=time.time() + area_lookup.RECENT_SECONDS + 60) == []


async def test_lookup_reads_the_disk_only_on_a_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "_vault_root", lambda uid=None: tmp_path)
    walks = []
    real = area_lookup.recent_rows
    monkeypatch.setattr(area_lookup, "recent_rows", lambda root, now=None: walks.append(root) or real(root, now))
    index = query.AreaIndex.from_rows([("Areas/Known/Known.md", {"area_tag": "area/known"})])
    _note(tmp_path, "Areas/Fresh/Fresh.md", "---\narea_tag: area/fresh\ndescription: Just made.\n---\n")

    assert await area_lookup.lookup(index, "known", None) == "area/known"
    assert walks == []                                   # a known area costs no walk
    tag, fresh = await area_lookup.resolve(index, "Fresh", None)
    assert tag == "area/fresh" and ("area/fresh", "Areas/Fresh/Fresh.md", "Just made.") in fresh.described()
    with pytest.raises(query.UnknownArea) as refused:
        await area_lookup.lookup(index, "nowhere", None, workaround="pass areas=[] meanwhile")
    assert "unknown area 'nowhere'" in str(refused.value)
    assert "were read from disk too" in str(refused.value) and "pass areas=[] meanwhile" in str(refused.value)
