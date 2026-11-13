"""The conventions of kelno's vault, as a plugin (`PLUGINS=["kelno_vault"]`).

Fully specific to that vault and not configurable: a different vault writes
its own plugin rather than adapting this one. It holds conventions only (folder
names, naming and linking rules), never the vault's content.

What it does today, on the lines a note write adds or changes: a first heading
repeating the note's name is dropped (`title_heading`), then bare-name
wikilinks get the note's full path (`link_paths`), in that order so the link
report's line numbers are the written note's. The vault-specific behaviours
still in the core, and the order they move here in, are in
docs/architecture/plugins.md.
"""

import asyncio
import logging

from sqlalchemy import select

from src import database, plugins
from src.models.db import NoteMetadata
from src.plugins.kelno_vault import link_paths, title_heading
from src.services.filters import apply_note_filters

logger = logging.getLogger(__name__)

_LINKS_TOOL_TEXT = (
    "**Wikilinks get the note's full path.** Write links with the full path yourself (`[[Areas/Areas|Areas]]`); "
    "this is a safety net. On the lines a write adds or changes (never on lines it leaves as they were, nor on "
    "checkbox lines), a wikilink or note embed naming a note by bare name (`[[Areas]]`) that exactly one note has, "
    "archived ones counted, gets that note's full path; its display text is kept, or, when it had none, the text "
    "Obsidian showed is added, so it reads the same. The published text is then not byte for byte what you sent "
    "(the reported `content_hash` is of what was written). The result lists each under `links_rewritten:`. A bare "
    "name several notes have, or none, a full path to no note, and a path in code formatting after a link are left "
    "as written and listed under `links_flagged:`. The write is never refused for a link."
)

_LINKS_GUIDE = (
    "## Links (this server)\n\n"
    "Write links with the note's full path yourself (`[[Areas/Areas|Areas]]`). As a safety net, the note tools "
    "(`edit_note`, `create_note`, `write_file` to a `.md`) give a bare-name link or note embed you write "
    "(`[[Areas]]`) the note's full path when exactly one note has that name, archived notes counted (display text "
    "kept, or added when there was none), on the lines you add or change only, and list it under "
    "`links_rewritten:`. A name several notes share, or none has, is left as you wrote it and listed under "
    "`links_flagged:`: write the full path of the note you mean."
)


async def _full_path_links(write: plugins.NoteWrite, new: str) -> tuple[str, link_paths.LinkReport | None, str]:
    """`new` with its bare-name wikilinks given full paths, the report, and a failure line.

    Only lines the write adds or changes are touched. Never fails the write:
    when the note list cannot be read, `new` is returned as is with a line
    saying so.
    """
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


_TITLE_HEADING_TOOL_TEXT = (
    "**No title heading.** Obsidian shows the file name as the note's title, so when the body's first non-blank "
    "line is a heading (`#` to `######`) whose text is exactly the note's name (its whole file name without `.md`, "
    "a date included; case and surrounding spaces ignored, formatting such as `**` not stripped) and this write "
    "adds or changes that line, it is removed with the blank lines after it, and the result says so under "
    "`title_heading_removed:`; the reported `content_hash` is of what was written. Only exact repeats are caught: "
    "a near repeat is yours to avoid. The write is never refused for it."
)

_TITLE_HEADING_GUIDE = (
    "## Titles (this server)\n\n"
    "Obsidian shows a note's file name as its title, so don't start a note with a heading repeating its name. "
    "When a write through the note tools adds or changes the body's first non-blank line and it is a heading "
    "(`#` to `######`) whose text is exactly the note's name (the whole file name without `.md`, a date included; "
    "case and surrounding spaces ignored, formatting such as `**` not stripped), the tool removes it and says so "
    "under `title_heading_removed:`. A heading the write leaves as it was stays, and a near repeat "
    "(`# Call with the vet` in `2026-11-12 Call with the vet`, `# **Dog food**`) is yours to avoid."
)


async def _rewrite(write: plugins.NoteWrite) -> plugins.Rewrite:
    """The title heading, then the links: the heading may drop lines, so the link report is made on the result."""
    text, removed = title_heading.strip(write.old, write.new, write.path)
    text, links_report, links_failure = await _full_path_links(write, text)

    def report(shift: int, dry_run: bool) -> str:
        links = links_failure or (
            link_paths.render(link_paths.shifted(links_report, shift), dry_run=dry_run) if links_report else "")
        return title_heading.render(removed, dry_run=dry_run) + links

    return plugins.Rewrite(text, report)


plugin = plugins.Plugin(
    name="kelno_vault",
    rewrite=_rewrite,
    tool_text={
        "create_note": _TITLE_HEADING_TOOL_TEXT + "\n\n" + _LINKS_TOOL_TEXT,
        "edit_note": _TITLE_HEADING_TOOL_TEXT + "\n\n" + _LINKS_TOOL_TEXT,
        "write_file": "A first heading repeating the note's name is dropped as in `edit_note` "
                      "(`title_heading_removed:`), and bare-name wikilinks get the note's full path as in `edit_note` "
                      "(`links_rewritten:`, `links_flagged:`).",
    },
    guide_text=_LINKS_GUIDE + "\n\n" + _TITLE_HEADING_GUIDE,
)
