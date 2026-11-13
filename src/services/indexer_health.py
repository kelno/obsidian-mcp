"""Indexer failure accounting, and the `/health` view of it (#308, D4).

For 8.5 days an indexer failed every tick while `/health` answered `ok` and the
only signal was one CRITICAL log line nobody read — and in multi-user mode the
failures were not even counted, because the counter was a local of the
single-user branch of `run_indexer_loop`. This module is the one place those
counts now live, so that every entrypoint (the startup pass, the periodic tick
in both modes, the panel's Reindex now) updates the same state and `/health`
can read it.

**Per scope** — `None` for single-user mode, else the user id:

- `index_consecutive_failures`: an index pass that raised (after the
  quarantine recovery); reset by one that returned;
- `embed_consecutive_failures`: an embed pass that reported failures (or
  raised); reset by one with none;
- `rederive_incomplete`: a committed re-derive that withheld its provenance
  stamp; reset by one that recorded it, or by a pass that was not a re-derive;
- `walk_incomplete`: a committed pass that could not list one or more
  directories beneath the root (#309, D4); reset by one that listed them all;
- `last_success_at` / `last_failure_at` of the index stage;
- `rechunk_pending` / `rechunk_observed`: the notes the scope's last embed
  stage counted as not yet re-chunked (#355), and whether any embed stage of
  this process has counted them at all. Published only as the process-wide,
  non-volumetric `rechunk: "in_progress" | "complete"` — never a number, never
  in `status`. "Not yet observed" is not "zero pending": until every known
  scope has been counted (and at least one has), the state is `in_progress`,
  so a fresh process over a pre-034 index does not claim `complete` before
  its first embed stage. Under `MCP_SANDBOX_MODE` no embed stage ever runs,
  and the state stays `complete` (nothing this process embeds is pending).
- `embed_stuck_since`: when this scope's embed stages started ending with
  notes pending and none embedded; cleared by a stage that embedded one or
  found nothing pending (see "Embedding without progress" below).

**Process-wide**: consecutive tick-level failures not attributable to a scope
(user enumeration, root-overlap detection, a tick that raised outside any
scope's stages); whether the indexer task is still running; whether indexing
is disabled (`MCP_SANDBOX_MODE`); a pluggable count of quarantined notes; and
when the loop last made progress.

**Progress, not failure.** Every counter above moves only when a stage
*returns*. On 2026-11-01 an embed stage did not return for about 1h50 — it was
working through a backlog against an overloaded embedding server — and for all
of it no index stage ran, nothing new reached keyword search or the link
graph, and `/health` said `ok`, because nothing had failed yet. So the loop
also stamps its progress: when a tick starts or finishes (either way), when an
index stage finishes (either way) and when a tick is skipped by the pause; the
sleep between ticks stops the clock. If none of those has happened for
`INDEXER_STALLED_AFTER_SECONDS` outside a sleep, `/health` reports
`index_stalled` and `degraded`, and one CRITICAL line is logged when `/health`
is read (it is evaluated there, so a deployment that never polls `/health`
never logs it), re-armed by the next stamp. A tick in progress for one scope stamps when that scope's
index stage finishes, so a long multi-user tick is not a stall unless one
scope's pass alone exceeds the threshold.

**Embedding without progress** (fork, owner's report 2026-11-13). The failure
counter above counts stages that *report* failed notes, and a stage can end
cleanly while embedding nothing: a fingerprint skip, a note skipped because its
bytes no longer match its row, a certification that lost its race, a failure
on a path that does not count. On 2026-11-13 two notes stayed pending for half
an hour while `/health` said `ok`. So each embed stage also reports how many
notes it embedded and how many were pending when it began, and a scope whose
stages have ended with notes pending and none embedded for
`EMBED_STUCK_AFTER_SECONDS` is `embedding_stuck`, whatever the cause. A stage
that raised counts as no progress with the backlog unknown. A paused tick
clears the clock: a pause is an operator's choice, reported on its own.

**In-process only and never persisted**: a restart starts clean, which is the
same horizon as the task it describes. `/health` reads `snapshot()`, which
carries counts only — no path, error text, SQLSTATE or user id, because
`/health` is unauthenticated and publicly routed.

The CRITICAL "manual intervention required" line is logged once when a counter
first reaches `INDEXER_DEGRADED_AFTER_FAILURES`, and re-armed when the counter
resets — once per episode, not once per tick.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from src.config import settings

logger = logging.getLogger(__name__)

Scope = int | None

_INDEX = "index"
_EMBED = "embed"
_REDERIVE = "rederive"
_WALK = "walk"
_EMBED_STUCK = "embed_stuck"

#: The start of the run-record line a pass that quarantined notes writes into
#: `indexer_runs.error` (#308, D5). Produced by `indexer.format_quarantined`
#: and recognised by `run_outcome` — one constant, so the two cannot drift.
QUARANTINED_RUN_PREFIX = "quarantined "
_QUARANTINED_RUN_LINE = re.compile(
    re.escape(QUARANTINED_RUN_PREFIX) + r"\d+ note\(s\): "
)

#: The start of the run-record line a pass that could not list a directory
#: writes (#309, D2). Produced by `indexer.format_walk_incomplete`. Not
#: recognised as a quarantine line, so such a run is labelled `failed`.
WALK_INCOMPLETE_RUN_PREFIX = "walk incomplete: "

RUN_OK = "ok"
RUN_QUARANTINED = "quarantined"
RUN_FAILED = "failed"


def run_outcome(error: str | None) -> str:
    """How the panel labels one `indexer_runs` row by its `error` text.

    A pass that quarantined notes **succeeded** (D5) but names them in
    `error`, so a non-empty `error` is not on its own a failure. The row is
    `quarantined` only when every line of `error` is a quarantine line —
    `format_quarantined` escapes line breaks in the paths it names, so a line
    of the record is either wholly that line or another stage's report, and
    any other line (a stage that raised, embed failures) makes it `failed`.
    """
    if not error:
        return RUN_OK
    lines = error.split("\n")
    if all(_QUARANTINED_RUN_LINE.match(line) for line in lines):
        return RUN_QUARANTINED
    return RUN_FAILED


@dataclass
class ScopeHealth:
    index_consecutive_failures: int = 0
    embed_consecutive_failures: int = 0
    rederive_incomplete: int = 0
    walk_incomplete: int = 0
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    #: When this scope's embed stages started ending with notes pending and
    #: none embedded; None while they make progress.
    embed_stuck_since: datetime | None = None
    #: Counters whose CRITICAL line has fired this episode.
    alerted: set[str] = field(default_factory=set)
    #: Notes of this scope not yet re-chunked (#355, D4), as the scope's most
    #: recent embed stage counted them under the one predicate. In process
    #: memory only, and **never** exposed: `/health` publishes only whether
    #: any scope's count is non-zero.
    rechunk_pending: int = 0
    #: Whether an embed stage of this process has counted this scope yet.
    #: Until it has, the scope's state is unknown and `/health` must not
    #: read `complete` on its account.
    rechunk_observed: bool = False


_scopes: dict[Scope, ScopeHealth] = {}
_enumeration_failures = 0
_enumeration_alerted = False
_task_running = True
_disabled = False
_quarantine_count_provider: Callable[[], int] | None = None
_last_progress_at: datetime | None = None
_stall_alerted = False
#: True while the loop sleeps between ticks; a sleep is never a stall.
_idle = False
#: The embed loop's own stall clock (since the stages got separate loops,
#: 2026-11-09): a stuck embed stage no longer stops the index loop, so the
#: index clock above cannot see it. Same rules, same threshold.
_embed_progress_at: datetime | None = None
_embed_stall_alerted = False
_embed_idle = False


def _threshold() -> int:
    return settings.indexer_degraded_after_failures


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: The stall clock's origin until the loop stamps its first progress.
_process_started_at = _now()


def _scope(scope: Scope) -> ScopeHealth:
    state = _scopes.get(scope)
    if state is None:
        state = _scopes[scope] = ScopeHealth()
    return state


def _describe(scope: Scope) -> str:
    return "single-user scope" if scope is None else f"user_id={scope}"


def _bump(scope: Scope, state: ScopeHealth, counter: str, attr: str, what: str) -> None:
    value = getattr(state, attr) + 1
    setattr(state, attr, value)
    if value >= _threshold() and counter not in state.alerted:
        state.alerted.add(counter)
        logger.critical(
            "Indexer %s has failed %d consecutive times (%s) — manual "
            "intervention required",
            what,
            value,
            _describe(scope),
        )


def _reset(state: ScopeHealth, counter: str, attr: str) -> None:
    setattr(state, attr, 0)
    state.alerted.discard(counter)


def record_loop_progress() -> None:
    """The loop reached a point that proves it is not stuck: a tick started
    or finished, an index stage finished, or a tick was skipped by the
    pause."""
    global _last_progress_at, _stall_alerted, _idle
    _last_progress_at = _now()
    _stall_alerted = False
    _idle = False


def record_loop_idle() -> None:
    """The loop is about to sleep until its next tick. The stall clock stops
    until `record_loop_progress` (the tick's start) restarts it, so an
    `INDEX_INTERVAL_SECONDS` longer than the stall threshold is not a stall."""
    global _idle
    record_loop_progress()
    _idle = True


def record_embed_loop_progress() -> None:
    """The embed loop reached a point that proves it is not stuck: a tick
    started, an embed stage finished, or a tick was skipped by the pause."""
    global _embed_progress_at, _embed_stall_alerted, _embed_idle
    _embed_progress_at = _now()
    _embed_stall_alerted = False
    _embed_idle = False


def record_embed_loop_idle() -> None:
    """The embed loop is about to sleep until its next tick."""
    global _embed_idle
    record_embed_loop_progress()
    _embed_idle = True


def _embed_stalled(now: datetime) -> tuple[bool, int]:
    """`_index_stalled`'s rule, on the embed loop's clock."""
    global _embed_stall_alerted
    since = _embed_progress_at or _process_started_at
    seconds = max(0, int((now - since).total_seconds()))
    limit = settings.indexer_stalled_after_seconds
    stalled = (
        bool(limit)
        and not _embed_idle
        and _task_running
        and not _disabled
        and seconds >= limit
    )
    if stalled and not _embed_stall_alerted:
        _embed_stall_alerted = True
        logger.critical(
            "Embedding has made no progress for %ds (no embed stage or tick "
            "finished; threshold INDEXER_STALLED_AFTER_SECONDS=%d): semantic "
            "search is not being updated. An embed stage is running far longer "
            "than it should — check the embedding server and the indexer's "
            "progress lines",
            seconds,
            limit,
        )
    return stalled, seconds


def _index_stalled(now: datetime) -> tuple[bool, int]:
    """Whether the loop has made no progress for the stall threshold, and
    for how many whole seconds it has not.

    Measured from process start until the first stamp, so a startup pass that
    never returns is caught too. Not a stall when the task is not running
    (that is its own signal) or indexing is disabled.
    """
    global _stall_alerted
    since = _last_progress_at or _process_started_at
    # While idle this reports the time since the sleep began, which is not
    # a stall and is never compared below.
    seconds = max(0, int((now - since).total_seconds()))
    limit = settings.indexer_stalled_after_seconds
    stalled = (
        bool(limit)
        and not _idle
        and _task_running
        and not _disabled
        and seconds >= limit
    )
    if stalled and not _stall_alerted:
        _stall_alerted = True
        logger.critical(
            "Indexer has made no progress for %ds (no index stage or tick "
            "finished; threshold INDEXER_STALLED_AFTER_SECONDS=%d): keyword "
            "search, backlinks and recent notes are not being updated. A stage "
            "is running far longer than it should — check the embedding "
            "server and the indexer's progress lines",
            seconds,
            limit,
        )
    return stalled, seconds


def record_index(scope: Scope, ok: bool) -> None:
    """One index pass for `scope` returned (`ok`) or raised."""
    record_loop_progress()
    state = _scope(scope)
    if ok:
        _reset(state, _INDEX, "index_consecutive_failures")
        state.last_success_at = _now()
    else:
        state.last_failure_at = _now()
        _bump(scope, state, _INDEX, "index_consecutive_failures", "index pass")


def record_embed(scope: Scope, failures: int) -> None:
    """One embed pass for `scope` completed with `failures` failed notes.

    Zero resets the counter; any failure (or a raised stage, recorded by the
    caller as 1) counts the pass as failed.
    """
    state = _scope(scope)
    if failures:
        _bump(scope, state, _EMBED, "embed_consecutive_failures", "embed pass")
    else:
        _reset(state, _EMBED, "embed_consecutive_failures")


def record_embed_progress(scope: Scope, embedded: int, pending: int | None) -> None:
    """One embed stage for `scope` ended having embedded `embedded` notes, with
    `pending` notes to embed when it began (`None`: unknown, as for a stage
    that raised).

    Progress — any note embedded, or nothing pending — clears the scope's
    stuck clock; anything else starts it, if it is not already running.
    """
    state = _scope(scope)
    if embedded > 0 or pending == 0:
        if state.embed_stuck_since is not None:
            state.embed_stuck_since = None
            state.alerted.discard(_EMBED_STUCK)
        return
    if state.embed_stuck_since is None:
        state.embed_stuck_since = _now()


def record_embed_paused() -> None:
    """The embed loop skipped a tick because indexing is paused: no scope's
    lack of progress is the embed stage's while it is."""
    for state in _scopes.values():
        state.embed_stuck_since = None
        state.alerted.discard(_EMBED_STUCK)


def _embed_stuck(now: datetime) -> tuple[int, int]:
    """How many scopes have had notes pending and none embedded for
    `EMBED_STUCK_AFTER_SECONDS`, and the longest such streak in whole seconds
    (below the threshold too, 0 when none is running)."""
    limit = settings.embed_stuck_after_seconds
    stuck = 0
    longest = 0
    for scope, state in _scopes.items():
        if state.embed_stuck_since is None:
            continue
        seconds = max(0, int((now - state.embed_stuck_since).total_seconds()))
        longest = max(longest, seconds)
        if not limit or seconds < limit or not _task_running or _disabled:
            continue
        stuck += 1
        if _EMBED_STUCK not in state.alerted:
            state.alerted.add(_EMBED_STUCK)
            logger.critical(
                "Embedding has made no progress for %ds (%s): notes are "
                "pending and no embed stage has embedded one (threshold "
                "EMBED_STUCK_AFTER_SECONDS=%d). Semantic search is not being "
                "updated — check the embedding server and the embed stage's "
                "log lines",
                seconds,
                _describe(scope),
                limit,
            )
    return stuck, longest


def record_rederive(scope: Scope, complete: bool | None) -> None:
    """A committed pass's re-derive outcome.

    `True` — the re-derive recorded provenance; `False` — it withheld the
    stamp (incomplete); `None` — the pass was not a re-derive, which also
    resets the counter (the scope left re-derive).
    """
    state = _scope(scope)
    if complete is False:
        _bump(scope, state, _REDERIVE, "rederive_incomplete", "re-derive")
    else:
        _reset(state, _REDERIVE, "rederive_incomplete")


def record_walk(scope: Scope, incomplete: bool) -> None:
    """A committed pass's walk: `incomplete` when it could not list one or
    more directories beneath the root (#309, D4); a complete walk resets."""
    state = _scope(scope)
    if incomplete:
        _bump(scope, state, _WALK, "walk_incomplete", "walk")
    else:
        _reset(state, _WALK, "walk_incomplete")


def record_rechunk(scope: Scope, pending: int) -> None:
    """The scope's embed stage counted `pending` notes whose
    `embedded_chunking_version IS DISTINCT FROM CHUNKING_VERSION` (#355).

    Not a failure counter: a long re-embed degrades nothing, alerts nothing
    and never changes `status` or the HTTP code.
    """
    state = _scope(scope)
    state.rechunk_pending = max(0, int(pending))
    state.rechunk_observed = True


def _rechunk_state(states: list[ScopeHealth]) -> str:
    """`complete` only when at least one scope has been counted and every
    known scope was counted with zero pending; `in_progress` otherwise,
    including before the first embed stage after a process start. A
    disabled indexer (`MCP_SANDBOX_MODE`) never counts, so it reads
    `complete` rather than `in_progress` forever."""
    if _disabled:
        return "complete"
    if not any(s.rechunk_observed for s in states):
        return "in_progress"
    if any(not s.rechunk_observed or s.rechunk_pending > 0 for s in states):
        return "in_progress"
    return "complete"


def record_enumeration(ok: bool) -> None:
    """One tick's scope-independent work succeeded (`ok`) or failed."""
    global _enumeration_failures, _enumeration_alerted
    if ok:
        _enumeration_failures = 0
        _enumeration_alerted = False
        return
    _enumeration_failures += 1
    if _enumeration_failures >= _threshold() and not _enumeration_alerted:
        _enumeration_alerted = True
        logger.critical(
            "Indexer tick (user enumeration / overlap detection) has failed "
            "%d consecutive times — manual intervention required",
            _enumeration_failures,
        )


def retain_scopes(active: Iterable[int]) -> None:
    """Forget per-user scopes no longer active (multi-user mode).

    A deactivated or deleted user whose last passes failed would otherwise
    hold `/health` at `degraded` until the next restart.
    """
    keep = set(active)
    for scope in [s for s in _scopes if s is not None and s not in keep]:
        del _scopes[scope]


def mark_task_stopped() -> None:
    """The indexer task ended by exception or returned while serving."""
    global _task_running
    _task_running = False


def mark_disabled() -> None:
    """Indexing is intentionally not run (`MCP_SANDBOX_MODE`)."""
    global _disabled, _task_running
    _disabled = True
    _task_running = False


def set_quarantine_count_provider(provider: Callable[[], int] | None) -> None:
    """Install the callable that returns the number of quarantined notes."""
    global _quarantine_count_provider
    _quarantine_count_provider = provider


def _quarantined_notes() -> int:
    if _quarantine_count_provider is None:
        return 0
    try:
        return max(0, int(_quarantine_count_provider()))
    except Exception as e:  # noqa: BLE001 - /health must never raise
        logger.error("Quarantine count unavailable for /health: %s", type(e).__name__)
        return 0


def snapshot() -> dict:
    """The `/health` `indexer` object. Counts only — nothing identifying."""
    threshold = _threshold()
    states = list(_scopes.values())
    # Distinct scopes with any index-side counter at the threshold (#309,
    # D4): a scope failing its index *and* its walk is one failing scope.
    failing = sum(
        1
        for s in states
        if max(
            s.index_consecutive_failures,
            s.rederive_incomplete,
            s.walk_incomplete,
        )
        >= threshold
    )
    embed_failing = sum(
        1 for s in states if s.embed_consecutive_failures >= threshold
    )
    max_failures = max(
        [_enumeration_failures]
        + [
            max(
                s.index_consecutive_failures,
                s.embed_consecutive_failures,
                s.rederive_incomplete,
                s.walk_incomplete,
            )
            for s in states
        ]
    )
    quarantined = _quarantined_notes()
    stalled, since_progress = _index_stalled(_now())
    embed_stalled, since_embed_progress = _embed_stalled(_now())
    embed_stuck, stuck_seconds = _embed_stuck(_now())
    successes = [s.last_success_at for s in states if s.last_success_at is not None]
    last_success = max(successes).isoformat() if successes else None

    degraded = (
        failing > 0
        or embed_failing > 0
        or _enumeration_failures >= threshold
        or quarantined > 0
        or stalled
        or embed_stalled
        or embed_stuck > 0
        or (not _task_running and not _disabled)
    )
    if degraded:
        status = "degraded"
    elif _disabled:
        status = "disabled"
    else:
        status = "ok"
    return {
        "status": status,
        "task_running": _task_running,
        "failing_scopes": failing,
        "embedding_failing_scopes": embed_failing,
        "max_consecutive_failures": max_failures,
        "quarantined_notes": quarantined,
        "last_success_at": last_success,
        "index_stalled": stalled,
        "seconds_since_progress": since_progress,
        "embedding_stalled": embed_stalled,
        "seconds_since_embedding_progress": since_embed_progress,
        # #355, D4: process-wide and **non-volumetric**, because `/health` is
        # unauthenticated and a count would be tenant-derived (Codex r2 B1).
        # Exact per-owner counts live on the panel and in the owner-scoped
        # `index_transition:` line. Never part of `status`.
        "rechunk": _rechunk_state(states),
        "embedding_stuck_scopes": embed_stuck,
        "seconds_embedding_stuck": stuck_seconds,
    }


def reset() -> None:
    """Forget everything. Tests only."""
    global _enumeration_failures, _enumeration_alerted, _task_running, _disabled
    global _quarantine_count_provider, _last_progress_at, _stall_alerted
    global _process_started_at, _idle
    global _embed_progress_at, _embed_stall_alerted, _embed_idle
    _scopes.clear()
    _last_progress_at = None
    _stall_alerted = False
    _idle = False
    _embed_progress_at = None
    _embed_stall_alerted = False
    _embed_idle = False
    _process_started_at = _now()
    _enumeration_failures = 0
    _enumeration_alerted = False
    _task_running = True
    _disabled = False
    _quarantine_count_provider = None
