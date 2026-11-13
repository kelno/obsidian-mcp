"""The conventions of kelno's vault, as a plugin (`PLUGINS=["kelno_vault"]`).

Fully specific to that vault and not configurable: a different vault writes
its own plugin rather than adapting this one. It holds conventions only (folder
names, naming and linking rules), never the vault's content.

What it does today: a first heading repeating the note's name is dropped from
what a note write adds (`title_heading`). The vault-specific behaviours still
in the core, and the order they move here in, are in
docs/architecture/plugins.md.
"""

from src import plugins
from src.plugins.kelno_vault import title_heading

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
    text, removed = title_heading.strip(write.old, write.new, write.path)
    if removed is None:
        return plugins.Rewrite(text)
    return plugins.Rewrite(text, lambda shift, dry_run: title_heading.render(removed, dry_run=dry_run))


plugin = plugins.Plugin(
    name="kelno_vault",
    rewrite=_rewrite,
    tool_text={
        "create_note": _TITLE_HEADING_TOOL_TEXT,
        "edit_note": _TITLE_HEADING_TOOL_TEXT,
        "write_file": "A first heading repeating the note's name is dropped as in `edit_note` (`title_heading_removed:`).",
    },
    guide_text=_TITLE_HEADING_GUIDE,
)
