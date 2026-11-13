"""`index_status`'s structured result.

`summary` first, in a sentence an agent can repeat to the owner; then the
counts behind it, the embedding progress, the indexer's health, and `notes`
saying what needs attention. Counts only: no note paths.
"""

from __future__ import annotations

from pydantic import BaseModel, PrivateAttr

from src.mcp_server.read_result import _OmitNone
from src.services.tool_outcomes import BodyOutcome


class NoteCounts(BaseModel):
    #: Notes in the index. The four states below add up to it.
    notes: int
    #: Embedded, and the vectors are of the note as it stands now.
    embedded: int
    #: Waiting to be embedded: never embedded, or changed since.
    pending: int
    #: Kept out of search by meaning by the server's settings: will never be embedded.
    excluded: int
    #: No text to embed (an empty note, say): will never be embedded.
    no_text: int
    #: Among `embedded`: only the note's beginning was embedded (it was too long).
    beginning_only: int


class LedgerCounts(BaseModel):
    #: Agent-ledger entries.
    entries: int
    #: Entries not yet embedded, or changed since (embedded after the notes).
    pending: int


class Progress(BaseModel):
    #: Notes embedded in the last hour.
    embedded_last_hour: int
    #: Minutes until all of the vault's pending notes are embedded at the last hour's
    #: rate (a rough guess); 0 when none is pending; absent when nothing was embedded in the last hour.
    estimated_minutes_left: int | None = None
    #: When the last embedding stage that embedded a note finished (UTC); absent if none is on record.
    last_embedded_at: str | None = None
    #: An embedding stage (or a reset from the control panel) is running right now.
    embedding_now: bool
    #: Indexing is paused from the control panel: nothing gets embedded until it is resumed.
    paused: bool
    #: The embedding stage runs every this many seconds (sooner while a backlog remains).
    embed_interval_seconds: int


class Health(BaseModel):
    #: ok, degraded or disabled: the server's `/health` verdict on the indexer, server-wide.
    status: str
    #: The indexer's background task is alive.
    task_running: bool
    #: The embedding server failed repeatedly for some vault on this server.
    embedding_failing: bool
    #: No embedding stage has finished for a long time.
    embedding_stalled: bool
    #: No indexing stage has finished for a long time (new and edited notes are not picked up).
    index_stalled: bool
    #: Notes set aside because the database refused them; absent from the index until they change.
    quarantined_notes: int


class IndexStatusResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    error: str | None = None
    #: The state in a sentence or two.
    summary: str | None = None
    #: The whole vault.
    vault: NoteCounts | None = None
    #: The folder counted, as a vault path ending in `/`.
    folder: str | None = None
    #: The notes under `folder`.
    folder_counts: NoteCounts | None = None
    #: The part of `vault` in private folders, counted only (no paths); present when the server has private folders and no `folder` is given.
    private_folders: NoteCounts | None = None
    ledger: LedgerCounts | None = None
    progress: Progress | None = None
    health: Health | None = None
    #: What needs attention, if anything.
    notes: list[str] | None = None
    #: The server's time when it counted (UTC): compare `progress.last_embedded_at` with it.
    checked_at: str | None = None
