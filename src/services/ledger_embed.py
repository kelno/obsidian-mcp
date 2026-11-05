"""Ledger vectors: the background embedder, the indexer sweep, and the meaning search.

**A write never waits for an embedding.** The reference deployment's Ollama
is CPU-only and slow, and the claude.ai app drops a tool call after about
30 s. So `ledger_add` and `ledger_update` commit the entry, then hand its id
to `enqueue`, and one background task embeds the queue one entry at a time.
Until its vector lands, an entry is found by words only: the meaning search
reads a vector only while `embedded_hash = content_hash`, so an outdated
vector is never used.

The indexer's embed stage also **sweeps** entries whose vector is missing or
outdated (`sweep`), inside the same `EmbedBudget` as the notes, after them:
that is what catches an entry the queue dropped, a restart, a provider
outage, and `make reset-embeddings`.

Both paths certify the same way the notes do (`embeddings._generation_matches`
is the model): the provider call runs with no lock and no transaction open;
then, in one transaction, the generation lock, the stored fingerprint
compared, a conditional `UPDATE … WHERE content_hash = <hash embedded>` (zero
rows: the entry changed meanwhile, discard), and only then the vectors
replaced. A configuration change or a reset between the provider call and the
write therefore writes nothing.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import async_session
from src.models.db import LedgerEmbedding, LedgerEntry
from src.services import ledger
from src.services.embeddings import NoteEmbedOutcome, chunk_text, get_embedding, get_embeddings_batch
from src.services.index_state import (
    KEY_EMBEDDING_FINGERPRINT,
    FingerprintStatus,
    acquire_generation_lock,
    compare_fingerprint,
    embedding_fingerprint,
    get_state,
    state_table_exists,
)

logger = logging.getLogger(__name__)

#: Entries one sweep looks at per stage, at most; the budget usually stops it first.
SWEEP_BATCH = 200


# ── Embedding one entry ──────────────────────────────────────────────────────

class ProviderFailed(RuntimeError):
    """The provider call for one entry failed; carries how many chunks it sent."""

    def __init__(self, chunks_submitted: int, cause: BaseException):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.chunks_submitted = chunks_submitted
        #: The provider exception's class name, as `EmbedNoteFailure.exc_type`
        #: carries it: an input-too-large refusal is not evidence of an outage.
        self.exc_type = type(cause).__name__


async def _generation_ok(session: AsyncSession) -> bool:
    """Generation lock taken and the stored fingerprint matches (or is absent)."""
    if not await state_table_exists(session):
        return True
    await acquire_generation_lock(session)
    verdict = compare_fingerprint(await get_state(session, KEY_EMBEDDING_FINGERPRINT), embedding_fingerprint())
    if verdict.status in (FingerprintStatus.MATCH, FingerprintStatus.ABSENT):
        return True
    logger.error(
        "Not storing ledger vectors: the embedding configuration changed under this process "
        "(stored %s, this process %s). A process running the stored configuration will embed them.",
        verdict.stored, embedding_fingerprint(),
    )
    return False


@dataclass
class EntryEmbedResult:
    chunks_submitted: int = 0
    embedded: bool = False
    skipped: str | None = None


async def embed_entry(entry_id: int, sessions=None) -> EntryEmbedResult:
    """Embed one entry's current text and store it, or write nothing.

    Raises what the provider raised (the caller counts it); returns
    `skipped` when there was nothing to do or the entry moved meanwhile.
    `sessions` is the session factory (the indexer passes its own).
    """
    sessions = sessions or async_session
    async with sessions() as session:
        row = (await session.execute(
            select(LedgerEntry).where(LedgerEntry.id == entry_id))).scalar_one_or_none()
        if row is None:
            return EntryEmbedResult(skipped="gone")
        if row.embedded_hash == row.content_hash:
            return EntryEmbedResult(skipped="current")
        text_ = ledger.embed_text(row)
        expected = row_hash = row.content_hash
        await session.rollback()
    if ledger.content_hash(text_) != expected:
        # A row written by something other than the tools (a hand edit in
        # psql) whose hash does not describe its text: re-stamp it from the
        # text, so the vector and the hash agree.
        expected = ledger.content_hash(text_)
        async with sessions() as session:
            # Conditional, so a tool update committed meanwhile is not overwritten.
            await session.execute(update(LedgerEntry).where(
                LedgerEntry.id == entry_id, LedgerEntry.content_hash == row_hash).values(content_hash=expected))
            await session.commit()
    chunks = chunk_text(text_, chunk_size=settings.chunk_size, overlap=settings.chunk_overlap) or [text_]
    result = EntryEmbedResult(chunks_submitted=len(chunks))
    try:
        vectors = await get_embeddings_batch(chunks)
    except Exception as exc:  # noqa: BLE001 - re-raised with the chunk count the budget needs
        raise ProviderFailed(len(chunks), exc) from exc
    if len(vectors) != len(chunks):
        raise ProviderFailed(len(chunks), RuntimeError(
            f"the embedding provider returned {len(vectors)} vectors for {len(chunks)} chunks"))
    async with sessions() as session:
        if not await _generation_ok(session):
            await session.rollback()
            result.skipped = "configuration changed"
            return result
        stamped = await session.execute(
            update(LedgerEntry).where(LedgerEntry.id == entry_id, LedgerEntry.content_hash == expected)
            .values(embedded_hash=expected).execution_options(synchronize_session=False)
        )
        if stamped.rowcount != 1:
            await session.rollback()
            result.skipped = "changed meanwhile"
            return result
        await session.execute(delete(LedgerEmbedding).where(LedgerEmbedding.entry_id == entry_id))
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            session.add(LedgerEmbedding(entry_id=entry_id, chunk_index=index, chunk_text=chunk, embedding=vector))
        await session.commit()
    result.embedded = True
    return result


# ── The background queue ─────────────────────────────────────────────────────

_queue: collections.deque[int] = collections.deque()
_queued: set[int] = set()
_in_flight: set[int] = set()
_worker: asyncio.Task | None = None


def enqueue(entry_id: int) -> bool:
    """Ask for `entry_id` to be embedded soon. Never waits, never raises.

    False when the queue is full: the entry is then left to the indexer's
    sweep, and the log says so.
    """
    global _worker
    if entry_id in _queued or entry_id in _in_flight:
        return True
    if len(_queue) >= int(settings.ledger_embed_queue_max):
        logger.info("Ledger embed queue full (%d); entry id %d is left to the indexer's sweep.",
                    len(_queue), entry_id)
        return False
    _queue.append(entry_id)
    _queued.add(entry_id)
    if _worker is None or _worker.done():
        try:
            _worker = asyncio.get_running_loop().create_task(_drain(), name="ledger-embed")
        except RuntimeError:
            # No running loop (a script): the sweep will embed it.
            _queue.pop()
            _queued.discard(entry_id)
            return False
    return True


def _paused() -> bool:
    from src.services import indexer

    return indexer._is_paused()


async def _drain() -> None:
    while _queue:
        entry_id = _queue.popleft()
        _queued.discard(entry_id)
        if _paused():
            logger.info("Indexing is paused; ledger entry id %d is left to the indexer's sweep.", entry_id)
            continue
        _in_flight.add(entry_id)
        try:
            result = await embed_entry(entry_id)
            if result.skipped and result.skipped not in ("current", "gone"):
                logger.info("Ledger entry id %d not embedded: %s.", entry_id, result.skipped)
        except Exception as exc:  # noqa: BLE001 - logged; the sweep retries it
            logger.warning("Embedding ledger entry id %d failed (%s: %s); the indexer's sweep will retry it.",
                           entry_id, type(exc).__name__, exc)
        finally:
            _in_flight.discard(entry_id)


def queue_state() -> dict:
    """For tests and diagnostics."""
    return {"queued": len(_queue), "in_flight": len(_in_flight),
            "running": _worker is not None and not _worker.done()}


async def wait_idle(timeout: float = 30.0) -> None:
    """Tests: wait for the background queue to drain."""
    if _worker is not None and not _worker.done():
        await asyncio.wait_for(asyncio.shield(_worker), timeout)


# ── The indexer sweep ────────────────────────────────────────────────────────

#: Per scope, the `(entry id, content_hash)` pairs whose provider call failed
#: in an earlier sweep. Tried last, as the notes' `_embed_failed_notes` are:
#: otherwise a few entries the provider always rejects, being the newest,
#: would trip the provider stop at the head of every sweep and nothing behind
#: them would ever be embedded. In-process, pruned to the rows still selected.
_failed_entries: dict[int | None, set[tuple[int, str]]] = {}

async def sweep(user_id: int | None, budget, outcome, log_suffix: str, is_paused, sessions=None) -> int:
    """Embed this scope's entries whose vector is missing or outdated, within `budget`.

    Runs at the end of the scope's embed stage, after the notes, under the
    same `EmbedBudget` (chunks, time, provider-failure stop) and feeding the
    same `EmbedPassResult` failures, so the ledger can neither hold the index
    stage back nor hide an outage. Returns how many entries it embedded.
    `sessions` is the indexer's session factory, so the sweep reads and
    writes the database the stage does.
    """
    if budget.stopped is not None:
        return 0
    sessions = sessions or async_session
    async with sessions() as session:
        rows = (await session.execute(
            select(LedgerEntry.id, LedgerEntry.ref, LedgerEntry.content_hash)
            .where(LedgerEntry.owner_key == ledger.owner_key(user_id),
                   LedgerEntry.embedded_hash.is_distinct_from(LedgerEntry.content_hash))
            .order_by(LedgerEntry.updated_at.desc()).limit(SWEEP_BATCH)
        )).all()
        await session.rollback()
    if not rows:
        _failed_entries.pop(user_id, None)
        return 0
    failed = _failed_entries.setdefault(user_id, set())
    failed &= {(r.id, r.content_hash) for r in rows}
    rows = ([r for r in rows if (r.id, r.content_hash) not in failed]
            + [r for r in rows if (r.id, r.content_hash) in failed])
    embedded = 0
    started = time.monotonic()
    for row in rows:
        if is_paused():
            break
        if budget.exhausted():
            budget.stop(log_suffix, "ledger sweep")
            break
        if budget.provider_unavailable():
            budget.stop_for_provider(log_suffix, "ledger sweep", len(rows) - embedded)
            break
        if row.id in _in_flight:
            continue
        _in_flight.add(row.id)
        try:
            result = await embed_entry(row.id, sessions)
            if result.chunks_submitted:
                outcome.record_attempt()
                budget.debit(result.chunks_submitted)
                budget.record_provider_result(SimpleNamespace(
                    chunks_submitted=result.chunks_submitted, outcome=NoteEmbedOutcome.EMBEDDED, failure=None))
            embedded += int(result.embedded)
            failed.discard((row.id, row.content_hash))
        except ProviderFailed as exc:
            failed.add((row.id, row.content_hash))
            outcome.record_attempt()
            budget.debit(exc.chunks_submitted)
            budget.record_provider_result(SimpleNamespace(
                chunks_submitted=exc.chunks_submitted, outcome=NoteEmbedOutcome.PROVIDER_FAILED,
                failure=SimpleNamespace(exc_type=exc.exc_type)))
            outcome.record_failure(RuntimeError(f"ledger entry {row.ref}: {exc}"))
            logger.warning("Embedding ledger entry %s failed%s: %s", row.ref, log_suffix, exc)
        except Exception as exc:  # noqa: BLE001 - a database fault: counted in the run row, like a note's
            outcome.record_failure(RuntimeError(f"ledger entry {row.ref}: {type(exc).__name__}: {exc}"))
            logger.warning("Embedding ledger entry %s failed%s: %s: %s", row.ref, log_suffix, type(exc).__name__, exc)
        finally:
            _in_flight.discard(row.id)
            budget.note_finished()
    if embedded:
        logger.info("Ledger sweep%s: %d of %d entries embedded in %.0fs.", log_suffix, embedded, len(rows),
                    time.monotonic() - started)
    return embedded


# ── Meaning search ───────────────────────────────────────────────────────────

@dataclass
class MeaningResult:
    hits: list[dict]
    not_embedded: int
    error: str | None = None


async def meaning_search(session: AsyncSession, uid: int | None, query: str, filters: "ledger.Filters",
                         limit: int) -> MeaningResult:
    """Entries closest in meaning to `query`, best chunk per entry, exact distance.

    Exact rather than through a vector index (the ledger is small, and an
    exact scan cannot miss a candidate a filter would have discarded). Only
    current vectors are read; `not_embedded` counts the filtered entries
    that have none yet, which the words list still covers.
    """
    clauses = filters.where(uid)
    not_embedded = (await session.execute(
        select(func.count()).select_from(LedgerEntry)
        .where(*clauses, LedgerEntry.embedded_hash.is_distinct_from(LedgerEntry.content_hash))
    )).scalar_one()
    try:
        vector = await asyncio.wait_for(get_embedding(query), timeout=float(settings.ledger_query_embed_timeout_seconds))
    except asyncio.TimeoutError:
        return MeaningResult([], not_embedded, error=(
            f"the embedding provider did not answer within {settings.ledger_query_embed_timeout_seconds:g} s, "
            "so there is no meaning list this time; the words list is complete."))
    except Exception as exc:  # noqa: BLE001 - reported to the caller, words still answer
        return MeaningResult([], not_embedded, error=(
            f"the embedding provider is unavailable ({type(exc).__name__}), so there is no meaning list this "
            "time; the words list is complete."))
    # Best chunk per entry, ranked in SQL: an over-fetch of chunks deduped in
    # Python let one long entry's many close chunks fill every slot.
    distance = LedgerEmbedding.embedding.cosine_distance(vector)
    best = (
        select(LedgerEmbedding.entry_id, func.min(distance).label("distance"))
        .group_by(LedgerEmbedding.entry_id).subquery()
    )
    rows = (await session.execute(
        select(LedgerEntry.id, LedgerEntry.ref, LedgerEntry.kind, LedgerEntry.status, LedgerEntry.area_tag,
               LedgerEntry.title, LedgerEntry.updated_at, LedgerEntry.observed_on, LedgerEntry.origin,
               LedgerEntry.seen, LedgerEntry.revision, best.c.distance)
        .join(best, best.c.entry_id == LedgerEntry.id)
        .where(*clauses, LedgerEntry.embedded_hash == LedgerEntry.content_hash)
        .order_by(best.c.distance, LedgerEntry.id).limit(limit)
    )).all()
    hits = [{"row": row, "similarity": round(1.0 - float(row.distance), 4)} for row in rows]
    return MeaningResult(hits, not_embedded)


# ── Reset (make reset-embeddings, the panel's reset) ─────────────────────────

async def reset_vectors(session: AsyncSession, dim: int) -> None:
    """Drop every ledger vector, retype the column, mark every entry unembedded.

    Called by both reset paths inside their own locked transaction, beside the
    notes' reset, so the ledger's vectors are never left in the previous
    model's space under the new fingerprint. The sweep rebuilds them.
    """
    await session.execute(delete(LedgerEmbedding))
    await session.execute(text(f"ALTER TABLE ledger_embeddings ALTER COLUMN embedding TYPE vector({int(dim)})"))
    await session.execute(update(LedgerEntry).values(embedded_hash=None).execution_options(synchronize_session=False))
