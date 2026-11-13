"""Syncthing conflict copies (fork): upstream's opt-in covers this vault's.

The fork used to skip `*.sync-conflict-*` in the walk itself; since the
2026-11 resync it relies on upstream's `INDEX_EXCLUDE_CONFLICT_FILES` (#392),
turned on in the deployment. These names are real ones from the vault (a
conflict copy's checkboxes showed up as tasks in the live check, 2026-11-01).
"""

from __future__ import annotations

from src.services import indexer
from src.services.conflict_copies import is_conflict_copy


def test_upstreams_patterns_match_this_vaults_conflict_copies():
    assert is_conflict_copy("Note.sync-conflict-20261028-181425-VRO24FG.md")
    assert is_conflict_copy("_commands.sync-conflict-20260810-161522-LQB3B4E.md")
    assert not is_conflict_copy("About sync-conflict files.md")
    assert not is_conflict_copy("Note.sync-conflict-draft.md")
    assert not is_conflict_copy("Note.md")


def test_the_walk_leaves_the_decision_to_the_setting(tmp_path):
    """The walk lists every note; the scan applies the setting (#392)."""
    vault = tmp_path / "vault"
    files = {
        "Note.md": "n\n",
        "Note.sync-conflict-20261028-181425-VRO24FG.md": "old n\n",
    }
    for rel, text in files.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    assert sorted(indexer.discover_markdown_files(vault)) == sorted(files)
