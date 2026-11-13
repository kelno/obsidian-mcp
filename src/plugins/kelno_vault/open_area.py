"""`open_area`: the one call an agent makes when it opens one of this vault's areas.

A tool of the `kelno_vault` plugin (`Plugin.tools`), registered by the core,
which wraps it in `_tracked` like every tool. It is built on this vault's
layout: the list of areas (`AREAS_INDEX_PATH`, a table read by
`areas_index`), the areas' main notes declaring `area_tag`, their `## Tasks`
section, `#next`. It reads the vault, the task index and the agent ledger
through the core's own helpers. See docs/architecture/open-area.md.
"""

import asyncio
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from src import database, plugins
from src.auth.session import current_user_id
from src.config import MAX_NOTE_BYTES, settings
from src.mcp_server.tools import (
    _area_index,
    _ledger,
    _ledger_listed,
    _ledger_summary,
    _ledger_task_links,
    _ledger_tasks,
    _note_owner_predicate,
    _open_note_url,
    _read_current_note,
    _task_context,
    _task_ops,
    _tf,
    _tq,
)
from src.plugins.kelno_vault import areas_index as _areas_index
from src.plugins.kelno_vault.area_result import AreaTask, OpenAreaResult
from src.services import private_folders
from src.services.tool_outcomes import body_refusal as _body_refusal
from src.services.vault import read_bytes

#: The note listing every area in one table (columns "Related folders" and "Vault folder").
AREAS_INDEX_PATH = "Areas/Areas.md"

# ── open_area ─────────────────────────────────────────────────────────
#
# The one call an agent makes when it opens an area: to start work there, or
# to answer a question about it. It gathers what the agent would otherwise
# collect in several calls and may skip: the main note (agent rules at the
# top, `## Current position`), the area's folders outside the vault, its
# ledger entries and its open tasks. See docs/architecture/open-area.md.
#
# Kept as one function per part so later parts slot in beside them: notes
# changed since an agent last looked, stale-task hints, matching a "When …"
# task's trigger.



#: The main note's text is cut past this many characters.
OPEN_AREA_NOTE_MAX_CHARS = 15000
#: Active ledger entries listed (the rest: `ledger_search(area=…)`).
OPEN_AREA_ENTRIES_MAX = 40
#: Open tasks listed (the rest: `list_tasks(area=…)`).
OPEN_AREA_TASKS_MAX = 30
#: The section of the main note left out: the open tasks come from every note instead.
OPEN_AREA_TASKS_HEADING = "Tasks"

OPEN_AREA_INSTRUCTIONS = (
    "Starting work: check each task and entry against the work you're starting (read an entry with ledger_get "
    "when its title is not enough). Is it related? Does the owner need to hear about it? Is it done, or made "
    "stale, by this work (set_task_status or edit_task for a task, ledger_update for an entry)? Is this work the "
    "event a \"When …\" task (#when) waits for? If so, say so to the owner in your reply before you tick or change that task.",
    "Answering a status question (\"where am I on X?\"): report the Current position (with its date) and the "
    "#next tasks, or say none is marked #next; the rest is for agents about to act.",
)


def _open_area_refusal(message: str) -> OpenAreaResult:
    return OpenAreaResult(error=message)


def _open_area_failed(prose: str, code: str = "invalid_argument") -> OpenAreaResult:
    outcome = _body_refusal(prose, code)
    result = OpenAreaResult(error=str(outcome))
    result._body_outcome = outcome
    return result


def _read_vault_text(path: str, uid) -> str:
    """A vault note's text, up to `MAX_NOTE_BYTES`; FileNotFoundError, ValueError or OSError otherwise."""
    return read_bytes(path, uid, MAX_NOTE_BYTES).decode("utf-8", errors="replace")


def _archived(main_note: str) -> bool | None:
    folder = settings.archive_folder
    if not folder:
        return None
    return main_note.casefold().startswith(folder.strip("/").casefold() + "/")


async def _open_area_rows(uid, notes: list[str]) -> list | None:
    """The rows of the list of areas, or None with a note saying why."""
    path = AREAS_INDEX_PATH
    try:
        text_ = await asyncio.to_thread(_read_vault_text, path, uid)
        return _areas_index.parse(text_)
    except FileNotFoundError:
        notes.append(f"The list of areas ({path}) was not found, so related folders are unknown.")
    except (ValueError, OSError) as exc:
        notes.append(f"The list of areas ({path}) could not be read ({type(exc).__name__}).")
    except _areas_index.AreasIndexError as exc:
        notes.append(f"The list of areas ({path}) could not be read: {exc}.")
    return None


async def _open_area_main_note(path: str, uid, notes: list[str]) -> tuple[str | None, int | None]:
    """`(text, full length when cut)`: the main note without frontmatter and its Tasks section."""
    try:
        text_ = await asyncio.to_thread(_read_vault_text, path, uid)
    except (FileNotFoundError, ValueError, OSError) as exc:
        notes.append(f"The main note {path} could not be read ({type(exc).__name__}).")
        return None, None
    text_, _removed = _areas_index.strip_section(_areas_index.strip_frontmatter(text_), OPEN_AREA_TASKS_HEADING)
    text_ = text_.strip("\n") + "\n"
    if len(text_) <= OPEN_AREA_NOTE_MAX_CHARS:
        return text_, None
    notes.append(f"The main note's text is cut at {OPEN_AREA_NOTE_MAX_CHARS:,} of {len(text_):,} characters: "
                 f"read_note(\"{path}\") has the rest.")
    return text_[:OPEN_AREA_NOTE_MAX_CHARS], len(text_)


async def _open_area_entries(tag: str, uid, notes: list[str]) -> tuple[list, bool]:
    """Active ledger entries of the area, as `ledger_search(area=…)` lists them."""
    try:
        async with database.async_session() as session:
            filters = await _ledger.build_filters(session, uid, area=tag)
            rows, truncated, via = await _ledger_listed(session, uid, filters, OPEN_AREA_ENTRIES_MAX)
            links, links_note = await _ledger_task_links(session, uid)
    except _ledger.LedgerError as exc:
        notes.append(f"The area's ledger entries could not be listed: {exc}")
        return [], False
    if links_note:
        notes.append(links_note)
    states = _ledger_tasks.states(links) if links is not None else {}
    entries = [_ledger_summary(r, tasks=states.get(_ledger_tasks.canonical(r.ref)), via_topic=via.get(r.id))
               for r in rows]
    if truncated:
        notes.append(f"Only the {OPEN_AREA_ENTRIES_MAX} most recently changed active entries are listed: "
                     f"ledger_search(area=\"{tag}\") lists the rest, by words or meaning too.")
    if links is not None:
        to_close = await _open_area_to_close(tag, uid, states)
        if to_close:
            notes.insert(0, f"Feedback of this area whose citing tasks are all done ({', '.join(to_close)}; the "
                            "owner may have ticked them in Obsidian): once the work you were asked for allows, or now "
                            "if the owner asked about the area, close each with ledger_update(status=\"handled\", "
                            "handled_account=…) saying what was done, unless part of it is still to do (then tell "
                            "the owner instead); then say so to the owner.")
    return entries, truncated


async def _open_area_to_close(tag: str, uid, states: dict[str, str]) -> list[str]:
    """Refs of the area's open feedback whose citing tasks are all done (`ledger_search(tasks="done")`)."""
    done = tuple(sorted(int(ref[1:]) for ref, state in states.items() if ref[0] == "F" and state == "done"))
    if not done:
        return []
    async with database.async_session() as session:
        filters = await _ledger.build_filters(session, uid, kinds=["feedback"], area=tag)
        # Filed under this area only: feedback tied to it by a topic belongs to
        # another area, whose agents close it.
        filters.area_topics = {}
        filters.feedback_numbers = done
        rows, truncated, _via = await _ledger_listed(session, uid, filters, OPEN_AREA_ENTRIES_MAX)
    return [r.ref for r in rows] + (["…"] if truncated else [])


async def _open_area_tasks(tag: str, uid, notes: list[str]) -> tuple[list[AreaTask], int]:
    """`(tasks, total)`: open tasks carrying `#<tag>` (or a tag under it), in any note.

    `#next` first, then by urgency; each listed line checked on disk like
    `list_tasks`. Statuses are read with the owner's Tasks settings.
    """
    from sqlalchemy import select

    from src.models.db import NoteMetadata, NoteTask

    try:
        task_settings, registry, today = await _task_context(uid)
    except _task_ops.TaskOpError as exc:
        notes.append(f"The area's tasks could not be read: {exc}")
        return [], 0
    wanted = "#" + tag.casefold()
    async with database.async_session() as session:
        rows = (await session.execute(
            select(NoteMetadata.file_path, NoteTask.line_number, NoteTask.line)
            .join(NoteMetadata, NoteMetadata.id == NoteTask.note_id)
            .where(_note_owner_predicate(uid), NoteTask.kind == "task", NoteTask.line.ilike(f"%{wanted}%"))
        )).all()

    def select_open():
        found = []
        for row in rows:
            task = _task_ops.parse_line(row.line, row.file_path, task_settings, registry)
            if task is None or task.status.type in ("DONE", "CANCELLED", "NON_TASK"):
                continue
            tags = [t.casefold() for t in task.tags]
            if not any(t == wanted or t.startswith(wanted + "/") for t in tags):
                continue
            found.append((row, task, "#next" in tags))
        found.sort(key=lambda item: (not item[2], -_tf.urgency(item[1], today), item[0].file_path,
                                     item[0].line_number))
        return found

    found = await asyncio.to_thread(select_open)
    current: dict[str, tuple] = {}
    out, stale = [], 0
    for row, task, is_next in found:
        if len(out) >= OPEN_AREA_TASKS_MAX:
            break
        if row.file_path not in current:
            current[row.file_path] = await asyncio.to_thread(_read_current_note, row.file_path, uid)
        _hash, note_lines, _reason = current[row.file_path]
        on_disk = (note_lines[row.line_number - 1][0]
                   if note_lines is not None and row.line_number <= len(note_lines) else None)
        if on_disk != row.line:
            stale += 1
            continue
        out.append(AreaTask(
            ref=f"{row.file_path}:{row.line_number}",
            text=None if private_folders.is_private(row.file_path) else row.line,
            status=task.status.type, next=True if is_next else None,
            due=task.due.text if task.due is not None else None,
            scheduled=task.scheduled.text if task.scheduled is not None else None,
        ))
    total = len(found) - stale
    if stale:
        notes.append(f"{stale} task line(s) changed since the index read them and are left out; the next index "
                     "pass picks them up.")
    if total > len(out):
        notes.append(f"{len(out)} of {total} open tasks are listed (#next first, then by urgency): "
                     f"list_tasks(area=\"{tag}\") lists them all.")
    if out and not any(t.next for t in out):
        notes.append("No open task of this area carries #next.")
    if any(t.text is None for t in out):
        notes.append("A task listed without its text is in a private folder: mention it to the owner only as "
                     "being in a private folder. If the owner asked for it, or a protocol the owner set up does, "
                     "list_tasks(area=…, include_private=true) shows its text.")
    return out, total


def _open_area_note_url(main_note: str | None) -> str | None:
    return _open_note_url.open_note_url(main_note) if main_note else None


async def open_area_impl(area: str | None = None, folder: str | None = None) -> OpenAreaResult:
    if (area is None) == (folder is None) or not (area or folder or "").strip():
        return _open_area_failed("give area (its name, slug or tag) or folder (a path outside the vault), "
                                 "one of the two.")
    uid = current_user_id.get()
    notes: list[str] = []
    areas = await _area_index(uid)
    declared = {path.casefold(): (tag, path, description) for tag, path, description in areas.described()}
    rows = await _open_area_rows(uid, notes)
    matched_folder, row = None, None

    if folder is not None:
        if rows is None:
            return _open_area_failed(f"{notes[0]} A folder cannot be matched to an area without it.",
                                     "not_found")
        hits = _areas_index.rows_for_folder(rows, folder)
        if not hits:
            return OpenAreaResult(found=False, notes=[
                f"No area lists {folder} (or a folder above it) among its related folders in "
                f"{AREAS_INDEX_PATH}. Ask the owner which area it belongs to."])
        deepest = len(_areas_index.normalise_folder(hits[0][1]))
        tied = {h[0].name for h in hits if len(_areas_index.normalise_folder(h[1])) == deepest}
        if len(tied) > 1:
            return OpenAreaResult(found=False, notes=[
                f"{folder} is listed under several areas ({', '.join(sorted(tied))}) in "
                f"{AREAS_INDEX_PATH}: ask the owner which one it belongs to, then call open_area(area=…)."])
        row, matched_folder = hits[0]
        if row.main_note is None or row.main_note.casefold() not in declared:
            why = ("it has no main note yet" if row.main_note is None
                   else f"its main note {row.main_note} declares no area_tag")
            return OpenAreaResult(
                found=True, name=row.name, matched_folder=matched_folder, related_folders=row.folders or None,
                main_note=row.main_note, main_note_open_url=_open_area_note_url(row.main_note),
                notes=[f"{folder} belongs to the area {row.name!r}, but {why}: the "
                       f"list of areas says \"{row.vault_cell}\"."])
        tag, main_note, description = declared[row.main_note.casefold()]
    else:
        try:
            tag = areas.lookup(area)
        except _tq.QueryError as exc:
            return _open_area_failed(f"{exc}.", "not_found")
        _tag, main_note, description = next(d for d in areas.described() if d[0] == tag)
        if rows is not None:
            row = _areas_index.row_for_main_note(rows, main_note)

    if rows is not None and row is None:
        notes.append(f"The area has no row in {AREAS_INDEX_PATH}, so its related folders are unknown.")
    text_, full_chars = await _open_area_main_note(main_note, uid, notes)
    entries, entries_truncated = await _open_area_entries(tag, uid, notes)
    tasks, tasks_total = await _open_area_tasks(tag, uid, notes)
    archived = _archived(main_note)
    if archived:
        notes.insert(0, "This area is archived: its work is finished. Tell the owner before starting new work in it.")
    return OpenAreaResult(
        found=True, area=tag, tag="#" + tag, name=row.name if row else None, description=description,
        main_note=main_note, main_note_open_url=_open_area_note_url(main_note), archived=archived,
        matched_folder=matched_folder, main_note_text=text_,
        main_note_chars=full_chars, related_folders=(row.folders or None) if row else None,
        entries=entries or None, entries_truncated=entries_truncated or None,
        tasks=tasks or None, tasks_total=tasks_total if tasks_total > len(tasks) else None,
        instructions=list(OPEN_AREA_INSTRUCTIONS), notes=notes or None,
    )


# ── The tool ────────────────────────────────────────────────────────

_A_AREA = Field(description="The area: its slug (voice-bridge), its tag (#area/voice-bridge), area/voice-bridge "
                            "or its main note's name (Voice bridge). Give area or folder, not both.")
_A_FOLDER = Field(description="Instead of area: an absolute path outside the vault, such as the repo or working "
                              "directory a session runs in; matched against each area's related folders, a "
                              "subfolder included.")


async def open_area(
    area: Annotated[str | None, _A_AREA] = None,
    folder: Annotated[str | None, _A_FOLDER] = None,
) -> OpenAreaResult:
    """Open an area: its rules, current position, ledger entries and open tasks.

    Read-only. Call it first when you start work in an area or answer a
    question about one, and when a session works in a folder outside the
    vault (`folder`), to find its area. One call returns the area's tag and
    description, its main note's text (the agent rules at its top and
    `## Current position`; without its Tasks section), its related folders
    outside the vault, its active ledger entries (those filed under another
    area but carrying one of its topics are marked `via_topic`), and its open
    tasks from every note carrying its tag, `#next` first, then by urgency.
    Both lists are capped; `notes` say where the rest is, and name open
    feedback whose citing tasks are all done (ticked in Obsidian, say). `instructions`
    then say what to do with it all. A folder no area lists comes back with
    `found: false`: ask the owner which area it belongs to.
    With `OPEN_NOTE_URL` set, `main_note_open_url`: give that link when
    pointing the owner to the main note.
    """
    return await open_area_impl(area=area, folder=folder)


TOOL = plugins.PluginTool(
    name="open_area",
    fn=open_area,
    logged_args=("area", "folder"),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    refusal_result=_open_area_refusal,
    result_type=OpenAreaResult,
)
