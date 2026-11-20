"""Resolve an area name, reading notes just changed on disk when the index doesn't know it yet.

The areas come from the index's frontmatter rows (`query.AreaIndex`), and the
index lags the disk by minutes, longer after a restart or a large sync. So a
new area was refused as unknown for that long after its main note was written,
by the task tools and the ledger alike (ledger F50). On a miss only, the
notes changed on disk in the last `RECENT_SECONDS` are read for an
`area_tag`, which costs a walk of the vault's file names: never on a call that
names a known area.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from src.services import conflict_copies, vault
from src.services.fork_tasks import query

#: How far back a note's change on disk is read on a miss: well past the index's lag on a quiet server
#: (a pass every 2 minutes), to cover a restart or a large sync. A main note older than this that the
#: index still lacks is not looked for, and the refusal says what to do.
RECENT_SECONDS = 3600
#: A note's frontmatter is read from at most this many bytes.
_HEAD_BYTES = 64 * 1024


def recent_rows(root: Path, now: float | None = None) -> list[tuple[str, dict]]:
    """`(vault path, frontmatter)` of the notes changed on disk in the last `RECENT_SECONDS` that declare `area_tag`.

    Changed means written or arrived here: the later of mtime and ctime, since a
    sync client keeps a note's mtime from the device that wrote it. Notes are
    the index's own (`indexer.discover_markdown_files`), sync-conflict copies
    left out (a copy is never an area's main note).

    Raises:
        OSError: the vault could not be walked.
    """
    # Imported here: the indexer imports the ledger (through ledger_embed), which imports this module.
    from src.services import indexer

    since = (time.time() if now is None else now) - RECENT_SECONDS
    rows = []
    for rel, path in indexer.discover_markdown_files(root).items():
        if conflict_copies.is_conflict_copy(os.path.basename(rel)):
            continue
        try:
            st = path.stat()
            if max(st.st_mtime, st.st_ctime) < since:
                continue
            with path.open("rb") as f:
                head = f.read(_HEAD_BYTES).decode("utf-8", errors="replace")
        except OSError:
            continue                          # gone or unreadable since the walk: not a note to read
        frontmatter, _body = vault.parse_frontmatter(head)
        if "area_tag" in frontmatter:
            rows.append((rel, frontmatter))
    return rows


async def refreshed(index: query.AreaIndex, uid: int | None) -> query.AreaIndex:
    """`index` with the notes changed on disk in the last `RECENT_SECONDS` read again: for a miss only.

    Raises:
        OSError: the vault could not be walked.
    """
    return index.with_rows(await asyncio.to_thread(recent_rows, vault._vault_root(uid)))


async def lookup(index: query.AreaIndex, name: str, uid: int | None, *, workaround: str = "") -> str:
    """`area/<slug>` for `name`, as `index.lookup`; on an unknown area, notes just changed on disk are read too.

    Args:
        index: the areas the index knows.
        name: a slug, a tag or a main note's name.
        uid: the caller, whose vault is read.
        workaround: what the caller's tool can do meanwhile when the main note is older and the index behind.

    Returns:
        The area's tag, `area/<slug>`.

    Raises:
        query.QueryError: the area is invalid, ambiguous, or unknown on disk too.
    """
    tag, _index = await resolve(index, name, uid, workaround=workaround)
    return tag


async def resolve(index: query.AreaIndex, name: str, uid: int | None, *,
                  workaround: str = "") -> tuple[str, query.AreaIndex]:
    """As `lookup`, with the index the tag was found in: `index`, or it with the notes read from disk.

    For a caller that goes on to read the area's main note or description from the index.
    """
    try:
        return index.lookup(name), index
    except query.UnknownArea as miss:
        unknown = miss
    try:
        fresh = await refreshed(index, uid)
    except OSError as exc:
        raise query.UnknownArea(
            f"{unknown}; notes changed in the last hour could not be read from disk to look for a new area "
            f"({type(exc).__name__})") from None
    try:
        return fresh.lookup(name), fresh
    except query.UnknownArea:
        minutes = RECENT_SECONDS // 60
        raise query.UnknownArea(
            f"{unknown}. Notes changed in the last {minutes} minutes were read from disk too, so a main note "
            "written just now would have been found: check the name and the main note's area_tag. If the main "
            "note is older and the index is behind (index_status)"
            + (f", {workaround}" if workaround else ", retry once it is indexed")) from None
