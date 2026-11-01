"""The embed stage cannot hold the index stage back (2026-11-01).

For about 1h50 a single-scope embed pass worked through 208 notes against an
overloaded embedding server. The next index stage runs only after the embed
stage returns, so nothing new reached keyword search, backlinks or
`get_recent`, and `/health` said `ok` because nothing had failed yet. Three
things now bound or expose that:

* **the embed budget applies to a single scope too**, and a budget stop starts
  the next tick after `EMBED_BACKLOG_RETICK_SECONDS` (covered in
  `test_issue_202_fairness.py`);
* **the stage stops after `EMBED_STOP_AFTER_PROVIDER_FAILURES` notes in a row
  fail at the provider**, at a note boundary, and the loop then waits the
  ordinary interval;
* **`/health` reports `index_stalled`** when no index stage or tick has
  finished for `INDEXER_STALLED_AFTER_SECONDS`.

Fully offline.
"""
import asyncio
import logging
import os
import tempfile
from datetime import timedelta

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import src.services.indexer as indexer  # noqa: E402
from src.services import indexer_health  # noqa: E402
from src.services.embeddings import (  # noqa: E402
    EmbedNoteFailure,
    EmbedNoteResult,
    NoteEmbedOutcome,
)
from tests.test_issue_201_pass_record import (  # noqa: E402
    _fixture,
    _no_sweep,
    cardinality,
    certified_empty,
    embedded,
    generation_mismatch,
    provider_failed,
)


def _no_budget(monkeypatch, *, provider_failures: int = 3):
    monkeypatch.setattr(indexer.settings, "embed_chunk_budget_per_user", 0)
    monkeypatch.setattr(indexer.settings, "embed_time_budget_seconds_per_user", 0)
    monkeypatch.setattr(
        indexer.settings, "embed_stop_after_provider_failures", provider_failures
    )


def _vault_of(n: int) -> dict[str, str]:
    return {f"n{i}.md": f"body {i}\n" for i in range(n)}


def _too_large() -> EmbedNoteResult:
    return EmbedNoteResult(
        outcome=NoteEmbedOutcome.PROVIDER_FAILED,
        chunks_submitted=2,
        chunks_embedded=0,
        truncated=False,
        failure=EmbedNoteFailure(
            exc_type="ProviderInputTooLarge", message="too long", requested=2
        ),
    )


def _run_with(monkeypatch, tmp_path, results, n=10):
    """Embed `n` notes, answering note i with `results(i)`; return the
    pass result and the notes the provider was asked for."""
    _fixture(monkeypatch, tmp_path, _vault_of(n))
    _no_sweep(monkeypatch)
    calls = []

    async def _embed(_session, note, _content, **_kw):
        calls.append(note.file_path)
        return results(len(calls) - 1)

    monkeypatch.setattr(indexer, "embed_note", _embed)
    return asyncio.run(indexer.embed_vault(user_id=7)), calls


# ══════════════════════════════════════════════════════════════════════════
# The provider stop
# ══════════════════════════════════════════════════════════════════════════


def test_consecutive_provider_failures_stop_the_stage(monkeypatch, tmp_path, caplog):
    _no_budget(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="src.services.indexer"):
        result, calls = _run_with(
            monkeypatch, tmp_path, lambda _i: provider_failed("500 from gateway")
        )
    assert calls == ["n0.md", "n1.md", "n2.md"], "the stage pressed on"
    assert result.failures == 3
    assert result.stopped is indexer.EmbedStop.PROVIDER_FAILURES
    # The run row says the rest was not tried, not only what failed.
    assert "stage stopped after consecutive provider failures" in (
        result.failure_summary
    )
    stops = [r for r in caplog.records if "Embedding stopped" in r.getMessage()]
    assert len(stops) == 1
    assert "remaining 7" in stops[0].getMessage()


def test_a_cardinality_mismatch_counts_towards_the_stop(monkeypatch, tmp_path):
    _no_budget(monkeypatch)
    result, calls = _run_with(monkeypatch, tmp_path, lambda _i: cardinality())
    assert len(calls) == 3
    assert result.stopped is indexer.EmbedStop.PROVIDER_FAILURES


def test_a_success_resets_the_streak(monkeypatch, tmp_path):
    _no_budget(monkeypatch)

    # fail, fail, ok, fail, fail, ok, ... never three in a row.
    def _answer(i):
        return embedded(1) if i % 3 == 2 else provider_failed("blip")

    result, calls = _run_with(monkeypatch, tmp_path, _answer)
    assert len(calls) == 10
    assert result.stopped is None


def test_a_generation_mismatch_resets_the_streak(monkeypatch, tmp_path):
    """The provider answered; only the configuration moved."""
    _no_budget(monkeypatch)

    def _answer(i):
        return generation_mismatch() if i % 3 == 2 else provider_failed("blip")

    _result, calls = _run_with(monkeypatch, tmp_path, _answer)
    assert len(calls) == 10


def test_a_note_refused_as_too_large_is_not_an_outage(monkeypatch, tmp_path):
    _no_budget(monkeypatch)
    result, calls = _run_with(monkeypatch, tmp_path, lambda _i: _too_large())
    assert len(calls) == 10, "a size refusal stopped the stage"
    assert result.stopped is None
    assert result.failures == 10


def test_a_size_refusal_does_not_reset_the_streak_either(monkeypatch, tmp_path):
    """fail, too-large, fail, fail: the too-large note is neutral, so the
    third provider failure is the third in a row."""
    _no_budget(monkeypatch)
    answers = [provider_failed("x"), _too_large(), provider_failed("x"),
               provider_failed("x")]
    result, calls = _run_with(
        monkeypatch, tmp_path, lambda i: answers[i] if i < 4 else embedded(1)
    )
    assert len(calls) == 4
    assert result.stopped is indexer.EmbedStop.PROVIDER_FAILURES


def test_a_note_with_no_provider_call_is_neutral(monkeypatch, tmp_path):
    _no_budget(monkeypatch)
    answers = [provider_failed("x"), provider_failed("x"), certified_empty(),
               provider_failed("x")]
    _result, calls = _run_with(
        monkeypatch, tmp_path, lambda i: answers[i] if i < 4 else embedded(1)
    )
    assert len(calls) == 4


def test_zero_disables_the_provider_stop(monkeypatch, tmp_path):
    _no_budget(monkeypatch, provider_failures=0)
    result, calls = _run_with(
        monkeypatch, tmp_path, lambda _i: provider_failed("down")
    )
    assert len(calls) == 10
    assert result.stopped is None


def test_failures_on_the_last_notes_are_not_a_stop(monkeypatch, tmp_path):
    """The stop is checked before a note; three failures that end the backlog
    leave nothing to stop, and the summary must not claim otherwise."""
    _no_budget(monkeypatch)
    result, calls = _run_with(
        monkeypatch, tmp_path, lambda _i: provider_failed("down"), n=3
    )
    assert len(calls) == 3
    assert result.stopped is None
    assert "stage stopped" not in result.failure_summary


def test_a_budget_check_does_not_overwrite_a_provider_stop():
    """The sweep checks the budget after the backlog stopped for the
    provider; a re-tick against a down provider must not follow."""
    budget = indexer.EmbedBudget(provider_failure_limit=1)
    budget.provider_failure_streak = 1
    budget.stop_for_provider("", "the hash-mismatch backlog", 5)
    budget.stop("", "the exclusion-reconciliation sweep")
    assert budget.stopped is indexer.EmbedStop.PROVIDER_FAILURES


# ══════════════════════════════════════════════════════════════════════════
# The loop's next-tick delay
# ══════════════════════════════════════════════════════════════════════════


def _drive_loop(monkeypatch, embed_result, *, ticks=2):
    """Run the single-user loop for the startup pass and `ticks` ticks;
    return the delays it slept for."""
    delays = []

    async def _noop(*_a, **_k):
        return None

    async def _index(*_a, **_k):
        return (1, 0)

    async def _embed(*_a, **_k):
        return embed_result()

    async def _sleep(seconds, *_a, **_k):
        delays.append(seconds)
        if len(delays) > ticks:
            raise asyncio.CancelledError

    import contextlib

    class _Stats:
        def record_index(self, _r):
            pass

        def record_embedded(self, _r):
            pass

        def record_error(self, *_a):
            pass

    @contextlib.asynccontextmanager
    async def _run(*_a):
        yield _Stats()

    monkeypatch.setattr(indexer.settings, "multi_user_mode", False)
    monkeypatch.setattr(indexer.settings, "index_interval_seconds", 300)
    monkeypatch.setattr(indexer, "record_indexer_run", _run)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(indexer, "detect_root_overlaps", _noop)
    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "embed_vault", _embed)
    monkeypatch.setattr(indexer, "link_backfill_pass", _noop)
    monkeypatch.setattr(indexer, "prewarm_search_caches", _noop)
    monkeypatch.setattr(indexer, "cleanup_expired_tokens", _noop)
    monkeypatch.setattr(indexer, "flush_expired", _noop)
    monkeypatch.setattr(indexer.asyncio, "sleep", _sleep)
    indexer.take_embed_backlog_left()
    try:
        asyncio.run(indexer.run_indexer_loop())
    except asyncio.CancelledError:
        pass
    return delays


def test_a_budget_stop_shortens_the_next_sleep(monkeypatch):
    delays = _drive_loop(
        monkeypatch,
        lambda: indexer.EmbedPassResult(stopped=indexer.EmbedStop.BUDGET),
    )
    assert delays == [indexer.EMBED_BACKLOG_RETICK_SECONDS] * 3


def test_a_provider_stop_waits_the_interval(monkeypatch):
    delays = _drive_loop(
        monkeypatch,
        lambda: indexer.EmbedPassResult(
            stopped=indexer.EmbedStop.PROVIDER_FAILURES
        ),
    )
    assert delays == [300] * 3


def test_a_finished_stage_waits_the_interval(monkeypatch):
    delays = _drive_loop(monkeypatch, indexer.EmbedPassResult)
    assert delays == [300] * 3


# ══════════════════════════════════════════════════════════════════════════
# /health: index_stalled
# ══════════════════════════════════════════════════════════════════════════


def _age_progress(monkeypatch, seconds):
    """Pretend the last progress stamp is `seconds` old."""
    real_now = indexer_health._now
    monkeypatch.setattr(
        indexer_health, "_now", lambda: real_now() + timedelta(seconds=seconds)
    )


def test_no_progress_for_the_threshold_is_a_stall(monkeypatch, caplog):
    monkeypatch.setattr(indexer_health.settings, "indexer_stalled_after_seconds", 1800)
    indexer_health.record_index(None, True)
    assert indexer_health.snapshot()["index_stalled"] is False

    _age_progress(monkeypatch, 1801)
    with caplog.at_level(logging.CRITICAL, logger="src.services.indexer_health"):
        snap = indexer_health.snapshot()
        indexer_health.snapshot()
    assert snap["index_stalled"] is True
    assert snap["status"] == "degraded"
    assert snap["seconds_since_progress"] >= 1800
    criticals = [r for r in caplog.records if "no progress" in r.getMessage()]
    assert len(criticals) == 1, "the CRITICAL line must fire once per episode"


def test_progress_clears_the_stall_and_rearms_the_alert(monkeypatch, caplog):
    monkeypatch.setattr(indexer_health.settings, "indexer_stalled_after_seconds", 60)
    _age_progress(monkeypatch, 61)
    assert indexer_health.snapshot()["index_stalled"] is True, (
        "a loop that never stamped is measured from process start"
    )
    indexer_health.record_loop_progress()
    assert indexer_health.snapshot()["index_stalled"] is False
    assert indexer_health.snapshot()["status"] == "ok"
    assert indexer_health._stall_alerted is False


def test_a_tick_end_counts_as_progress(monkeypatch):
    monkeypatch.setattr(indexer_health.settings, "indexer_stalled_after_seconds", 60)
    indexer._record_index_run(False)
    assert indexer_health._last_progress_at is not None


def test_zero_disables_the_stall_check(monkeypatch):
    monkeypatch.setattr(indexer_health.settings, "indexer_stalled_after_seconds", 0)
    _age_progress(monkeypatch, 10**6)
    assert indexer_health.snapshot()["index_stalled"] is False


def test_a_disabled_or_dead_indexer_is_not_a_stall(monkeypatch):
    monkeypatch.setattr(indexer_health.settings, "indexer_stalled_after_seconds", 60)
    _age_progress(monkeypatch, 61)
    indexer_health.mark_disabled()
    assert indexer_health.snapshot()["index_stalled"] is False
    assert indexer_health.snapshot()["status"] == "disabled"


# ══════════════════════════════════════════════════════════════════════════
# Adversarial-review regressions
# ══════════════════════════════════════════════════════════════════════════


def test_notes_that_always_fail_do_not_block_the_backlog(monkeypatch, tmp_path):
    """Three notes that fail on every attempt, with a healthy provider, sit at
    the head of the backlog. The first pass stops on them; every later pass
    tries them last, so the rest is embedded and they cannot stop anything."""
    _no_budget(monkeypatch)
    poison = {"n0.md", "n1.md", "n2.md"}
    seen = []
    for _ in range(2):
        calls = []
        _fixture(monkeypatch, tmp_path, _vault_of(10))
        _no_sweep(monkeypatch)

        async def _embed(_s, note, _c, **_k):
            calls.append(note.file_path)
            return provider_failed("500") if note.file_path in poison else embedded(1)

        monkeypatch.setattr(indexer, "embed_note", _embed)
        result = asyncio.run(indexer.embed_vault(user_id=7))
        seen.append((calls, result.stopped))

    assert seen[0] == (["n0.md", "n1.md", "n2.md"], indexer.EmbedStop.PROVIDER_FAILURES)
    calls, stopped = seen[1]
    assert calls[:7] == [f"n{i}.md" for i in range(3, 10)], "healthy notes first"
    assert sorted(calls[7:]) == sorted(poison), "known failures last"
    assert stopped is None


def test_budget_and_streak_at_one_boundary_is_a_provider_stop(monkeypatch, tmp_path):
    """Chunk budget 9, three failing notes of 3 chunks each: both run out at
    the same boundary. A budget stop would re-tick every 5 s against a down
    provider, so the provider stop wins."""
    _fixture(monkeypatch, tmp_path, _vault_of(10))
    _no_sweep(monkeypatch)
    monkeypatch.setattr(indexer.settings, "embed_chunk_budget_per_user", 9)
    monkeypatch.setattr(indexer.settings, "embed_time_budget_seconds_per_user", 0)
    monkeypatch.setattr(indexer.settings, "embed_stop_after_provider_failures", 3)

    async def _embed(_s, note, _c, on_provider_call=None, **_k):
        if on_provider_call:
            on_provider_call(3)
        return provider_failed("down", chunks=3)

    monkeypatch.setattr(indexer, "embed_note", _embed)
    result = asyncio.run(indexer.embed_vault(user_id=7))
    indexer.take_embed_backlog_left()
    indexer.record_embed_outcome(7, result)
    assert result.stopped is indexer.EmbedStop.PROVIDER_FAILURES
    assert indexer.take_embed_backlog_left() is False


def test_a_budget_stop_while_failing_does_not_retick(monkeypatch):
    """The time budget ran out with failures in progress (each failing note
    slower than budget / limit): a budget stop, but not a prompt re-tick."""
    indexer.take_embed_backlog_left()
    result = indexer.EmbedPassResult(
        stopped=indexer.EmbedStop.BUDGET, provider_failure_streak=2
    )
    indexer.record_embed_outcome(None, result)
    assert indexer.take_embed_backlog_left() is False


def test_the_streak_carries_over_and_the_next_stage_probes_one_note(
    monkeypatch, tmp_path
):
    """A stage cut short with two failures hands them on; the next stage
    probes one note, and a third failure stops it there. Had the provider
    recovered, that one note would reset the streak."""
    _no_budget(monkeypatch)
    indexer._provider_failure_streaks[7] = 2
    result, calls = _run_with(monkeypatch, tmp_path, lambda _i: provider_failed("down"))
    assert len(calls) == 1
    assert result.stopped is indexer.EmbedStop.PROVIDER_FAILURES

    indexer._provider_failure_streaks[7] = 3
    result, calls = _run_with(monkeypatch, tmp_path, lambda _i: embedded(1))
    assert len(calls) == 10, "a recovered provider was never probed"
    assert result.stopped is None
    assert indexer._provider_failure_streaks[7] == 0


def test_failures_ending_the_backlog_do_not_stop_the_sweep(monkeypatch, tmp_path):
    """The backlog's last three notes fail; the real sweep then still removes
    an excluded note's vectors, which needs no provider call, and completes."""
    _fixture(
        monkeypatch, tmp_path, _vault_of(3),
        sweep=[("Private/secret.md", "secret\n", True)],
    )
    monkeypatch.setattr(
        indexer.settings, "embedding_exclude_patterns", ["Private/*"], raising=False
    )
    _no_budget(monkeypatch)
    indexer._swept.clear()

    async def _embed(_s, _note, _c, **_k):
        return provider_failed("500 on this note")

    monkeypatch.setattr(indexer, "embed_note", _embed)
    result = asyncio.run(indexer.embed_vault(user_id=7))
    assert result.stopped is None
    assert 7 in indexer._swept, "the sweep was stopped by a streak it inherited"
    assert "stage stopped" not in result.failure_summary


def test_sleeping_longer_than_the_threshold_is_not_a_stall(monkeypatch):
    monkeypatch.setattr(indexer_health.settings, "indexer_stalled_after_seconds", 60)
    indexer_health.record_loop_idle()
    _age_progress(monkeypatch, 3600)
    assert indexer_health.snapshot()["index_stalled"] is False
    # The tick starts: the clock runs again from there.
    indexer_health.record_loop_progress()
    assert indexer_health.snapshot()["index_stalled"] is False
