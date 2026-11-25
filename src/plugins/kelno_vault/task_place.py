"""`create_task` with an area and no path is refused: a task whose area is known goes in that area.

The vault's Tasks inbox is for tasks whose place isn't known yet (owner, 2026-11-25, ledger P422). Without a
`path`, `create_task` writes to the inbox, so a task that names its area there lands in the wrong place; the
refusal names the area's main note and its `Tasks` heading as the place to put it.
"""

from __future__ import annotations

from src import plugins

CODE = "task_area_without_path"

#: The refusal, worded by the Arborist (ledger P422); `{main}` is the area's main note.
MESSAGE = (
    "Refused: this task has an area but no path, so it would go to the Tasks inbox, which is for tasks whose "
    "place isn't known yet. Put it in that area: its main note is `{main}` (path=\"{main}\", heading=\"Tasks\"), "
    "or a note about it within the area. If you don't know where it goes, leave out the area."
)

#: Added to `create_task`'s `path` parameter (ledger P422).
PATH_TEXT = "Required with `areas`: a task whose area is known goes in that area."


async def check(place: plugins.TaskPlace) -> plugins.Refusal | None:
    """Refuse `create_task` with an area and no path, naming the first area's main note."""
    if place.tool != "create_task" or place.path is not None:
        return None
    main = next((a.main_note for a in place.areas if a.main_note), None)
    if main is None:
        # No area, or none whose main note is known (an area is always resolved from its main note, so this
        # is only an area-less task): nothing to refuse.
        return None
    return plugins.Refusal(CODE, MESSAGE.format(main=main))
