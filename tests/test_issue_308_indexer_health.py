"""#308 D4/D9 — indexer failure accounting, `/health` degraded, tick isolation.

The indexer failed every tick for 8.5 days while `/health` said `ok`; in
multi-user mode failures were not even counted. These tests pin the registry
(`src/services/indexer_health.py`), its callers in `run_indexer_loop`, the
`/health` shape, and `_on_indexer_done`. Fully offline.
"""

from __future__ import annotations

import asyncio
import sys
import contextlib
import json
from datetime import timedelta
import logging

import httpx
import pytest
from tests.conftest import INDEXER_LOOP_STOPPED, run_indexer_loop_until_stopped

from src.services import indexer, indexer_health
from src.services.indexer import EmbedPassResult


# --- helpers -----------------------------------------------------------------


def _health() -> tuple[int, dict, str]:
    from src.main import app

    async def _get():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("203.0.113.7", 4242)),
            base_url="http://localhost:8000",
        ) as client:
            return await client.get("/health")

    response = asyncio.run(_get())
    return response.status_code, response.json(), response.text


@pytest.fixture
def threshold(monkeypatch):
    monkeypatch.setattr(indexer_health.settings, "indexer_degraded_after_failures", 3)
    return 3


class _Stats:
    def __init__(self):
        self.errors = []

    def record_index(self, _r):
        pass

    def record_embedded(self, _r):
        pass

    def record_error(self, stage, exc):
        self.errors.append(f"{stage}: {exc}")


@pytest.fixture
def no_run_rows(monkeypatch):
    """`record_indexer_run` writes a DB row; these tests do not have one."""

    @contextlib.asynccontextmanager
    async def _run(_trigger, _uid):
        yield _Stats()

    monkeypatch.setattr(indexer, "record_indexer_run", _run)


def _two_loop_sleep(ticks):
    """A fake `asyncio.sleep` for both loops (separate since 2026-11-09).

    Each loop's sleeps are counted on its own, by task name. The embed loop
    gets its immediate first tick (no sleep) plus `ticks`, then parks; the
    index loop gets `ticks` ticks, waits for the embed loop to park, then
    ends the run.
    """
    state = {"index-loop": 0, "embed-loop": 0, "parked": asyncio.Event()}

    async def _sleep(*_a, **_k):
        name = asyncio.current_task().get_name()
        if name not in ("index-loop", "embed-loop"):
            return
        state[name] += 1
        if name == "embed-loop":
            if state[name] > ticks:
                state["parked"].set()
                await asyncio.get_running_loop().create_future()
            return
        if state[name] > ticks:
            await state["parked"].wait()
            raise asyncio.CancelledError

    return _sleep


def _loop_fixture(monkeypatch, *, multi_user, index, embed=None, ticks=1, calls=None, users=None):
    """Drive `run_indexer_loop` for the startup pass plus `ticks` ticks of
    each loop (the embed loop's immediate first tick is its startup stage)."""
    calls = calls if calls is not None else []

    async def _noop(*_a, **_k):
        return None

    async def _embed(user_id=None, **_k):
        calls.append(("embed", user_id))
        return await embed(user_id) if embed else None

    async def _cleanup():
        calls.append(("cleanup", None))

    async def _users():
        return [1, 2, 3]

    monkeypatch.setattr(indexer.settings, "multi_user_mode", multi_user)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(indexer, "detect_root_overlaps", _noop)
    monkeypatch.setattr(indexer, "_rotated_user_ids", users or _users)
    monkeypatch.setattr(indexer, "_active_user_ids", users or _users)
    monkeypatch.setattr(indexer, "_advance_rotation_cursor", _noop)
    monkeypatch.setattr(indexer, "index_vault", index)
    monkeypatch.setattr(indexer, "embed_vault", _embed)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "prewarm_search_caches", _noop)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _cleanup)
    monkeypatch.setattr(indexer, "flush_expired", _noop)
    monkeypatch.setattr(indexer.asyncio, "sleep", _two_loop_sleep(ticks))

    run_indexer_loop_until_stopped()
    return calls


# --- /health shapes -----------------------------------------------------------

EXPECTED_KEYS = {
    "status",
    "task_running",
    "failing_scopes",
    "embedding_failing_scopes",
    "max_consecutive_failures",
    "quarantined_notes",
    "last_success_at",
    "index_stalled",
    "seconds_since_progress",
    "embedding_stalled",
    "seconds_since_embedding_progress",
    "embedding_stuck_scopes",
    "seconds_embedding_stuck",
}


def test_health_ok(threshold):
    indexer_health.record_index(None, True)
    code, body, _ = _health()
    assert code == 200
    assert body["status"] == "ok"
    assert set(body["indexer"]) == EXPECTED_KEYS
    assert body["indexer"]["status"] == "ok"
    assert body["indexer"]["task_running"] is True
    assert body["indexer"]["last_success_at"] is not None
    # Existing fields are unchanged.
    assert "vault_named_staging_fallback_active" in body
    assert "transfer_mount_check_available" in body


def test_health_below_threshold_is_ok(threshold):
    indexer_health.record_index(None, False)
    indexer_health.record_index(None, False)
    code, body, _ = _health()
    assert (code, body["status"]) == (200, "ok")
    assert body["indexer"]["max_consecutive_failures"] == 2


def test_degraded_by_index_failures(threshold):
    for _ in range(3):
        indexer_health.record_index(None, False)
    code, body, _ = _health()
    assert code == 200
    assert body["status"] == "degraded"
    assert body["indexer"]["status"] == "degraded"
    assert body["indexer"]["failing_scopes"] == 1
    assert body["indexer"]["max_consecutive_failures"] >= 3


def test_degraded_by_embedding_failures(threshold):
    for _ in range(3):
        indexer_health.record_index(7, True)
        indexer_health.record_embed(7, 5)
    code, body, _ = _health()
    assert (code, body["status"]) == (200, "degraded")
    assert body["indexer"]["embedding_failing_scopes"] == 1
    assert body["indexer"]["failing_scopes"] == 0


def test_degraded_by_embedding_without_progress(threshold, monkeypatch):
    """Owner's report, 2026-11-13: notes stayed pending, nothing was embedded,
    no failure was counted, and `/health` said ok. Stages that end with notes
    pending and none embedded are no progress, whatever the cause."""
    monkeypatch.setattr(indexer_health.settings, "embed_stuck_after_seconds", 900)
    start = indexer_health._now()
    clock = [start]
    monkeypatch.setattr(indexer_health, "_now", lambda: clock[0])
    indexer_health.record_embed(None, 0)  # a clean stage: no failure counted
    indexer_health.record_embed_progress(None, 0, 2)
    clock[0] = start + timedelta(seconds=899)
    indexer_health.record_embed_progress(None, 0, 2)  # the clock keeps its start
    code, body, _ = _health()
    assert (code, body["status"]) == (200, "ok")
    assert body["indexer"]["seconds_embedding_stuck"] == 899

    clock[0] = start + timedelta(seconds=900)
    code, body, _ = _health()
    assert (code, body["status"]) == (200, "degraded")
    assert body["indexer"]["embedding_stuck_scopes"] == 1
    assert body["indexer"]["embedding_failing_scopes"] == 0

    # One note embedded is progress: ok at once.
    indexer_health.record_embed_progress(None, 1, 2)
    code, body, _ = _health()
    assert (code, body["status"]) == (200, "ok")
    assert body["indexer"]["seconds_embedding_stuck"] == 0


def test_embedding_without_progress_rules(monkeypatch):
    monkeypatch.setattr(indexer_health.settings, "embed_stuck_after_seconds", 60)
    start = indexer_health._now()
    clock = [start]
    monkeypatch.setattr(indexer_health, "_now", lambda: clock[0])
    later = start + timedelta(seconds=61)

    # Nothing pending is progress, even with nothing embedded.
    indexer_health.record_embed_progress(None, 0, 2)
    indexer_health.record_embed_progress(None, 0, 0)
    clock[0] = later
    assert indexer_health.snapshot()["embedding_stuck_scopes"] == 0

    # A raised stage (backlog unknown) is not.
    clock[0] = start
    indexer_health.record_embed_progress(None, 0, None)
    clock[0] = later
    assert indexer_health.snapshot()["embedding_stuck_scopes"] == 1

    # A paused tick clears the clock: the pause is reported on its own.
    indexer_health.record_embed_paused()
    assert indexer_health.snapshot()["embedding_stuck_scopes"] == 0

    # 0 disables the check.
    clock[0] = start
    indexer_health.record_embed_progress(None, 0, 3)
    clock[0] = later
    monkeypatch.setattr(indexer_health.settings, "embed_stuck_after_seconds", 0)
    assert indexer_health.snapshot()["embedding_stuck_scopes"] == 0


def test_embedding_without_progress_logs_critical_once(monkeypatch, caplog):
    monkeypatch.setattr(indexer_health.settings, "embed_stuck_after_seconds", 60)
    start = indexer_health._now()
    clock = [start]
    monkeypatch.setattr(indexer_health, "_now", lambda: clock[0])
    indexer_health.record_embed_progress(5, 0, 1)
    clock[0] = start + timedelta(seconds=120)
    with caplog.at_level(logging.CRITICAL, logger="src.services.indexer_health"):
        indexer_health.snapshot()
        indexer_health.snapshot()
    lines = [r for r in caplog.records if "no progress" in r.getMessage()]
    assert len(lines) == 1 and "user_id=5" in lines[0].getMessage()


def test_degraded_by_dead_task():
    indexer_health.mark_task_stopped()
    code, body, _ = _health()
    assert (code, body["status"]) == (200, "degraded")
    assert body["indexer"]["task_running"] is False


def test_degraded_by_enumeration_failures(threshold):
    for _ in range(3):
        indexer_health.record_enumeration(False)
    code, body, _ = _health()
    assert (code, body["status"]) == (200, "degraded")
    indexer_health.record_enumeration(True)
    assert _health()[1]["status"] == "ok"


def test_degraded_by_incomplete_rederive(threshold):
    for _ in range(3):
        indexer_health.record_rederive(4, False)
    _, body, _ = _health()
    assert body["status"] == "degraded"
    indexer_health.record_rederive(4, True)
    assert _health()[1]["status"] == "ok"


def test_degraded_by_quarantine():
    indexer_health.set_quarantine_count_provider(lambda: 1)
    code, body, _ = _health()
    assert (code, body["status"]) == (200, "degraded")
    assert body["indexer"]["quarantined_notes"] == 1


def test_a_failing_quarantine_provider_never_breaks_health():
    def _boom():
        raise RuntimeError("/obsidian/Secret.md")

    indexer_health.set_quarantine_count_provider(_boom)
    code, body, text = _health()
    assert (code, body["indexer"]["quarantined_notes"]) == (200, 0)
    assert "Secret" not in text


def test_disabled_under_sandbox_is_not_degraded():
    indexer_health.mark_disabled()
    code, body, _ = _health()
    assert code == 200
    assert body["status"] == "ok"
    assert body["indexer"]["status"] == "disabled"
    assert body["indexer"]["task_running"] is False


def test_body_names_no_path_error_or_user(threshold, no_run_rows, monkeypatch):
    """A path-bearing error from a tenant's pass must not reach `/health`."""
    planted = "/obsidian/tenants/Poison Note.md: 22021 invalid byte sequence"

    async def _index(user_id=None, **_k):
        raise RuntimeError(planted)

    async def _embed(user_id=None, **_k):
        raise RuntimeError(planted)

    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "embed_vault", _embed)
    indexer_health.set_quarantine_count_provider(lambda: 1)
    for _ in range(3):
        asyncio.run(indexer._index_pass_once(987654))

    code, body, text = _health()
    assert (code, body["status"]) == (200, "degraded")
    for needle in ("Poison", "obsidian", "tenants", "22021", "987654", "RuntimeError"):
        assert needle not in text, needle
    # Every value is a count, a flag, a status word or a timestamp.
    json.dumps(body)


# --- counting and the CRITICAL line ------------------------------------------


def _criticals(caplog):
    return [r for r in caplog.records if r.levelno == logging.CRITICAL]


def test_critical_logged_once_and_rearmed_after_reset(threshold, caplog):
    caplog.set_level(logging.CRITICAL, logger=indexer_health.__name__)
    for _ in range(5):
        indexer_health.record_index(3, False)
    assert len(_criticals(caplog)) == 1
    assert "manual intervention required" in _criticals(caplog)[0].getMessage()

    indexer_health.record_index(3, True)
    for _ in range(3):
        indexer_health.record_index(3, False)
    assert len(_criticals(caplog)) == 2, "the CRITICAL line was not re-armed"


def test_each_counter_alerts_independently(threshold, caplog):
    caplog.set_level(logging.CRITICAL, logger=indexer_health.__name__)
    for _ in range(3):
        indexer_health.record_index(None, False)
        indexer_health.record_embed(None, 1)
        indexer_health.record_enumeration(False)
    assert len(_criticals(caplog)) == 3


def test_multi_user_failing_tenant_counted_per_scope(threshold, caplog, no_run_rows, monkeypatch):
    """One tenant fails at startup and on two ticks; the others succeed."""
    caplog.set_level(logging.CRITICAL, logger=indexer_health.__name__)

    async def _index(user_id=None, **_k):
        if user_id == 2:
            raise RuntimeError("user 2's vault is broken")
        return (1, 0)

    _loop_fixture(monkeypatch, multi_user=True, index=_index, ticks=2)

    scopes = indexer_health._scopes
    assert scopes[2].index_consecutive_failures == 3
    assert scopes[1].index_consecutive_failures == 0
    assert scopes[3].index_consecutive_failures == 0
    assert len(_criticals(caplog)) == 1
    _, body, _ = _health()
    assert body["status"] == "degraded"
    assert body["indexer"]["failing_scopes"] == 1


def test_provider_outage_counted_while_indexing_succeeds(threshold, no_run_rows, monkeypatch):
    async def _index(user_id=None, **_k):
        return (1, 0)

    async def _embed(_uid):
        return EmbedPassResult(embedded=0, attempted=4, failures=4, first_error="x")

    _loop_fixture(monkeypatch, multi_user=False, index=_index, embed=_embed, ticks=2)
    state = indexer_health._scopes[None]
    assert state.embed_consecutive_failures == 3
    assert state.index_consecutive_failures == 0


def test_inactive_scopes_are_forgotten(threshold):
    for _ in range(3):
        indexer_health.record_index(9, False)
    indexer_health.retain_scopes([1, 2])
    assert _health()[1]["status"] == "ok"


def test_user_enumeration_failure_is_counted(threshold, no_run_rows, monkeypatch):
    state = {"startup": True}

    async def _index(user_id=None, **_k):
        return (1, 0)

    async def _users():
        if state["startup"]:
            state["startup"] = False
            return [1]
        raise RuntimeError("database is down")

    indexer_health.reset()
    calls = _loop_fixture(monkeypatch, multi_user=True, index=_index, ticks=3, users=_users)
    # Counted by the index loop's ticks only (the embed loop logs its own).
    assert indexer_health._enumeration_failures == 3
    assert _health()[1]["status"] == "degraded"
    # And the credential sweep still ran on every failing tick (D9).
    assert calls.count(("cleanup", None)) == 3


# --- D9: single-user tick isolation ------------------------------------------


def test_single_user_index_failure_still_embeds_and_cleans_up(threshold, no_run_rows, monkeypatch):
    phase = {"startup": True}

    async def _index(user_id=None, **_k):
        if phase["startup"]:
            phase["startup"] = False
            return (1, 1)
        raise RuntimeError("index stage failed")

    calls = _loop_fixture(monkeypatch, multi_user=False, index=_index, ticks=1)
    # The embed loop's startup and one tick both embed; the failing index
    # tick still cleans up. The loops interleave, so compare as a multiset.
    assert sorted(calls) == [("cleanup", None), ("embed", None), ("embed", None)]
    assert indexer_health._scopes[None].index_consecutive_failures == 1
    assert indexer.last_index_run_ok is False


def test_cleanup_failure_is_housekeeping(no_run_rows, monkeypatch):
    async def _index(user_id=None, **_k):
        return (1, 0)

    flushed = []

    async def _boom():
        raise RuntimeError("cleanup failed")

    async def _flush():
        flushed.append(1)

    _loop_fixture(monkeypatch, multi_user=False, index=_index, ticks=2)
    # `_loop_fixture` installs its own cleanup and flush; run again with the
    # failing cleanup and a counting flush.
    monkeypatch.setattr(indexer, "flush_expired", _flush)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _boom)
    monkeypatch.setattr(indexer.asyncio, "sleep", _two_loop_sleep(2))
    run_indexer_loop_until_stopped()
    assert flushed == [1, 1], "a cleanup failure stopped the refusal flush"
    assert indexer.last_index_run_ok is True


# --- _on_indexer_done ---------------------------------------------------------


def _finished_task(coro_fn):
    async def _run():
        task = asyncio.create_task(coro_fn())
        try:
            await task
        except BaseException:  # noqa: BLE001
            pass
        return task

    return asyncio.run(_run())


def test_lifespan_cancellation_is_not_a_dead_task():
    from src.main import _on_indexer_done

    async def _run():
        task = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return task

    task = asyncio.run(_run())
    _on_indexer_done(task)
    assert indexer_health.snapshot()["task_running"] is True
    assert _health()[1]["status"] == "ok"


def test_a_raising_task_is_recorded_dead():
    from src.main import _on_indexer_done

    async def _raises():
        raise RuntimeError("startup enumeration failed")

    _on_indexer_done(_finished_task(_raises))
    assert indexer_health.snapshot()["task_running"] is False
    assert _health()[1]["status"] == "degraded"


def test_an_unexpected_return_is_recorded_dead():
    from src.main import _on_indexer_done

    async def _returns():
        return None

    _on_indexer_done(_finished_task(_returns))
    assert indexer_health.snapshot()["task_running"] is False


def test_a_child_loop_cancelled_from_outside_is_recorded_dead(no_run_rows, monkeypatch):
    """A loop cancelled by anything but the parent (a leaked cancel scope) is
    a death, not a lifespan shutdown the done-callback would ignore."""
    from src.main import _on_indexer_done

    async def _cancelled():
        raise asyncio.CancelledError

    async def _forever():
        await asyncio.Event().wait()

    async def _index(user_id=None, **_k):
        return None

    monkeypatch.setattr(indexer, "_index_loop", _cancelled)
    monkeypatch.setattr(indexer, "run_embed_loop", _forever)
    # Unlike the other loop tests, this one wants the RuntimeError itself.
    monkeypatch.setattr(
        sys.modules[__name__],
        "run_indexer_loop_until_stopped",
        lambda: asyncio.run(indexer.run_indexer_loop()),
    )
    with pytest.raises(RuntimeError, match="cancelled unexpectedly") as raised:
        _loop_fixture(monkeypatch, multi_user=False, index=_index)

    async def _dies():
        raise raised.value

    _on_indexer_done(_finished_task(_dies))
    assert indexer_health.snapshot()["task_running"] is False


def test_the_embed_clock_does_not_run_during_the_startup_pass(no_run_rows, monkeypatch):
    seen = []

    async def _index(user_id=None, **_k):
        seen.append(indexer_health._embed_idle)

    async def _stop():
        raise RuntimeError("stop after startup")

    monkeypatch.setattr(indexer, "_index_loop", _stop)
    with pytest.raises(RuntimeError, match="stop after startup"):
        _loop_fixture(monkeypatch, multi_user=False, index=_index)
    assert seen == [True]


def test_each_users_embed_stage_is_progress(no_run_rows, monkeypatch):
    """One embed tick over many users, each with its own budget, may outlast
    the stall threshold: every finished stage stamps the embed clock."""
    calls = []

    async def _index(user_id=None, **_k):
        return None

    real = indexer_health.record_embed_loop_progress

    def _progress():
        calls.append(("progress", None))
        real()

    monkeypatch.setattr(indexer_health, "record_embed_loop_progress", _progress)
    _loop_fixture(monkeypatch, multi_user=True, index=_index, ticks=0, calls=calls)
    first_tick = calls[calls.index(("embed", 1)):calls.index(("embed", 3)) + 2]
    assert first_tick == [
        ("embed", 1), ("progress", None),
        ("embed", 2), ("progress", None),
        ("embed", 3), ("progress", None),
    ]


def test_sandbox_lifespan_marks_the_indexer_disabled(monkeypatch):
    """The real lifespan's sandbox branch, nothing external."""
    from src import main
    from src.services import vault_overlap

    class _SessionManager:
        def run(self):
            return _Async()

    class _Async:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Mcp:
        session_manager = _SessionManager()

    monkeypatch.setattr(main.settings, "mcp_sandbox_mode", True, raising=False)
    monkeypatch.setattr(vault_overlap.settings, "mcp_sandbox_mode", True, raising=False)
    monkeypatch.setattr(main, "mcp", _Mcp())

    async def _run():
        cm = main.lifespan(object())
        await cm.__aenter__()
        snap = indexer_health.snapshot()
        await cm.__aexit__(None, None, None)
        return snap

    snap = asyncio.run(_run())
    assert snap["status"] == "disabled"
    assert _health()[1]["status"] == "ok"
