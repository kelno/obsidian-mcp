"""The one-off conversion script giving every task an id (fork)."""

from __future__ import annotations

import re

from scripts import fork_tasks_assign_ids as script
from tests.test_fork_task_tools import write_settings

ID = r"🆔 [a-z0-9]{6}"


def _vault(tmp_path):
    write_settings(tmp_path)
    (tmp_path / "a.md").write_text(
        "# A\n- [ ] one\n- [x] two ✅ 2026-10-01\n- [ ] has one 🆔 kept\n- [ ] out of order 📅 2026-11-05 ⏫\n"
        "text\n```\n- [ ] fenced\n```\n- [ ] pay rent 📅 2026-11-01 📅 2026-12-01\n- [ ] linked ^blk1\n"
        "- [ ] hard break  \n", encoding="utf-8")
    (tmp_path / "b.md").write_bytes(b"- [ ] crlf\r\n- [ ] last")
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "old.md").write_text("- [ ] trashed\n", encoding="utf-8")
    (tmp_path / "c.sync-conflict-20261101-120000-ABCDEFG.md").write_text("- [ ] copy\n", encoding="utf-8")
    (tmp_path / "d.md").write_text("  ```\n- [ ] after an unclosed indented fence\n", encoding="utf-8")
    return tmp_path


def test_dry_run_writes_nothing_and_lists_every_change(tmp_path, capsys):
    root = _vault(tmp_path)
    before = (root / "a.md").read_bytes()
    assert script.main([str(root)]) == 0
    out = capsys.readouterr().out
    assert (root / "a.md").read_bytes() == before
    assert "Dry run: 7 task(s) in 2 note(s) would get an id; 1 task(s) left without one; 2 note(s) skipped" in out
    assert "a.md:10: it gives the same field twice" in out
    assert "c.sync-conflict" in out and "d.md" in out


def test_apply_gives_each_task_a_unique_id_and_changes_nothing_else(tmp_path, capsys):
    root = _vault(tmp_path)
    assert script.main([str(root), "--apply"]) == 0
    a = (root / "a.md").read_text(encoding="utf-8").split("\n")
    assert a[0] == "# A"
    assert re.fullmatch(rf"- \[ \] one {ID}", a[1])
    assert re.fullmatch(rf"- \[x\] two ✅ 2026-10-01 {ID}", a[2])
    assert a[3] == "- [ ] has one 🆔 kept"
    assert re.fullmatch(rf"- \[ \] out of order 📅 2026-11-05 ⏫ {ID}", a[4])
    assert a[5:10] == ["text", "```", "- [ ] fenced", "```", "- [ ] pay rent 📅 2026-11-01 📅 2026-12-01"]
    assert re.fullmatch(rf"- \[ \] linked {ID} \^blk1", a[10])
    assert re.fullmatch(rf"- \[ \] hard break {ID}  ", a[11])
    assert a[12:] == [""]
    b = (root / "b.md").read_bytes().decode("utf-8")
    assert re.fullmatch(rf"- \[ \] crlf {ID}\r\n- \[ \] last {ID}", b)
    ids = re.findall(r"🆔 (\w+)", (root / "a.md").read_text(encoding="utf-8") + b)
    assert len(ids) == len(set(ids)) == 8
    assert (root / ".trash" / "old.md").read_text(encoding="utf-8") == "- [ ] trashed\n"
    assert script.main([str(root)]) == 0
    assert "Dry run: 0 task(s)" in capsys.readouterr().out


def test_a_note_changed_meanwhile_is_left_alone(tmp_path, monkeypatch, capsys):
    root = _vault(tmp_path)
    real = script._write_if_unchanged

    def racing(full, plan):
        if full.name == "a.md":
            full.write_text("- [ ] edited meanwhile\n", encoding="utf-8")
        return real(full, plan)

    monkeypatch.setattr(script, "_write_if_unchanged", racing)
    assert script.main([str(root), "--apply"]) == 1
    assert (root / "a.md").read_text(encoding="utf-8") == "- [ ] edited meanwhile\n"
    assert "left as it is" in capsys.readouterr().out
