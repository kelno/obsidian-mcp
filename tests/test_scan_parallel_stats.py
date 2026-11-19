"""The scan's shortcut stats, taken a run of notes at a time on a small pool.

On the reference deployment (gocryptfs over FUSE, about 1 ms per stat) the
scan's one-at-a-time stats were most of `move_note`'s pre-move refresh. The
pool must change nothing but the time: the same scan result, the same stats
(only of files the scan would stat), the same failure handling, and no stat
running once the walk has closed the directory it goes through.
"""

import errno
import hashlib
import os
import threading
import time

import pytest

from src.services import indexer

V = indexer.CURRENT_EXTRACTION_VERSION
CONFLICT = "c.sync-conflict-20260101-120000-ABCDEFG.md"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _note(root, rel: str, text: str | None = None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else f"{rel}\n", encoding="utf-8")
    old = time.time() - 60
    os.utime(path, (old, old))
    return path


def _row(root, rel: str, **kw) -> indexer.SnapshotRow:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    stat = indexer._stat_tuple(os.stat(path))
    return indexer.SnapshotRow(
        kw.get("content_hash", _h(text)),
        kw.get("extraction_version", V),
        kw.get("stat", stat),
        task_dirty=kw.get("task_dirty", False),
    )


def _scan(root, snapshot, **kw):
    with indexer.pinned_root(root) as fd:
        return indexer._scan_vault(
            fd, snapshot, force_read=False, re_derive=False,
            stop=kw.pop("stop", threading.Event()), **kw,
        )


def _outcome(result: indexer.ScanResult) -> dict:
    """Everything a scan decided, discovery order included."""
    return {
        "files": list(result.files.items()),
        "seen": result.seen,
        "skips": result.skips,
        "unverified": result.unverified,
        "read_failures": result.read_failures,
        "walk_failures": result.walk_failures,
        "not_indexable": result.not_indexable,
        "reads": result.reads,
        "shortcut": result.shortcut,
        "read_reasons": result.read_reasons,
        "stat_not_recorded": result.stat_not_recorded,
        "stats": result.stats,
    }


class _StatSpy:
    """`_stat_follow`, recording what it stats and on which thread."""

    def __init__(self, fail: set[str] = frozenset()):
        self.fail = fail
        self.names: list[str] = []
        self.threads: set[str] = set()
        self._lock = threading.Lock()

    def __call__(self, parent_fd, name):
        with self._lock:
            self.names.append(name)
            self.threads.add(threading.current_thread().name)
        if name in self.fail:
            raise OSError(errno.EIO, "Input/output error")
        return os.stat(name, dir_fd=parent_fd)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A tree with every pre-stat decision in it, notes and subdirectories
    interleaved so one directory holds several runs."""
    monkeypatch.setattr(indexer, "MAX_PATH_CHARS", 50)
    for rel in (
        "a.md", "b.md", "c.md", "d/x.md", "d/y.md", "d/z.txt", "e.md",
        "f.md", "m/n/p.md", "m/n/q.md", "m/o.md", "m/r.md", "s.md",
        "newer.md", "nostat.md", "oldparser.md", "dirty.md", "changed.md",
        ".hidden/h.md", CONFLICT, "t" * 50 + ".md",
    ):
        _note(tmp_path, rel)
    snapshot = {
        rel: _row(tmp_path, rel)
        for rel in (
            "a.md", "b.md", "c.md", "d/x.md", "d/y.md", "e.md", "f.md",
            "m/n/p.md", "m/n/q.md", "m/o.md", "m/r.md", "s.md", CONFLICT,
        )
    }
    snapshot["nostat.md"] = _row(tmp_path, "nostat.md", stat=None)
    snapshot["oldparser.md"] = _row(
        tmp_path, "oldparser.md", extraction_version=V - 1
    )
    snapshot["dirty.md"] = _row(tmp_path, "dirty.md", task_dirty=True)
    changed = _row(tmp_path, "changed.md")
    snapshot["changed.md"] = indexer.SnapshotRow(
        changed.content_hash, V, (changed.stat[0] + 1, *changed.stat[1:])
    )
    return tmp_path, snapshot


#: The notes whose stat decides them in `vault` (with conflicts excluded).
STATTED = {
    "a.md", "b.md", "c.md", "x.md", "y.md", "e.md", "f.md", "p.md", "q.md",
    "o.md", "r.md", "s.md", "changed.md",
}


def _both(monkeypatch, root, snapshot, spy_fail=frozenset(), **kw):
    """The scan with the pool and without it, and each one's stat spy."""
    out = []
    for threads in (8, 1):
        monkeypatch.setattr(indexer, "SCAN_STAT_THREADS", threads)
        spy = _StatSpy(spy_fail)
        monkeypatch.setattr(indexer, "_stat_follow", spy)
        out.append((_scan(root, snapshot, **kw), spy))
    return out


def test_the_pool_changes_nothing_the_scan_decides(monkeypatch, vault):
    root, snapshot = vault

    (pooled, pooled_spy), (alone, alone_spy) = _both(
        monkeypatch, root, snapshot, exclude_conflicts=True
    )

    assert _outcome(pooled) == _outcome(alone)
    assert pooled.shortcut == 12
    assert pooled.read_reasons == {
        "new": 1, "no stored stat": 1, "parser version": 1, "task dirty": 1,
        "changed size": 1,
    }
    # Only the files the scan would stat, each once, with the pool or not.
    assert sorted(pooled_spy.names) == sorted(alone_spy.names) == sorted(STATTED)
    assert any(t.startswith("scan-stat") for t in pooled_spy.threads)
    assert not any(t.startswith("scan-stat") for t in alone_spy.threads)


def test_a_conflict_copy_is_statted_when_it_is_not_excluded(monkeypatch, vault):
    root, snapshot = vault

    (pooled, pooled_spy), (alone, alone_spy) = _both(
        monkeypatch, root, snapshot, exclude_conflicts=False
    )

    assert _outcome(pooled) == _outcome(alone)
    assert sorted(pooled_spy.names) == sorted(STATTED | {CONFLICT})


def test_a_forced_pass_takes_no_stat(monkeypatch, vault):
    root, snapshot = vault
    monkeypatch.setattr(indexer, "SCAN_STAT_THREADS", 8)
    spy = _StatSpy()
    monkeypatch.setattr(indexer, "_stat_follow", spy)

    with indexer.pinned_root(root) as fd:
        result = indexer._scan_vault(
            fd, snapshot, force_read=True, re_derive=False,
            stop=threading.Event(),
        )

    assert spy.names == []
    assert result.stats == 0


def test_a_stat_that_fails_on_the_pool_is_read_as_before(monkeypatch, vault):
    root, snapshot = vault

    (pooled, pooled_spy), (alone, _) = _both(
        monkeypatch, root, snapshot, spy_fail={"b.md"}, exclude_conflicts=True
    )

    assert _outcome(pooled) == _outcome(alone)
    assert pooled.read_reasons["stat failed"] == 1
    assert pooled.files["b.md"].read is True
    assert pooled.shortcut == 11
    assert "b.md" in pooled_spy.names


def test_a_pool_that_cannot_start_a_thread_falls_back_to_the_loop(
    monkeypatch, vault
):
    root, snapshot = vault

    class NoThreads(indexer.ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(indexer, "ThreadPoolExecutor", NoThreads)
    (pooled, pooled_spy), (alone, _) = _both(
        monkeypatch, root, snapshot, exclude_conflicts=True
    )

    assert _outcome(pooled) == _outcome(alone)
    assert sorted(pooled_spy.names) == sorted(STATTED)


def test_the_walk_hands_runs_and_yields_the_same(tmp_path):
    for rel in ("a.md", "b.md", "c/k.md", "d.md", ".x.md", "e.txt", "f.md"):
        _note(tmp_path, rel)
    runs = []

    def prestat(parent_fd, run):
        runs.append(run)
        return []

    with indexer.pinned_root(tmp_path) as fd:
        plain = [f.rel for f in indexer.discover_markdown_files_at(fd)]
        hooked = [
            f.rel for f in indexer.discover_markdown_files_at(fd, prestat=prestat)
        ]

    assert hooked == plain == ["a.md", "b.md", "c/k.md", "d.md", "f.md"]
    assert runs == [
        [("a.md", "a.md"), ("b.md", "b.md")],
        [("c/k.md", "k.md")],
        [("d.md", "d.md"), ("f.md", "f.md")],
    ]


def test_a_cancelled_scan_leaves_no_stat_running(monkeypatch, tmp_path):
    """Cancelled mid-run: queued stats are dropped, running ones finish
    before the walk closes the directory they go through, through it."""
    names = [f"n{i:02d}.md" for i in range(40)]
    for name in names:
        _note(tmp_path, f"sub/{name}")
    snapshot = {f"sub/{n}": _row(tmp_path, f"sub/{n}") for n in names}
    sub_ino = os.stat(tmp_path / "sub").st_ino
    stop = threading.Event()
    lock = threading.Lock()
    state = {"active": 0, "calls": 0, "wrong_dir": 0}

    def slow_stat(parent_fd, name):
        with lock:
            state["active"] += 1
            state["calls"] += 1
        stop.set()
        try:
            time.sleep(0.02)
            try:
                same_dir = os.fstat(parent_fd).st_ino == sub_ino
            except OSError:  # EBADF: the walk closed it under this stat
                same_dir = False
            if not same_dir:
                state["wrong_dir"] += 1
            return os.stat(name, dir_fd=parent_fd)
        finally:
            with lock:
                state["active"] -= 1

    monkeypatch.setattr(indexer, "SCAN_STAT_THREADS", 8)
    monkeypatch.setattr(indexer, "_stat_follow", slow_stat)

    with pytest.raises(indexer.ScanCancelled):
        _scan(tmp_path, snapshot, stop=stop)

    assert state["active"] == 0
    assert state["calls"] < len(names)
    assert state["wrong_dir"] == 0


def test_stats_run_at_most_the_pool_size_ahead_of_the_loop(monkeypatch, tmp_path):
    """A long run's stats are not all taken when it starts: a note late in
    a big folder would be statted long before the loop reaches it (the
    adversarial review's finding, 2026-11-19)."""
    names = [f"n{i:02d}.md" for i in range(40)]
    for name in names:
        _note(tmp_path, f"sub/{name}")
    snapshot = {f"sub/{n}": _row(tmp_path, f"sub/{n}") for n in names}
    lock = threading.Lock()
    state = {"started": 0, "taken": 0, "ahead": 0}
    real_stat, real_tuple = indexer._stat_follow, indexer._stat_tuple

    def counted_stat(parent_fd, name):
        with lock:
            state["started"] += 1
            state["ahead"] = max(state["ahead"], state["started"] - state["taken"])
        return real_stat(parent_fd, name)

    def counted_tuple(st):
        with lock:
            state["taken"] += 1
        return real_tuple(st)

    monkeypatch.setattr(indexer, "SCAN_STAT_THREADS", 3)
    monkeypatch.setattr(indexer, "_stat_follow", counted_stat)
    monkeypatch.setattr(indexer, "_stat_tuple", counted_tuple)

    result = _scan(tmp_path, snapshot)

    assert result.shortcut == len(names) and state["started"] == len(names)
    assert state["ahead"] <= 3
