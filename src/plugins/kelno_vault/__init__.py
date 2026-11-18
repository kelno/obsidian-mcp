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
report's line numbers are the written note's; last, a change to a rule note
whose lines gain no proposal ref is pointed out (`rule_notes`, on the text
as written; what a rule file is, the notes and the `## For agents` sections,
comes from a note in the vault, `rule_files`). The vault-specific behaviours
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
    conventions,
    link_paths,
    note_names,
    open_area,
    rule_notes,
    task_lines,
    title_heading,
)
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
    "of whose lines gains a new proposal ref is flagged (`rule_change_unlogged:`)."
)

_CREATE_NOTE_NAME_TEXT = (
    "A new note's name is checked against its folder's `note_names` setting: in a dated folder, one not starting "
    "with the date the note is about is refused with `note_name_date`; in an undated one, a dated name is flagged "
    "(`note_name:`)."
)

_WRITE_FILE_TEXT = (
    "On a `.md`, this vault's checks run as in `create_note` (`task_line_by_hand`, `note_name_date` and "
    "`note_name:` for a new note, `title_heading_removed:`, `links_rewritten:`, `links_flagged:`, "
    "`rule_change_unlogged:`)."
)

_MOVE_NOTE_TEXT = "The name is checked as in `create_note`."

_EDIT_NOTE_TEXT = "Vault checks as in `create_note`: a task line written by hand is refused."

_LINKS_GUIDE = (
    "## Links (this server)\n\n"
    f"{conventions.see_below(conventions.LINKS)} As a safety net, the note tools (`edit_note`, `create_note`, "
    "`write_file` to a `.md`) give a bare-name link or note embed you write (`[[Areas]]`) the note's full path when "
    "exactly one note has that name, archived notes counted (display text kept, or the text Obsidian showed added "
    "when there was none), on the lines you add or change only, checkbox lines left alone, and list it under "
    "`links_rewritten:`; the reported `content_hash` is of what was written, not what you sent. A name several "
    "notes share, or none has, a full path to no note, and a path in code formatting after a link are left as you "
    "wrote them and listed under `links_flagged:`: write the full path of the note you mean. A write is never "
    "refused for a link."
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
        rewritten, report = await asyncio.to_thread(link_paths.rewrite, write.old, new, index)
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
    "`open_area` opens one of the vault's areas in one call; when to call it is in the "
    f"\"{open_area.OPEN_AREA_CONVENTIONS_HEADING}\" section of the vault's conventions note, below. Its result "
    "carries the area's own rules for agents (`for_agents`, its main note's `## For agents` section) and the "
    f"\"{open_area.OPEN_AREA_CONVENTIONS_HEADING}\" section (`area_conventions`, read at each call, so it is "
    "current): follow both while you work in the area. Its description says what else it returns."
)


async def _rewrite(write: plugins.NoteWrite) -> plugins.Rewrite:
    """The title heading, then the links: the heading may drop lines, so the link report is made on the result.

    The rule-edit nudge only reports, on the text as written.
    """
    text, removed = title_heading.strip(write.old, write.new, write.path)
    text, links_report, links_failure = await _full_path_links(write, text)
    rule_lines, rules_failure = await rule_notes.unlogged_lines(write.path, write.old, text, write.user_id)

    def report(shift: int, dry_run: bool) -> str:
        links = links_failure or (
            link_paths.render(link_paths.shifted(links_report, shift), dry_run=dry_run) if links_report else "")
        rules = rules_failure or rule_notes.render(rule_lines, shift)
        return rules + title_heading.render(removed, dry_run=dry_run) + links

    return plugins.Rewrite(text, report)


plugin = plugins.Plugin(
    name="kelno_vault",
    check=task_lines.check,
    rewrite=_rewrite,
    check_name=note_names.check_name,
    tools=(open_area.TOOL, *agent_path_tools.TOOLS),
    tool_text={
        "create_note": f"{_WRITER_TEXT} {_CREATE_NOTE_NAME_TEXT}",
        "edit_note": _EDIT_NOTE_TEXT,
        "move_note": _MOVE_NOTE_TEXT,
        "write_file": _WRITE_FILE_TEXT,
    },
    guide_text="\n\n".join((task_lines.GUIDE, rule_notes.GUIDE, note_names.GUIDE, _LINKS_GUIDE,
                           _TITLE_HEADING_GUIDE, _OPEN_AREA_GUIDE)),
)
