"""How far indexing and embedding have got, counted from the database.

`index_status` (the MCP tool) and the panel's dashboard read their figures
here, so "pending" means one thing everywhere: a note whose stored vectors are
not its current content (`vectors_not_current`).

Every count is scoped to one owner by the same total mapping as
`apply_note_filters`: `None` is the NULL-owned slice (single-user mode), an
int is that user's rows. Counts only, never paths.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.db import IndexerRun, LedgerEntry, NoteEmbedding, NoteMetadata
from src.services import ledger, private_folders
from src.services.filters import apply_note_filters

#: How far back the embedding rate is measured. An hour smooths over the
#: embed stage's rhythm (a budget-bounded stage every few minutes) while still
#: reflecting the provider's current speed.
RATE_WINDOW = timedelta(hours=1)


def vectors_not_current():
    """The one expression for "this note's stored vectors are not current".

    ``embedded_content_hash IS NULL OR embedded_content_hash IS DISTINCT FROM
    content_hash OR embedded_chunking_version IS DISTINCT FROM
    CHUNKING_VERSION`` — a note never embedded, a note whose certification was
    invalidated (a move, a grammar bump), a note edited since it was last
    embedded, or a note embedded under another chunking version (#355). The
    expression itself is `embeddings.vectors_not_current_expr`, beside
    `CHUNKING_VERSION`, so the embed backlog's SQL and this count are one
    definition.

    Its callers scope it differently on purpose: the dashboard and
    `index_status` by owner, the panel's reset poller (admin-only) not at all.
    The *definition of pending* must not differ between them, which is why it
    is written once (design D8 of #201).
    """
    from src.services.embeddings import vectors_not_current_expr

    return vectors_not_current_expr()


def embedding_excluded(file_path: str, patterns: list[str] | None = None) -> bool:
    """Whether `EMBEDDING_EXCLUDE_PATTERNS` (or `patterns`) keeps this note out of embedding.

    The embed stage's own test: `fnmatch` over the vault-relative path.
    """
    if patterns is None:
        patterns = settings.embedding_exclude_patterns or []
    return any(fnmatch.fnmatch(file_path, pat) for pat in patterns)


@dataclass(frozen=True)
class NoteCounts:
    """The embedding state of a set of notes. The four parts add up to `notes`."""

    #: Notes in the index.
    notes: int
    #: Embedded, and the vectors are of the note as it stands.
    embedded: int
    #: Waiting for the embed stage: never embedded, or changed since.
    pending: int
    #: Processed, kept out of embedding by `EMBEDDING_EXCLUDE_PATTERNS`: will never have vectors.
    excluded: int
    #: Processed, no text to embed (an empty note, say): will never have vectors.
    no_text: int
    #: Among `embedded`: only the note's beginning was embedded (chunk cap).
    beginning_only: int


@dataclass(frozen=True)
class LedgerCounts:
    entries: int
    #: Entries whose vectors are missing or outdated.
    pending: int


@dataclass(frozen=True)
class EmbedActivity:
    #: Notes embedded by stages that finished within `RATE_WINDOW`.
    embedded_last_hour: int
    #: When the last stage that embedded at least one note finished; None if none is on record.
    last_embedded_at: datetime | None


def _owned(stmt, user_id: int | None):
    return stmt.where(NoteMetadata.user_id.is_(None) if user_id is None else NoteMetadata.user_id == user_id)


async def note_counts(
    session: AsyncSession, user_id: int | None, *, folder: str | None = None, private_only: bool = False
) -> NoteCounts:
    """Count `user_id`'s notes by embedding state, under `folder` or in private folders only.

    `private_only` with no private folders configured counts nothing.
    """
    private = "only" if private_only else None
    if private_only and not private_folders.enabled():
        return NoteCounts(0, 0, 0, 0, 0, 0)

    def scoped(stmt):
        return apply_note_filters(stmt, folder=folder, user_id=user_id, private=private)

    has_vectors = exists().where(NoteEmbedding.note_id == NoteMetadata.id)
    pending_flag = vectors_not_current()
    row = (await session.execute(scoped(select(
        func.count(NoteMetadata.id),
        func.count(NoteMetadata.id).filter(pending_flag),
        func.count(NoteMetadata.id).filter(~pending_flag, has_vectors),
        func.count(NoteMetadata.id).filter(~pending_flag, has_vectors, NoteMetadata.chunks_truncated.is_(True)),
    )))).one()
    total, pending, embedded, beginning_only = (int(v or 0) for v in row)
    # Current with no vectors: excluded or empty. The pattern test is Python's
    # fnmatch, as in the embed stage, so the paths are read and matched here.
    without = (await session.execute(scoped(
        select(NoteMetadata.file_path).where(~pending_flag, ~has_vectors)
    ))).scalars().all()
    excluded = sum(1 for path in without if embedding_excluded(path))
    return NoteCounts(
        notes=total, embedded=embedded, pending=pending, excluded=excluded,
        no_text=len(without) - excluded, beginning_only=beginning_only,
    )


async def ledger_counts(session: AsyncSession, user_id: int | None) -> LedgerCounts:
    """Count `user_id`'s agent-ledger entries, and those whose vectors are missing or outdated."""
    row = (await session.execute(
        select(
            func.count(LedgerEntry.id),
            func.count(LedgerEntry.id).filter(LedgerEntry.embedded_hash.is_distinct_from(LedgerEntry.content_hash)),
        ).where(LedgerEntry.owner_key == ledger.owner_key(user_id))
    )).one()
    return LedgerCounts(entries=int(row[0] or 0), pending=int(row[1] or 0))


async def embed_activity(session: AsyncSession, user_id: int | None, now: datetime | None = None) -> EmbedActivity:
    """Recent embedding work for `user_id`, from the pass history (`indexer_runs`).

    A stage's notes count in the window when the stage *finished* in it, so a
    long stage that started earlier counts whole: a rough rate, not a meter.
    """
    now = now or datetime.now(timezone.utc)
    owner = IndexerRun.user_id.is_(None) if user_id is None else IndexerRun.user_id == user_id
    row = (await session.execute(
        select(
            func.coalesce(func.sum(IndexerRun.notes_embedded).filter(IndexerRun.finished_at >= now - RATE_WINDOW), 0),
            func.max(IndexerRun.finished_at).filter(IndexerRun.notes_embedded > 0),
        ).where(owner)
    )).one()
    return EmbedActivity(embedded_last_hour=int(row[0]), last_embedded_at=row[1])


def minutes_left(pending: int, per_hour: int) -> int | None:
    """At `per_hour`, how many minutes `pending` notes take; None when nothing is moving."""
    if pending <= 0:
        return 0
    if per_hour <= 0:
        return None
    return max(1, round(pending * 60 / per_hour))
