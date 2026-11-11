"""Links between agent-ledger entries and the vault tasks that cite them.

A vault task cites an entry in its text as `[F12]`, `[Q14]` or `[P35, P48]`
(the form and its matching rules: `ledger_citations`). That citation is the
only link anyone writes: the server reads it back from the task index
(`note_tasks`, migration 031) whenever a ledger or task tool needs it, so it
never goes stale against a second, hand-written copy. Nothing is stored and
no foreign key points into the index tables: the ledger stays primary data
that no reindex can reach.

- Any kind can be cited. For feedback, the task is what the owner decided to
  do: an entry is **decided** when a task that is not cancelled cites it. For
  a quirk or a proposal, an open citing task means something is pending on
  it (often a decision postponed until some event).
- Task state, over the tasks citing an entry: `open` (at least one is not
  closed), `done` (all closed, at least one done) or `cancelled` (all
  cancelled: for feedback, the decision was dropped and the entry is
  undecided again).
- A bare `F12` in a task is not a citation; the task tools point it out when
  it names an existing entry.

The index lags behind hand edits until the next index pass; the task tools
re-index their note at once. See docs/architecture/agent-ledger.md, "Task
links".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.db import LedgerEntry, NoteMetadata, NoteTask
from src.services import ledger_citations as citations
from src.services.ledger_citations import Ref

#: Kind of each ref letter.
_KIND_OF = {"Q": "quirk", "F": "feedback", "P": "proposal"}

#: Status types that close a task. NON_TASK lines are not tasks and are ignored.
_CLOSED = ("DONE", "CANCELLED")
_IGNORED = ("NON_TASK",)


@dataclass(frozen=True)
class CitingTask:
    ref: str          # "path:line", 1-based
    status_type: str  # TODO, IN_PROGRESS, ON_HOLD, DONE, CANCELLED
    text: str         # the line as indexed

    def as_dict(self) -> dict:
        return {"ref": self.ref, "status": self.status_type, "text": self.text}


def task_state(status_types: Iterable[str]) -> str | None:
    """`open`, `done`, `cancelled`, or None when no task cites the entry."""
    types = [t for t in status_types if t not in _IGNORED]
    if not types:
        return None
    if any(t not in _CLOSED for t in types):
        return "open"
    return "done" if "DONE" in types else "cancelled"


async def citing_rows(session: AsyncSession, uid: int | None) -> list:
    """The caller's indexed task lines that may cite a ledger entry."""
    owner = NoteMetadata.user_id.is_(None) if uid is None else NoteMetadata.user_id == uid
    return (await session.execute(
        select(NoteMetadata.file_path, NoteTask.line_number, NoteTask.line)
        .join(NoteMetadata, NoteMetadata.id == NoteTask.note_id)
        .where(owner, NoteTask.kind == "task", NoteTask.line.op("~")(citations.SQL_BRACKET_PREFILTER))
        .order_by(NoteMetadata.file_path, NoteTask.line_number)
    )).all()


def link(rows, parse: Callable[[str, str], object]) -> dict[str, list[CitingTask]]:
    """`{ref such as "F12": [tasks citing it]}` from `citing_rows`.

    `parse(line, path)` returns the plugin's reading of the line (a task with
    a `status.type`) or None when the plugin would not read it as a task.
    """
    out: dict[str, list[CitingTask]] = {}
    for row in rows:
        refs = citations.bracketed(row.line)
        if not refs:
            continue
        task = parse(row.line, row.file_path)
        if task is None:
            continue
        item = CitingTask(f"{row.file_path}:{row.line_number}", task.status.type, row.line)
        for ref in refs:
            out.setdefault(str(ref), []).append(item)
    return out


def states(links: dict[str, list[CitingTask]]) -> dict[str, str]:
    """`{ref: task state}` for every cited entry."""
    out = {}
    for ref, tasks in links.items():
        state = task_state(t.status_type for t in tasks)
        if state is not None:
            out[ref] = state
    return out


def canonical(ref: str) -> str:
    """`Q14` for a stored ref such as `Q014` (imported refs keep their zeros)."""
    return f"{ref[0].upper()}{int(ref[1:])}"


async def entries_by_ref(session: AsyncSession, uid: int | None, refs: Iterable[Ref]) -> dict[Ref, LedgerEntry]:
    """The caller's entries for these refs, any kind."""
    refs = list(dict.fromkeys(refs))
    if not refs:
        return {}
    owner_key = 0 if uid is None else int(uid)
    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.owner_key == owner_key, or_(*[
            and_(LedgerEntry.kind == _KIND_OF[r.letter], LedgerEntry.number == r.number) for r in refs]))
    )).scalars().all()
    by_key = {(e.kind, e.number): e for e in rows}
    return {r: by_key[(_KIND_OF[r.letter], r.number)] for r in refs if (_KIND_OF[r.letter], r.number) in by_key}


def entry_state(entry: LedgerEntry, day: Callable[[object], str | None]) -> dict:
    """What a reader needs to check a citation against: ref, kind, title, status,
    a quirk's certainty, and the day it last changed."""
    out = {"ref": canonical(entry.ref), "kind": entry.kind, "title": entry.title, "status": entry.status}
    if entry.kind == "quirk":
        out["certainty"] = entry.certainty
    out["updated"] = day(entry.updated_at)
    return out


@dataclass(frozen=True)
class _Row:
    file_path: str
    line_number: int
    line: str


def rows_of_note(path: str, scanned_lines) -> list[_Row]:
    """`citing_rows`-shaped rows for one note's scan (`task_scan.scan_note(...).lines`)."""
    return [_Row(path, s.line_number, s.text) for s in scanned_lines if s.kind == "task"]


async def refs_on_line(session: AsyncSession, uid: int | None, text: str,
                       parse: Callable[[str, str], object], *, note_path: str,
                       note_rows: list, day: Callable[[object], str | None]) -> tuple[list[dict], list[str]]:
    """What a task tool reports about the ledger citations in the line it wrote.

    `([{ref, kind, title, status, certainty?, updated, tasks}], notes)`: each
    cited entry as it stands now, with the state of all the tasks citing it;
    a cited ref with no entry is reported as `unknown`; a bare `F12` naming an
    existing entry gets a note saying it links nothing.

    The note just written is read from `note_rows` (its text as published,
    scanned), never from the index: the immediate re-index does not run while
    an index pass holds the lock, nor for a note not yet indexed, and a state
    read from stale rows could say "done" beside the open task just created.
    """
    cited = citations.bracketed(text)
    loose = [r for r in citations.bare(text) if r not in cited]
    if not cited and not loose:
        return [], []
    entries = await entries_by_ref(session, uid, cited + loose)
    task_states = {}
    if cited:
        rows = [r for r in await citing_rows(session, uid) if r.file_path != note_path] + list(note_rows)
        task_states = states(link(rows, parse))
    refs = []
    for ref in cited:
        entry = entries.get(ref)
        if entry is None:
            refs.append({"ref": str(ref), "status": "unknown"})
            continue
        refs.append({**entry_state(entry, day), "tasks": task_states.get(str(ref))})
    notes = [
        f"The line mentions {ref} without brackets, so it is not linked to ledger entry {ref} "
        f"(\"{entries[ref].title}\"). To link it, write [{ref}]."
        for ref in loose if ref in entries
    ]
    return refs, notes


# ── What a ledger write cites, and what cites the entry it changed ───────────

async def cited_states(session: AsyncSession, uid: int | None, refs: Iterable[Ref], *, own_ref: str | None,
                       day: Callable[[object], str | None]) -> list[dict]:
    """Each entry in `refs` as it stands now (`entry_state`), `status: unknown` when
    there is none; the entry being written (`own_ref`) is left out."""
    own = canonical(own_ref) if own_ref else None
    refs = [r for r in dict.fromkeys(refs) if str(r) != own]
    entries = await entries_by_ref(session, uid, refs)
    return [entry_state(entries[r], day) if r in entries else {"ref": str(r), "status": "unknown"} for r in refs]


async def entries_citing(session: AsyncSession, uid: int | None, entry: LedgerEntry, text_columns,
                         text_of: Callable[[LedgerEntry], str], *, limit: int,
                         day: Callable[[object], str | None]) -> tuple[list[dict], int]:
    """`(entries, total)`: the caller's other entries whose text cites `entry`, bare or bracketed.

    PostgreSQL hands over the entries whose text may name the ref (a POSIX
    regex, a superset of the match), then Python matches exactly.
    """
    from sqlalchemy import func

    ref = Ref(canonical(entry.ref)[0], entry.number)
    owner_key = 0 if uid is None else int(uid)
    rows = (await session.execute(
        select(LedgerEntry).where(
            LedgerEntry.owner_key == owner_key, LedgerEntry.id != entry.id,
            func.concat_ws(" ", *text_columns).op("~")(citations.sql_ref_pattern(ref)))
        .order_by(LedgerEntry.updated_at.desc(), LedgerEntry.id.desc())
    )).scalars().all()
    citing = [e for e in rows if ref in citations.in_ledger_text(text_of(e))]
    return [entry_state(e, day) for e in citing[:limit]], len(citing)


async def note_candidates(session: AsyncSession, uid: int | None, ref: Ref) -> list[str]:
    """Indexed notes whose words include the ref (`q14`, `q014`): the ones that may cite it.

    The keyword index drops the brackets, so the caller reads each note and
    matches exactly.
    """
    from src.services.fts import combined_tsquery

    letter = ref.letter.lower()
    query = " or ".join(f"{letter}{'0' * zeros}{ref.number}" for zeros in range(3))
    owner = NoteMetadata.user_id.is_(None) if uid is None else NoteMetadata.user_id == uid
    return list((await session.execute(
        select(NoteMetadata.file_path)
        .where(owner, NoteMetadata.content_tsvector.op("@@")(combined_tsquery(query)))
        .order_by(NoteMetadata.file_path)
    )).scalars().all())
