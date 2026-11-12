from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.db import NoteMetadata
from src.services import private_folders, timing
from src.services.filters import apply_note_filters
from src.services.fts import combined_tsquery


async def full_text_search(
    session: AsyncSession,
    query: str,
    folder: str | None = None,
    limit: int = 20,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
    hide_private: bool = False,
    counts: dict | None = None,
) -> list[dict]:
    """Full-text search over notes_metadata using tsvector.

    The tsquery is built from `settings.fts_configs` (see `src/services/fts.py`)
    so query-time configs match the index-time configs in `indexer.py`.

    `hide_private` (with private folders configured) leaves notes under a
    private folder out inside the query, so the limit is still filled, and
    sets `counts["private_hidden"]` to the number of private matches that
    would have been among these results: every one when the results did not
    fill the limit, else those ranked ahead of the last result shown; at
    most `limit`, as for the vector searches.
    """
    limit = max(1, min(limit, 500))
    tsquery = combined_tsquery(query)
    rank = func.ts_rank_cd(NoteMetadata.content_tsvector, tsquery).label("rank")

    # The same transaction-scoped hint the vector path uses, for the same
    # reason: the planner's cost model does not see detoast I/O. It costs the
    # `notes_metadata` heap at `relpages` (~481) and happily picks a Seq Scan,
    # which then detoasts every one of ~3,800 tsvectors out of a 36 MB TOAST
    # table — 13,086 buffers per query. Lowering `random_page_cost` flips it to
    # a GIN bitmap scan: 1,146 buffers, 6–9 ms warm instead of 26–37 ms.
    # Lifetime counters before this: 5 index scans vs 3,655 sequential ones.
    # SET LOCAL scopes to this transaction only — the database is shared with
    # other tenants, so no global setting is touched.
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))

    # Explicit projection (#280, design D6): the four columns the result
    # renders. `ts_rank_cd` and `@@` still read the tsvector server-side; what
    # is gone is shipping it (and `frontmatter`) to Python and hydrating an
    # entity per row. `content_tsvector` is deferred with raiseload on the
    # model, so a whole-entity select here would no longer carry it anyway.
    stmt = (
        select(
            NoteMetadata.file_path,
            NoteMetadata.title,
            NoteMetadata.tags,
            rank,
        )
        .where(NoteMetadata.content_tsvector.op("@@")(tsquery))
    )
    private = "exclude" if hide_private and private_folders.enabled() else None
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id, private=private
    )
    # `file_path` breaks rank ties deterministically. `ts_rank_cd` produces
    # plenty of them, and which tied row survived the LIMIT otherwise depended
    # on the plan — so the hint above would have changed *results*, not just
    # speed. With the tie-break, membership and order are stable across plans.
    stmt = stmt.order_by(rank.desc(), NoteMetadata.file_path.asc()).limit(limit)

    result = await session.execute(stmt)
    rows = result.all()
    if private is not None and counts is not None:
        hidden = (
            select(func.count())
            .select_from(NoteMetadata)
            .where(NoteMetadata.content_tsvector.op("@@")(tsquery))
        )
        hidden = apply_note_filters(
            hidden, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id, private="only"
        )
        if len(rows) >= limit:
            # Ahead of the last result in the result's own order: rank, then path.
            last = rows[-1]
            ahead = func.ts_rank_cd(NoteMetadata.content_tsvector, tsquery)
            hidden = hidden.where(or_(
                ahead > last.rank, and_(ahead == last.rank, NoteMetadata.file_path < last.file_path)
            ))
        counts["private_hidden"] = min(int((await session.execute(hidden)).scalar_one()), limit)
    results = [
        {
            "path": row.file_path,
            "title": row.title,
            "tags": row.tags,
            "rank": float(row.rank),
        }
        for row in rows
    ]
    # Result telemetry, recorded where the result set is final (#161). The
    # bounds live in `timing.record_results`, which is the record site the
    # `_tracked` merge cannot truncate — see `src/services/timing.py`. Outside
    # a tracked tool call (the panel, the benchmarks) there is no holder and
    # this is a no-op.
    timing.record_results(r["path"] for r in results)
    return results
