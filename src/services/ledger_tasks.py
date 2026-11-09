"""Links between agent-ledger feedback and the vault tasks that cite it.

What the owner decides to do about a feedback entry becomes a vault task whose
text cites the entry as `ledger F12`. That citation is the only link anyone
writes: the server reads it back from the task index (`note_tasks`, migration
031) whenever a ledger or task tool needs it, so it never goes stale against
a second, hand-written copy. Nothing is stored and no foreign key points into
the index tables: the ledger stays primary data that no reindex can reach.

- An entry is **decided** when a task that is not cancelled cites it.
- Its task state, over the tasks citing it: `open` (at least one is not
  closed), `done` (all closed, at least one done) or `cancelled` (all
  cancelled: the decision was dropped, the entry is undecided again).
- Matching: `ledger F<n>`, case-insensitive, whole words, leading zeros
  ignored, one citation per entry (`ledger F9, F10` links F9 only). Only
  feedback refs count. A bare `F12` is not a citation; the task tools point it
  out when it names an existing feedback entry.

The index lags behind hand edits until the next index pass; the task tools
re-index their note at once. See docs/architecture/agent-ledger.md, "Task
links".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.db import LedgerEntry, NoteMetadata, NoteTask

#: `ledger F12`: the citation that links a task to a feedback entry.
CITATION_RE = re.compile(r"(?<!\w)ledger[ \t]+F0*([0-9]{1,6})(?!\w)", re.IGNORECASE)
#: A bare `F12` (capital F, a whole word): looks like a ref but links nothing.
_BARE_RE = re.compile(r"(?<![\w#/])F0*([0-9]{1,6})(?!\w)")
#: The lines PostgreSQL hands over for exact matching: any holding the word.
#: A plain substring, so it can never leave out a line `CITATION_RE` matches
#: (a regex there would follow the database's locale, Python's does not).
_SQL_PREFILTER = "%ledger%"

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


def cited_numbers(text: str) -> list[int]:
    """Feedback numbers `text` cites as `ledger F<n>`, in order, once each."""
    return list(dict.fromkeys(int(m.group(1)) for m in CITATION_RE.finditer(text or "")))


def bare_numbers(text: str) -> list[int]:
    """Numbers written as a bare `F<n>`, outside any `ledger F<n>` citation."""
    text = text or ""
    cited_spans = [m.span() for m in CITATION_RE.finditer(text)]
    out = []
    for m in _BARE_RE.finditer(text):
        if not any(start <= m.start() < end for start, end in cited_spans):
            out.append(int(m.group(1)))
    return list(dict.fromkeys(out))


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
        .where(owner, NoteTask.kind == "task", NoteTask.line.ilike(_SQL_PREFILTER))
        .order_by(NoteMetadata.file_path, NoteTask.line_number)
    )).all()


def link(rows, parse: Callable[[str, str], object]) -> dict[int, list[CitingTask]]:
    """`{feedback number: [tasks citing it]}` from `citing_rows`.

    `parse(line, path)` returns the plugin's reading of the line (a task with
    a `status.type`) or None when the plugin would not read it as a task.
    """
    out: dict[int, list[CitingTask]] = {}
    for row in rows:
        numbers = cited_numbers(row.line)
        if not numbers:
            continue
        task = parse(row.line, row.file_path)
        if task is None:
            continue
        item = CitingTask(f"{row.file_path}:{row.line_number}", task.status.type, row.line)
        for number in numbers:
            out.setdefault(number, []).append(item)
    return out


def states(links: dict[int, list[CitingTask]]) -> dict[int, str]:
    """`{feedback number: task state}` for every cited entry."""
    out = {}
    for number, tasks in links.items():
        state = task_state(t.status_type for t in tasks)
        if state is not None:
            out[number] = state
    return out


async def feedback_entries(session: AsyncSession, uid: int | None, numbers: Iterable[int]) -> dict[int, LedgerEntry]:
    """The caller's feedback entries with these numbers."""
    numbers = sorted(set(numbers))
    if not numbers:
        return {}
    owner_key = 0 if uid is None else int(uid)
    rows = (await session.execute(
        select(LedgerEntry).where(LedgerEntry.owner_key == owner_key, LedgerEntry.kind == "feedback",
                                  LedgerEntry.number.in_(numbers))
    )).scalars().all()
    return {e.number: e for e in rows}


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
                       note_rows: list) -> tuple[list[dict], list[str]]:
    """What a task tool reports about the ledger citations in the line it wrote.

    `([{ref, title, status, tasks}], notes)`: each cited entry with its own
    status and the state of all the tasks citing it now; a cited number with
    no entry is reported as `unknown`; a bare `F<n>` naming an existing
    feedback entry gets a note saying it links nothing.

    The note just written is read from `note_rows` (its text as published,
    scanned), never from the index: the immediate re-index does not run while
    an index pass holds the lock, nor for a note not yet indexed, and a state
    read from stale rows could say "done" beside the open task just created.
    """
    cited = cited_numbers(text)
    bare = [n for n in bare_numbers(text) if n not in cited]
    if not cited and not bare:
        return [], []
    entries = await feedback_entries(session, uid, cited + bare)
    task_states = {}
    if cited:
        rows = [r for r in await citing_rows(session, uid) if r.file_path != note_path] + list(note_rows)
        task_states = states(link(rows, parse))
    refs = []
    for number in cited:
        entry = entries.get(number)
        if entry is None:
            refs.append({"ref": f"F{number}", "status": "unknown"})
            continue
        refs.append({"ref": entry.ref, "title": entry.title, "status": entry.status,
                     "tasks": task_states.get(number)})
    notes = [
        f"The line mentions F{n} without `ledger` before it, so it is not linked to ledger entry F{n} "
        f"(\"{entries[n].title}\"). To link it, write `ledger F{n}`."
        for n in bare if n in entries
    ]
    return refs, notes
