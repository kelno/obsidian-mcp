"""Give every task in a vault an id (🆔), once: the conversion to tasks named by id.

    python -m scripts.fork_tasks_assign_ids /path/to/vault            # dry run: lists every change
    python -m scripts.fork_tasks_assign_ids /path/to/vault --apply    # writes them
    python -m scripts.fork_tasks_assign_ids /path/to/vault --block-ids [--apply]

`--block-ids` instead gives every task that has an id and no block link its
id as its block id (` ^<id>` at the very end of the line, before trailing
spaces), as the task tools write it with `TASKS_BLOCK_ID`, so a link can
open the task on its own line. Same checks, same listing, same reruns.

Runs on a vault folder directly, with no database and no server, through the
task tools' own code: the owner's Tasks settings from the vault, the same
scan of which lines are tasks and the same read-back checks as `edit_task`.
Each task without a 🆔 gets six random lowercase letters and digits, unique
in the vault, added at the end of its line (before a block link `^abc` and
trailing spaces): no other byte of the line changes, and the line must read
back as the same task with that id, or it is skipped and listed. The plugin
reads fields in any order, and puts them in its own the next time it edits
the line. Lines the task tools refuse to rewrite (a date that is not a real
date, an unknown `🏁` value, a field given twice) are skipped and listed too.

Skipped, and listed: hidden folders (`.obsidian`, `.trash`), symlinked
notes, sync-conflict copies, notes that are not UTF-8, and notes with an
indented code fence nothing closes (the task tools refuse those too). A note
that changes on disk between its read and its write is left as it is and
listed: run again. A run stopped partway can simply be run again: tasks that
have an id are left alone.

Linux only (the settings reader opens files beneath the vault root without
following symlinks); on the owner's PC, run it in the test container with the
vault mounted. Not a server feature: see "Task ids" in docs/architecture/tasks.md.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from src.services.conflict_copies import is_conflict_copy
from src.services.fork_tasks import format as tf
from src.services.fork_tasks.ids import new_id
from src.services.fork_tasks.ops import DAMAGING_WARNINGS, TaskOpError, _comparable, parse_line
from src.services.fork_tasks.scan import scan_note, split_lines
from src.services.fork_tasks.settings import read_settings
from src.services.open_note_url import BLOCK_ID_RE

DEFAULT_SETTINGS_PATH = ".obsidian/plugins/obsidian-tasks-plugin/data.json"


@dataclass
class NotePlan:
    path: str
    original: bytes
    new_text: str
    #: (line number, old line, new line)
    changes: list[tuple[int, str, str]] = field(default_factory=list)
    #: (line number, line, why it was left without an id)
    left: list[tuple[int, str, str]] = field(default_factory=list)


def with_id(line: str, task_id: str) -> str:
    """`line` with ` 🆔 <task_id>` added after its last field, before a block
    link and trailing whitespace; every other byte kept."""
    body = line.rstrip()
    end = len(body)
    if (bl := tf.BLOCK_LINK_RE.search(body)) is not None:
        end = bl.start()
    return line[:end] + f" 🆔 {task_id}" + line[end:]


def with_block_id(line: str, task_id: str) -> str:
    """`line` with ` ^<task_id>` at its end, before trailing whitespace; every other byte kept."""
    end = len(line.rstrip())
    return line[:end] + f" ^{task_id}" + line[end:]


def _notes(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(".md") and not name.startswith("."):
                full = Path(dirpath) / name
                yield full, full.relative_to(root).as_posix()


def _rows(text: str) -> list[tuple[int, str, str | None, str]]:
    scan = scan_note(text)
    return [(s.line_number, s.kind, s.reason, s.text) for s in scan.lines]


def plan_note(rel: str, data: bytes, settings, registry, taken: set[str], *,
              block_ids: bool = False) -> NotePlan | str | None:
    """The changes for one note, a reason it is skipped, or None when it has nothing to change."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "not UTF-8"
    scan = scan_note(text)
    if scan.unmatched_fence_lines:
        return "an indented code fence nothing closes (line " + ", ".join(map(str, scan.unmatched_fence_lines)) + ")"
    lines = split_lines(text)
    plan = NotePlan(rel, data, text)
    written: dict[int, tf.Task] = {}
    for scanned in scan.lines:
        if scanned.kind != "task":
            continue
        task = parse_line(scanned.text, rel, settings, registry)
        if task is None:
            continue
        if block_ids:
            if not task.id or task.block_link or not BLOCK_ID_RE.fullmatch(task.id):
                continue
        elif task.id:
            continue
        damaging = [w for w in task.warnings if w in DAMAGING_WARNINGS]
        if damaging:
            plan.left.append((scanned.line_number, scanned.text, DAMAGING_WARNINGS[damaging[0]]))
            continue
        if block_ids:
            task_id = task.id
            new_line = with_block_id(scanned.text, task_id)
            intended = replace(task, block_link=f" ^{task_id}")
        else:
            task_id = new_id(taken)
            new_line = with_id(scanned.text, task_id)
            intended = replace(task, id=task_id)
        back = parse_line(new_line, rel, settings, registry)
        if back is None or _comparable(back) != _comparable(intended) or back.warnings != task.warnings:
            plan.left.append((scanned.line_number, scanned.text,
                              f"with {'a block id' if block_ids else 'an id'} added at its end, the Tasks plugin "
                              "would read it differently"))
            continue
        taken.add(task_id)
        index = scanned.line_number - 1
        lines[index] = (new_line, lines[index][1])
        written[scanned.line_number] = intended
        plan.changes.append((scanned.line_number, scanned.text, new_line))
    if not plan.changes:
        return plan if plan.left else None
    plan.new_text = "".join(content + term for content, term in lines)
    # Every other line must be read exactly as before, and each written line
    # still be a task there (a scan of the whole note, as edit_task does).
    after_rows = {n: k for n, k, _r, _t in _rows(plan.new_text)}
    if any(after_rows.get(n) != "task" for n in written):
        raise TaskOpError("content_unsafe", f"a line given an id in {rel} would no longer be read as a task")
    before = [(n, k, r, None if n in written else t) for n, k, r, t in _rows(text)]
    after = [(n, k, r, None if n in written else t) for n, k, r, t in _rows(plan.new_text)]
    if before != after or scan_note(plan.new_text).unmatched_fence_lines:
        raise TaskOpError("content_unsafe", f"the ids would change how other lines of {rel} are read")
    return plan


def _write_if_unchanged(full: Path, plan: NotePlan) -> bool:
    """Replace the note atomically, only if it still holds the bytes planned from."""
    if full.read_bytes() != plan.original:
        return False
    fd, tmp = tempfile.mkstemp(dir=full.parent, prefix=".task-ids-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(plan.new_text.encode("utf-8"))
        try:
            os.chmod(tmp, os.stat(full).st_mode & 0o7777)
        except OSError:
            pass  # a mount that keeps no modes (a Windows drive): the default is what it has
        if full.read_bytes() != plan.original:
            os.unlink(tmp)
            return False
        os.replace(tmp, full)
        return True
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("vault", type=Path)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--block-ids", action="store_true",
                        help="give tasks that have an id that id as their block id, instead of giving ids")
    parser.add_argument("--settings-path", default=DEFAULT_SETTINGS_PATH)
    args = parser.parse_args(argv)
    root = args.vault.resolve()
    settings = read_settings(root, args.settings_path)
    registry = tf.StatusRegistry.from_settings(settings)
    out = sys.stdout

    notes = []
    skipped: list[tuple[str, str]] = []
    taken: set[str] = set()
    for full, rel in _notes(root):
        if full.is_symlink():
            skipped.append((rel, "a symlink"))
            continue
        if is_conflict_copy(full.name):
            skipped.append((rel, "a sync-conflict copy"))
            continue
        data = full.read_bytes()
        notes.append((full, rel, data))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for scanned in scan_note(text).lines:
            if scanned.kind == "task" and "🆔" in scanned.text:
                task = parse_line(scanned.text, rel, settings, registry)
                if task is not None and task.id:
                    taken.add(task.id)

    plans: list[tuple[Path, NotePlan]] = []
    for full, rel, data in notes:
        try:
            result = plan_note(rel, data, settings, registry, taken, block_ids=args.block_ids)
        except TaskOpError as exc:
            skipped.append((rel, str(exc)))
            continue
        if isinstance(result, str):
            skipped.append((rel, result))
        elif result is not None:
            plans.append((full, result))

    total = sum(len(p.changes) for _, p in plans)
    left = [(p.path, n, line, why) for _, p in plans for n, line, why in p.left]
    for _, plan in plans:
        if plan.changes:
            out.write(f"\n## {plan.path}\n")
        for number, old, new in plan.changes:
            out.write(f"{number}:\n  - {old}\n  + {new}\n")
    if left:
        out.write(f"\n## Tasks left without {'a block id' if args.block_ids else 'an id'} (fix the line, then run "
                  "again)\n")
        for rel, number, line, why in left:
            out.write(f"{rel}:{number}: {why}\n  {line}\n")
    if skipped:
        out.write("\n## Notes skipped\n")
        for rel, why in skipped:
            out.write(f"{rel}: {why}\n")

    if not args.apply:
        what = "a block id" if args.block_ids else "an id"
        out.write(f"\nDry run: {total} task(s) in {sum(1 for _, p in plans if p.changes)} note(s) would get {what}; "
                  f"{len(left)} task(s) left without one; {len(skipped)} note(s) skipped. Run again with --apply "
                  "to write.\n")
        return 0
    plans = [(full, plan) for full, plan in plans if plan.changes]
    changed, lost = 0, []
    for full, plan in plans:
        if _write_if_unchanged(full, plan):
            changed += 1
        else:
            lost.append(plan.path)
    out.write(f"\nWritten: {changed} note(s), {sum(len(p.changes) for f, p in plans if p.path not in lost)} task(s).\n")
    for rel in lost:
        out.write(f"Changed on disk meanwhile, left as it is (run again): {rel}\n")
    return 1 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
