"""The conventions of kelno's vault, as a plugin (`PLUGINS=["kelno_vault"]`).

Fully specific to that vault and not configurable: a different vault writes
its own plugin rather than adapting this one. It holds conventions only (folder
names, naming and linking rules), never the vault's content.

What it does today: it adds the tool `open_area` (`open_area`), opening one
of the vault's areas, with the vault guide note's "Working on an area"
section, and the agent path tools `agent_path` and `agent_path_reached_by`
(`agent_path_tools`, over the engine `agent_paths`), drawing what each kind of
agent in the owner's setup meets. A new note's name, or a move's destination, is checked
against its folder's `note_names` setting (`note_names`, the name check). On
the lines a note write adds or changes: a task line written by hand is refused (`task_lines`, the check); a first heading
repeating the note's name is dropped (`title_heading`), then bare-name
wikilinks get the note's full path (`link_paths`), in that order so the link
report's line numbers are the written note's; then a `<` Obsidian would read
as the start of an HTML tag gets a backslash, unless the caller passed
`allow_html` (`angle_brackets`, `create_note` and `edit_note` only); last, a change to a rule note
whose lines gain no proposal ref is pointed out, and so are a link to another
rule note in none of the pointer shapes and a rule no agent path then reaches
(`rule_notes`, on the text as written; what a rule file is, the notes and the
`## For agents` sections, comes from a note in the vault, `rule_files`). A
note write, or a move's destination, inside an area's folder when the caller hasn't called
`open_area` for that area in the last hour is pointed out too
(`area_opened`, `area_not_opened:`, ledger F89). The vault-specific behaviours
still in the core, and the order they move here in, are in
docs/architecture/plugins.md.
"""

import asyncio
import logging

from sqlalchemy import select

from src import database, plugins
from src.models.db import NoteMetadata
from src.plugins.kelno_vault import (
    agent_path_tools,
    angle_brackets,
    area_opened,
    conventions,
    link_paths,
    note_names,
    open_area,
    rule_notes,
    task_lines,
    task_place,
    title_heading,
)
from src.services import vault
from src.services.filters import apply_note_filters

logger = logging.getLogger(__name__)

#: The tool descriptions name each check and its result in a line; the guide sections hold the detail.
#: Every served description stays within the cap `tests/test_issue_343_tool_metadata.py` sets, this
#: text included: `edit_note` and `move_note` sit close to theirs, so each gets one line.
_WRITER_TEXT = (
    "**This vault's checks**, on the lines a write adds or changes (conventions: "
    f"`{conventions.CONVENTIONS_NOTE}`; details: the \"(this server)\" sections of `get_vault_guide`). A task line "
    "is refused (`task_line_by_hand`): use the task tools. A first heading repeating the note's name is dropped "
    "(`title_heading_removed:`). A bare-name wikilink one note has gets its full path (`links_rewritten:`; an "
    "ambiguous or unknown one `links_flagged:`), so `content_hash` is of what was written. A rule-note change none "
    "of whose lines gains a new proposal ref is flagged (`rule_change_unlogged:`), a link in its lines to "
    "another rule note in none of the pointer shapes (`pointer_or_mention:`), and a rule no agent path then "
    "reaches (`reached_by_no_path:`). A write in an area's folder "
    "with no `open_area` call for it in the last hour is flagged (`area_not_opened:`). A `<` read as HTML is "
    "escaped (`angle_brackets_escaped:`) unless `allow_html`."
)

_CREATE_NOTE_NAME_TEXT = (
    "A new note's name is checked against its folder's `note_names` setting: in a dated folder, one not starting "
    "with the date the note is about is refused with `note_name_date`; in an undated one, a dated name is flagged "
    f"(`note_name:`). Where a new note goes: `{conventions.FILING_NOTE}`."
)

_WRITE_FILE_TEXT = (
    "On a `.md`, this vault's checks run as in `create_note` (`task_line_by_hand`, `note_name_date` and "
    "`note_name:` for a new note, `title_heading_removed:`, `links_rewritten:`, `links_flagged:`, "
    "`rule_change_unlogged:`, `pointer_or_mention:`, `reached_by_no_path:`, `area_not_opened:`)."
)

_MOVE_NOTE_TEXT = f"The name is checked as in `create_note`. Where a note goes: `{conventions.FILING_NOTE}`."

_EDIT_NOTE_TEXT = "Vault checks as in `create_note`: hand-written task lines refused, `<` escaped unless `allow_html`."

_LINKS_GUIDE = (
    "## Links (this server)\n\n"
    f"{conventions.see_below(conventions.LINKS)} As a safety net, the note tools (`edit_note`, `create_note`, "
    "`write_file` to a `.md`) give a bare-name link or note embed you write (`[[Areas]]`) the note's full path when "
    "exactly one note has that name, archived notes counted (display text kept, or the text Obsidian showed added "
    "when there was none), on the lines you add or change only, checkbox lines left alone, and list it under "
    "`links_rewritten:`; the reported `content_hash` is of what was written, not what you sent. A name several "
    "notes share, or none has (a note made in the last few minutes may not be indexed yet), a full path to no note "
    "on disk, and a path in code formatting after a link are left as you wrote them and listed under "
    "`links_flagged:`: write the full path of the note you mean. A write is never refused for a link."
)


async def _full_path_links(write: plugins.NoteWrite, new: str) -> tuple[str, link_paths.LinkReport | None, str]:
    """`new` with its bare-name wikilinks given full paths, the report, and a failure line.

    Only lines the write adds or changes are touched. Never fails the write:
    when the note list cannot be read, `new` is returned as is with a line
    saying so.
    """
    if "\n" not in new and "\r" in new:
        # A note with lone-CR line endings: work on LF and give its CRs back,
        # as the title heading does, so the lines compare one by one.
        old_lf = write.old.replace("\r", "\n") if write.old is not None else None
        text, report, failure = await _full_path_links(
            plugins.NoteWrite(write.tool, write.path, old_lf, new.replace("\r", "\n"), write.dry_run, write.user_id),
            new.replace("\r", "\n"))
        return text.replace("\n", "\r"), report, failure
    if not link_paths.needs_check(write.old, new):
        return new, None, ""
    try:
        async with database.async_session() as session:
            paths = (await session.execute(
                apply_note_filters(select(NoteMetadata.file_path), user_id=write.user_id))).scalars().all()
        index = link_paths.NoteIndex([*paths, write.path])

        def on_disk(target: str) -> bool:
            """Whether the note a full-path link names is a visible file in the caller's vault."""
            note = target if target.lower().endswith(".md") else target + ".md"
            try:
                return vault.validate_visible_path(note, user_id=write.user_id).is_file()
            except (ValueError, OSError):
                return False                          # outside the vault, hidden or unreadable: no note there

        rewritten, report = await asyncio.to_thread(link_paths.rewrite, write.old, new, index, on_disk)
        return rewritten, report, ""
    except Exception as exc:  # noqa: BLE001 — the write goes ahead unchanged; the result says so
        # debug, not warning: a caller drives this path (the D18 permit rule); the result tells the caller.
        logger.debug("note write: bare-name links could not be checked", exc_info=True)
        return new, None, (f"\n\nBare-name links in this write could not be checked ({type(exc).__name__}): "
                           "write each link with the note's full path yourself.")


_TITLE_HEADING_GUIDE = (
    "## Titles (this server)\n\n"
    f"{conventions.see_below(conventions.TITLES)} When a write through the note tools adds or changes the body's "
    "first non-blank line and it is a heading (`#` to `######`) whose text is exactly the note's whole file name "
    "without `.md`, its date too (case and surrounding spaces ignored, formatting such as `**` not stripped), the "
    "tool removes it with the blank lines after it and says so under `title_heading_removed:`; the reported "
    "`content_hash` is of what was written. A heading the write leaves as it was stays, and a near repeat "
    "(`# Call with the vet` in `2026-11-12 Call with the vet`, `# **Dog food**`) is not caught. A write is never "
    "refused for it."
)


_OPEN_AREA_GUIDE = (
    "## Opening an area (this server)\n\n"
    "`open_area` opens one of the vault's areas in one call: call it before working on an area (the vault's "
    "conventions note, below, says so too); it may come before `get_vault_guide`. Its result carries the area's own "
    "rules for agents (`for_agents`, its main note's `## For agents` section) and the conventions for working on "
    f"any area (`area_conventions`, the note `{open_area.OPEN_AREA_CONVENTIONS_NOTE}`, read at each call, so it is "
    "current): follow both while you work in the area. Its description says what else it returns.\n\n"
    + area_opened.GUIDE
)


async def _rewrite(write: plugins.NoteWrite) -> plugins.Rewrite:
    """The title heading, then the links: the heading may drop lines, so the link report is made on the result.

    The rule-edit nudge only reports, on the text as written.
    """
    text, removed = title_heading.strip(write.old, write.new, write.path)
    text, links_report, links_failure = await _full_path_links(write, text)
    # After the links, which never hold a `<`; it keeps the line count, so their report's lines stay right.
    escaped = angle_brackets.EscapeReport()
    if write.tool in angle_brackets.TOOLS:
        text, escaped = angle_brackets.escape(write.old, text, allow_html=write.allow_html)
    rule_check = await rule_notes.check(write.path, write.old, text, write.user_id)
    unopened = await area_opened.unopened_area(write.path, write.user_id)

    def report(shift: int, dry_run: bool) -> str:
        links = links_failure or (
            link_paths.render(link_paths.shifted(links_report, shift), dry_run=dry_run) if links_report else "")
        rules = rule_check.failure or (rule_notes.render(rule_check.unlogged, shift)
                                       + rule_notes.render_unshaped(rule_check.unshaped, shift)
                                       + rule_notes.render_unreached(rule_check.unreached, rule_check.reach_unknown))
        return (rules + title_heading.render(removed, dry_run=dry_run) + links
                + angle_brackets.render(angle_brackets.shifted(escaped, shift), dry_run=dry_run)
                + area_opened.render(unopened, dry_run=dry_run))

    return plugins.Rewrite(text, report)


async def _check_name(note: plugins.NoteName) -> plugins.NameCheck | None:
    """The folder's `note_names` setting; for `move_note`, also the area the note moves into (`area_opened`).

    A new note gets the area nudge from its write's report instead, so it is not given twice.
    """
    result = await note_names.check_name(note)
    if note.tool != "move_note" or (result is not None and result.refusal is not None):
        return result
    nudge = area_opened.render(await area_opened.unopened_area(note.path, note.user_id))
    if not nudge:
        return result
    return plugins.NameCheck(note=(result.note if result is not None else "") + nudge)


plugin = plugins.Plugin(
    name="kelno_vault",
    check=task_lines.check,
    rewrite=_rewrite,
    check_name=_check_name,
    check_task=task_place.check,
    tools=(open_area.TOOL, *agent_path_tools.TOOLS),
    tool_text={
        "create_note": f"{_WRITER_TEXT} {_CREATE_NOTE_NAME_TEXT}",
        "edit_note": _EDIT_NOTE_TEXT,
        "move_note": _MOVE_NOTE_TEXT,
        "write_file": _WRITE_FILE_TEXT,
        "create_task:path": task_place.PATH_TEXT,
    },
    guide_text="\n\n".join((task_lines.GUIDE, rule_notes.GUIDE, note_names.GUIDE, _LINKS_GUIDE,
                           _TITLE_HEADING_GUIDE, angle_brackets.GUIDE, _OPEN_AREA_GUIDE)),
    # Ends the path tools' helper process with the server (`agent_path_tools.helper_lifespan`).
    startup=agent_path_tools.helper_lifespan,
)
