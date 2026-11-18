"""A note write inside an area that was not opened is pointed out (`area_not_opened:`, ledger F89).

This vault's areas each have a main note whose top holds the area's rules for
agents (how freely to edit, how to report), and `open_area` returns those with
the area's ledger entries and open tasks. An agent led by a search straight to
a note inside an area's folder can write there without ever seeing them. So a
note write (`create_note`, `edit_note`, `write_file` on a `.md`, `move_note`'s
destination) inside an area's folder, when the caller has not called
`open_area` for that area in the last `WINDOW_SECONDS`, gets a nudge in its
result. Never a refusal: scripts and quick edits the owner asked for go ahead.

"The caller" is the credential (`current_principal`: the API key, or the OAuth
grant) and the user: the MCP transport is stateless, so there is no session to
key on. The map lives in this process's memory, bounded, lost on restart.
Accepted limitations in docs/architecture/open-area.md.
"""

from __future__ import annotations

import logging
import time
from pathlib import PurePosixPath

from src.auth.session import current_principal

logger = logging.getLogger(__name__)

#: How long an `open_area` call counts as having opened the area.
WINDOW_SECONDS = 3600
#: At most this many remembered `open_area` calls; the oldest go first.
MAX_ENTRIES = 10_000

#: (principal, user id, area tag) → when `open_area` last opened that area.
_opened: dict[tuple, float] = {}


def _now() -> float:
    return time.monotonic()


async def _area_index(uid):
    from src.mcp_server.tools import _area_index as index

    return await index(uid)


def clear() -> None:
    _opened.clear()


def _key(tag: str, user_id) -> tuple:
    return (current_principal.get(), user_id, tag)


def record(tag: str, user_id) -> None:
    """`open_area` opened `tag` for the current caller."""
    try:
        key = _key(tag, user_id)
        _opened.pop(key, None)
        _opened[key] = _now()
        while len(_opened) > MAX_ENTRIES:
            del _opened[next(iter(_opened))]
    except Exception:  # noqa: BLE001 — an unhashable principal: no memory, the nudge just keeps showing
        logger.debug("open_area: the opened area could not be remembered", exc_info=True)


def opened(tag: str, user_id) -> bool:
    try:
        at = _opened.get(_key(tag, user_id))
    except Exception:  # noqa: BLE001
        return False
    return at is not None and _now() - at <= WINDOW_SECONDS


async def unopened_area(path: str, user_id) -> tuple[str, str] | None:
    """`(tag, area name)` when `path` is in an area's folder the caller hasn't opened lately; else None.

    Never raises: the nudge never fails the write it follows.
    """
    try:
        index = await _area_index(user_id)
        tag = index.area_for_path(path)
        if tag is None or opened(tag, user_id):
            return None
        name = next((PurePosixPath(main).stem for t, main, _d in index.described() if t == tag), tag)
        return tag, name
    except Exception:  # noqa: BLE001 — an ambiguous folder, an index that can't be read: say nothing
        # debug, not warning: a caller drives this path (the D18 permit rule).
        logger.debug("area_not_opened could not be computed for %s", path, exc_info=True)
        return None


def render(found: tuple[str, str] | None, dry_run: bool = False) -> str:
    """The result's `area_not_opened:` block; "" for None."""
    if found is None:
        return ""
    tag, name = found
    slug = tag.removeprefix("area/")
    after = ("This was a dry run: check the change against those rules before you write it." if dry_run else
             "This write went ahead: check it against those rules, and if it breaks one, tell the owner what "
             "you wrote rather than undoing it yourself.")
    return (f"\n\narea_not_opened: this note is in the area {name} (#{tag}), and open_area hasn't been called "
            f"for it in the last hour. Call open_area(area=\"{slug}\") before more work there: it returns the "
            "area's rules for agents (how freely you may edit, how to report changes), its ledger entries and "
            "open tasks. " + after)


#: A paragraph of the guide's "Opening an area (this server)" section.
GUIDE = (
    "A search can lead straight to a note deep inside an area, past its rules. A note write inside an area's "
    "folder (`create_note`, `edit_note`, `write_file` to a `.md`, `move_note`'s destination) when `open_area` "
    "wasn't called for that area in the last hour gets `area_not_opened:` in its result, naming the area. The "
    "write has gone ahead: call `open_area`, then check what you wrote against the area's rules; if it breaks "
    "one, tell the owner what you wrote rather than undoing it yourself."
)
