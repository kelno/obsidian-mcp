"""`open_area`'s structured result.

Fields come in the order an agent needs them: which area, what it is for,
its main note (the agent rules at its top and its `## Current position`),
where else it lives, then its ledger entries and its open tasks, and last
the two lines saying what to do with all of it.
"""

from __future__ import annotations

from pydantic import PrivateAttr

from src.mcp_server.ledger_result import EntrySummary
from src.mcp_server.read_result import _OmitNone
from src.services.tool_outcomes import BodyOutcome


class AreaTask(_OmitNone):
    #: `path:line`, 1-based.
    ref: str
    #: The task line as written; left out for a note in a private folder.
    text: str | None = None
    #: The Tasks plugin's status type: TODO, IN_PROGRESS, ON_HOLD, …
    status: str
    #: True when the task carries `#next`.
    next: bool | None = None
    due: str | None = None
    scheduled: str | None = None


class OpenAreaResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    error: str | None = None
    #: False when `folder` is listed under no area: ask the owner which area it belongs to.
    found: bool | None = None
    #: `area/<slug>`, as in `ledger_search` and `list_tasks(area=…)`.
    area: str | None = None
    #: The tag tasks carry: `#area/<slug>`.
    tag: str | None = None
    #: The area's name in the list of areas.
    name: str | None = None
    #: The main note's `description` frontmatter: what the area is for.
    description: str | None = None
    main_note: str | None = None
    #: The http(s) link that opens the main note in the Obsidian app, when the
    #: server sets `OPEN_NOTE_URL`: give it to the owner when pointing them to the note.
    main_note_open_url: str | None = None
    #: True when the main note is under the archive folder: the area is finished.
    archived: bool | None = None
    #: `folder=`: the listed folder it matched (it, or a folder above it).
    matched_folder: str | None = None
    #: The main note's text without its frontmatter and its `## Tasks` section
    #: (the open tasks are below, from every note).
    main_note_text: str | None = None
    #: Set when `main_note_text` was cut: the full length, in characters.
    main_note_chars: int | None = None
    #: Folders outside the vault that belong to the area (repos, working directories).
    related_folders: list[str] | None = None
    #: Active ledger entries, as `ledger_search(area=…)` lists them, newest change first.
    entries: list[EntrySummary] | None = None
    #: More active entries exist than are listed.
    entries_truncated: bool | None = None
    #: Open tasks (not done or cancelled) carrying the area's tag, in any note:
    #: `#next` first, then by urgency.
    tasks: list[AreaTask] | None = None
    #: How many open tasks there are in all, when more than are listed.
    tasks_total: int | None = None
    #: What to do with the above, for work and for a status question.
    instructions: list[str] | None = None
    notes: list[str] | None = None
