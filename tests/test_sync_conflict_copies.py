"""Syncthing conflict copies are not indexed (fork).

A conflict copy is a stale duplicate of a note; indexed, its checkboxes showed
up as real tasks in `list_tasks` (seen in the live check, 2026-11-01). The walk
skips it like a dot-file; the file stays on disk, readable by path.
"""

from __future__ import annotations

from src.services import indexer


def test_the_walk_skips_conflict_copies_and_keeps_the_original(tmp_path):
    vault = tmp_path / "vault"
    files = {
        "Note.md": "n\n",
        "Note.sync-conflict-20261028-181425-VRO24FG.md": "old n\n",
        "sub/_commands.sync-conflict-20260810-161522-LQB3B4E.md": "c\n",
        "sub/Keep.md": "k\n",
        # Not Syncthing's pattern: kept.
        "About sync-conflict files.md": "a\n",
        "Note.sync-conflict-draft.md": "d\n",
    }
    for rel, text in files.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    found = indexer.discover_markdown_files(vault)

    assert sorted(found) == [
        "About sync-conflict files.md",
        "Note.md",
        "Note.sync-conflict-draft.md",
        "sub/Keep.md",
    ]
    assert (vault / "Note.sync-conflict-20261028-181425-VRO24FG.md").exists()


def test_is_sync_conflict_copy():
    assert indexer.is_sync_conflict_copy("A.sync-conflict-20261028-181425-VRO24FG.md")
    assert indexer.is_sync_conflict_copy("tasks.sync-conflict-20260717-213650-LQB3B4E.json")
    assert not indexer.is_sync_conflict_copy("A.md")
    assert not indexer.is_sync_conflict_copy("A.sync-conflict-notes.md")
