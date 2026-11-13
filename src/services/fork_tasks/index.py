"""Writing a note's `fork_note_tasks` rows: one implementation, two callers.

- **The index pass** (`indexer._update_tasks_for_changed`) replaces the rows of
  every changed or moved note inside its own transaction, under the
  generation lock it already holds, next to the note's links.
- **The task write tools** (`reindex_note_tasks_now`) replace the rows of the
  one note they just wrote, from the bytes they just published, so the next
  `list_tasks` sees the change without waiting for the next pass. The note's
  other derived data (row, keyword vector, links, embeddings) still waits for
  that pass — which re-derives the task rows too, from what is on disk then.

The tool path takes the same generation lock, with a bounded wait, so its
delete-and-insert never interleaves with a pass's. A pass that read the note
*before* the tool's write and commits *after* it writes the older rows back;
the note's stat changed with the write, so the next pass re-reads it and
corrects them, and a write based on the older rows is refused by its hash
check meanwhile. Declared in docs/architecture/tasks.md, not hidden.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import delete, insert, select, text, update

from src.models.db import NoteMetadata, ForkNoteTask
from src.services.index_state import INDEX_GENERATION_LOCK_KEY
from src.services.fork_tasks.scan import ScannedLine, scan_note

logger = logging.getLogger(__name__)

#: How long a task tool waits for an in-flight pass before giving up on the
#: immediate re-index (the write itself has already succeeded).
IMMEDIATE_INDEX_WAIT_SECONDS = 5.0
_INSERT_BATCH = 1000


def task_rows(note_id: int, lines: tuple[ScannedLine, ...] | list[ScannedLine]) -> list[dict]:
    return [
        {
            "note_id": note_id,
            "line_number": s.line_number,
            "kind": s.kind,
            "line": s.text,
            "reason": s.reason,
        }
        for s in lines
    ]


async def insert_task_rows(session, rows: list[dict]) -> None:
    for start in range(0, len(rows), _INSERT_BATCH):
        await session.execute(insert(ForkNoteTask).values(rows[start:start + _INSERT_BATCH]))


async def delete_task_rows(session, note_ids: list[int]) -> None:
    if note_ids:
        await session.execute(delete(ForkNoteTask).where(ForkNoteTask.note_id.in_(note_ids)))


async def _try_generation_lock(session, deadline: float) -> bool:
    while True:
        got = (await session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": INDEX_GENERATION_LOCK_KEY}
        )).scalar()
        if got:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.2)


async def reindex_note_tasks_now(user_id: int | None, rel_path: str, text_: str) -> tuple[bool, str]:
    """Replace one note's task rows from `text_`, the text just published.

    Returns `(done, explanation)`. Never raises for an index-side reason: the
    write has already happened, so a failure here is reported to the caller
    as "the index catches up at the next pass", never as a failed write.
    """
    from src.database import async_session

    # Same normalisation as the pass's decode point (`read_note_at`): the
    # rows describe universal-newline, NUL-free text.
    normalised = text_.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    try:
        lines = await asyncio.to_thread(lambda: scan_note(normalised).lines)
        async with async_session() as session:
            async with session.begin():
                if not await _try_generation_lock(session, time.monotonic() + IMMEDIATE_INDEX_WAIT_SECONDS):
                    return False, "an index pass is running; the task index catches up when it ends"
                owner = NoteMetadata.user_id.is_(None) if user_id is None else NoteMetadata.user_id == user_id
                note_id = (await session.execute(
                    select(NoteMetadata.id).where(owner, NoteMetadata.file_path == rel_path).with_for_update()
                )).scalar()
                if note_id is None:
                    return False, "the note is not in the index yet; its tasks appear after the next index pass"
                await delete_task_rows(session, [note_id])
                await insert_task_rows(session, task_rows(note_id, lines))
                # The rows changed without the path, hash or extraction version
                # moving, so a re-derive must not take this row as current
                # (#311's rule for every such writer).
                await session.execute(
                    update(NoteMetadata).where(NoteMetadata.id == note_id).values(derived_under=None)
                )
        return True, "task index updated"
    except Exception as exc:  # noqa: BLE001 — reported, never swallowed silently
        from src.services.indexer import _loggable_path

        # Class only: a driver message can quote note content.
        logger.error("Immediate task re-index of %s failed: %s", _loggable_path(rel_path), type(exc).__name__)
        return False, f"the task index could not be updated now ({type(exc).__name__}); it catches up at the next index pass"
