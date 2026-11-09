"""The scan counts why it read each file (2026-11-09).

A server re-read all 6,071 notes on every pass and nothing in the log said
which rule forced it (it was `INDEX_STAT_SHORTCUT=false`). The pass's "Found"
line now carries these counts.
"""

import hashlib
import os
import threading
import time

from src.services import indexer

V = indexer.CURRENT_EXTRACTION_VERSION


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _settled(path) -> tuple:
    old = time.time() - 60
    os.utime(path, (old, old))
    return indexer._stat_tuple(os.stat(path))


def _scan(root, snapshot, *, force_read=False):
    with indexer.pinned_root(root) as fd:
        return indexer._scan_vault(
            fd, snapshot, force_read=force_read, re_derive=False,
            stop=threading.Event(),
        )


def test_each_read_is_counted_under_the_rule_that_forced_it(tmp_path):
    for name in ("same", "new", "nostat", "oldparser", "moved"):
        (tmp_path / f"{name}.md").write_text(f"{name}\n", encoding="utf-8")
    same = _settled(tmp_path / "same.md")
    moved = _settled(tmp_path / "moved.md")
    snapshot = {
        "same.md": indexer.SnapshotRow(_h("same\n"), V, same),
        "nostat.md": indexer.SnapshotRow(_h("nostat\n"), V, None),
        "oldparser.md": indexer.SnapshotRow(
            _h("oldparser\n"), V - 1, _settled(tmp_path / "oldparser.md")
        ),
        # A recorded stat whose inode differs: what an unstable mount shows.
        "moved.md": indexer.SnapshotRow(
            _h("moved\n"), V, (moved[0], moved[1], moved[2], moved[3] + 1)
        ),
    }

    result = _scan(tmp_path, snapshot)

    assert result.shortcut == 1
    assert result.read_reasons == {
        "new": 1,
        "no stored stat": 1,
        "parser version": 1,
        "changed inode": 1,
    }


def test_a_forced_pass_counts_every_read_as_forced(tmp_path):
    (tmp_path / "a.md").write_text("a\n", encoding="utf-8")
    snapshot = {"a.md": indexer.SnapshotRow(_h("a\n"), V, _settled(tmp_path / "a.md"))}

    result = _scan(tmp_path, snapshot, force_read=True)

    assert result.read_reasons == {"forced": 1}


def test_a_stat_too_recent_to_record_is_counted(tmp_path):
    (tmp_path / "fresh.md").write_text("fresh\n", encoding="utf-8")

    result = _scan(tmp_path, {})

    assert result.stat_not_recorded == 1
