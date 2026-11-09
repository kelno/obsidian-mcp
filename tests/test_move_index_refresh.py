"""The pre-move index refresh: its lock wait and its refusals, offline.

The real pass against PostgreSQL is in
`tests/integration/test_move_index_refresh_pg.py`; here the pass is a stand-in
so each refusal can be produced exactly.
"""

import asyncio

import pytest

from src.mcp_server import tools
from src.services import indexer

# Opt out of the conftest stand-in: this module tests the real seam.
_real_pre_move_refresh = True


@pytest.fixture
def index_lock(monkeypatch):
    lock = asyncio.Lock()
    monkeypatch.setattr(indexer, "index_pass_lock", lock)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(indexer, "record_index_outcome", lambda *a, **k: None)
    return lock


def _pass_returning(result):
    async def fake_index_vault(user_id=None, **kwargs):
        assert kwargs.get("allow_backstop") is False
        return result

    return fake_index_vault


async def test_acquire_within_gives_up_and_leaves_the_lock_usable():
    lock = asyncio.Lock()
    await lock.acquire()
    assert await indexer._acquire_within(lock, 0.05) is False
    lock.release()
    assert not lock.locked()
    assert await indexer._acquire_within(lock, 0.05) is True
    lock.release()


async def test_acquire_within_cancelled_caller_does_not_leak_the_lock():
    lock = asyncio.Lock()
    await lock.acquire()
    waiter = asyncio.ensure_future(indexer._acquire_within(lock, 10))
    await asyncio.sleep(0.01)
    # Release and cancel in the same step: the inner acquire may win the lock
    # just as the caller is cancelled; it must be given back.
    lock.release()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)
    assert not lock.locked()


async def test_a_clean_pass_is_returned(index_lock, monkeypatch):
    result = indexer.IndexPassResult(3, 1)
    monkeypatch.setattr(indexer, "index_vault", _pass_returning(result))
    assert await indexer.refresh_index_for_move(None, wait_seconds=1) is result
    assert not index_lock.locked()


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("read_failed", "a.md (could not be read)"),
        ("walk_failed", "dir/ (folder could not be listed)"),
        ("quarantined", "a.md (quarantined by the indexer)"),
    ],
)
async def test_a_pass_that_could_not_see_every_note_refuses(
    index_lock, monkeypatch, field, expected
):
    value = ("dir",) if field == "walk_failed" else ("a.md",)
    result = indexer.IndexPassResult(3, 1, **{field: value})
    monkeypatch.setattr(indexer, "index_vault", _pass_returning(result))
    with pytest.raises(indexer.MoveRefreshRefused, match=expected.replace("(", r"\(").replace(")", r"\)")):
        await indexer.refresh_index_for_move(None, wait_seconds=1)
    assert not index_lock.locked()


async def test_a_failed_pass_refuses_and_releases_the_lock(index_lock, monkeypatch):
    async def boom(user_id=None, **kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(indexer, "index_vault", boom)
    with pytest.raises(indexer.MoveRefreshRefused, match="RuntimeError"):
        await indexer.refresh_index_for_move(None, wait_seconds=1)
    assert not index_lock.locked()


async def test_a_busy_lock_refuses_after_the_wait(index_lock, monkeypatch):
    monkeypatch.setattr(indexer, "index_vault", _pass_returning(indexer.IndexPassResult(0, 0)))
    await index_lock.acquire()
    try:
        with pytest.raises(indexer.MoveRefreshRefused, match="over 0.05 s"):
            await indexer.refresh_index_for_move(None, wait_seconds=0.05)
    finally:
        index_lock.release()


async def test_a_paused_indexer_refuses(index_lock, monkeypatch):
    monkeypatch.setattr(indexer, "_is_paused", lambda: True)
    with pytest.raises(indexer.MoveRefreshRefused, match="paused"):
        await indexer.refresh_index_for_move(None, wait_seconds=1)


async def test_the_tool_seam_turns_a_refusal_into_index_not_ready(monkeypatch):
    async def refuse(uid, *, wait_seconds):
        raise indexer.MoveRefreshRefused("an index pass has been running for over 30 s")

    monkeypatch.setattr(indexer, "refresh_index_for_move", refuse)
    err = await tools._refresh_index_before_move(None)
    assert err is not None
    assert "Move aborted: an index pass has been running" in err
    assert "Nothing was moved" in err
    assert "index_not_ready" in err


async def test_the_tool_seam_passes_a_clean_refresh(monkeypatch):
    async def fine(uid, *, wait_seconds):
        return indexer.IndexPassResult(0, 0)

    monkeypatch.setattr(indexer, "refresh_index_for_move", fine)
    assert await tools._refresh_index_before_move(None) is None


# ── The stage locks (2026-11-09) ────────────────────────────────────────────


async def test_the_refresh_is_not_blocked_by_a_running_embed_stage(index_lock, monkeypatch):
    embed_lock = asyncio.Lock()
    monkeypatch.setattr(indexer, "embed_pass_lock", embed_lock)
    monkeypatch.setattr(indexer, "index_vault", _pass_returning(indexer.IndexPassResult(0, 0)))
    await embed_lock.acquire()
    try:
        await indexer.refresh_index_for_move(None, wait_seconds=0.5)
    finally:
        embed_lock.release()


async def test_each_stage_holds_only_its_own_lock(monkeypatch):
    index_lock, embed_lock = asyncio.Lock(), asyncio.Lock()
    monkeypatch.setattr(indexer, "index_pass_lock", index_lock)
    monkeypatch.setattr(indexer, "embed_pass_lock", embed_lock)
    seen = {}

    async def fake_index(user_id=None, **kwargs):
        seen["index"] = (index_lock.locked(), embed_lock.locked())
        return indexer.IndexPassResult(0, 0)

    async def fake_embed(user_id=None, **kwargs):
        seen["embed"] = (index_lock.locked(), embed_lock.locked())

    class NoRun:
        def record_index(self, *a):
            pass

        def record_embedded(self, *a):
            pass

        def record_error(self, *a):
            pass

    class Recorder:
        async def __aenter__(self):
            return NoRun()

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(indexer, "index_vault", fake_index)
    monkeypatch.setattr(indexer, "embed_vault", fake_embed)
    monkeypatch.setattr(indexer, "record_indexer_run", lambda *a, **k: Recorder())
    monkeypatch.setattr(indexer, "record_index_outcome", lambda *a, **k: None)
    monkeypatch.setattr(indexer, "record_embed_outcome", lambda *a, **k: None)

    assert await indexer._index_pass_once(None) is True
    assert seen == {"index": (True, False), "embed": (False, True)}


class _SweepSession:
    """Answers the sweep's one query with no rows, after running `during`."""

    def __init__(self, during):
        self.during = during

    async def execute(self, *a, **k):
        self.during()

        class Rows:
            def fetchall(self):
                return []

        return Rows()

    async def commit(self):
        return None


async def test_a_sweep_records_itself_clean_when_nothing_cleared_it():
    indexer.clear_sweep_state()
    await indexer._reconcile_exclusions(
        _SweepSession(lambda: None), None, -1, [], "", None, None
    )
    assert indexer._swept.get(None) == indexer.exclusion_fingerprint([])


async def test_a_clear_made_during_a_sweep_is_not_overwritten():
    """An index stage beside the embed stage may clear the record mid-sweep
    (a backstop, a re-derive); the sweep that started before must not record
    itself clean over that clear."""
    indexer.clear_sweep_state()
    await indexer._reconcile_exclusions(
        _SweepSession(lambda: indexer.clear_sweep_state(None)),
        None, -1, [], "", None, None,
    )
    assert None not in indexer._swept


async def test_the_panel_lock_takes_both_stage_locks_index_first(monkeypatch):
    from src.control_panel import routes

    order = []

    class Spy(asyncio.Lock):
        def __init__(self, name):
            super().__init__()
            self.name = name

        async def __aenter__(self):
            order.append(self.name)
            return await super().__aenter__()

    index_spy, embed_spy = Spy("index"), Spy("embed")
    monkeypatch.setattr(indexer, "index_pass_lock", index_spy)
    monkeypatch.setattr(indexer, "embed_pass_lock", embed_spy)

    class Session:
        async def close(self):
            return None

    async with routes._pass_lock_without_a_connection(Session()):
        assert index_spy.locked() and embed_spy.locked()
    assert order == ["index", "embed"]
