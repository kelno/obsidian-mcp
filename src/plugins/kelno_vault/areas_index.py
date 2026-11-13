"""The vault's list of areas (`open_area.AREAS_INDEX_PATH`, `Areas/Areas.md`), read for `open_area`.

The list is one Markdown table: a row per area, with a column of related
folders outside the vault (repos, working directories) and a column linking
the area's main note. That is the only place a folder outside the vault is
tied to an area: repos never point back into the vault, so `open_area(folder=…)`
reads the link from this side.

What is read from a row:

- **name**: the first cell, as written.
- **folders**: every code span in the "Related folders" cell that holds an
  absolute path (`D:\\work\\repo`, `/srv/x`, `\\\\host\\share`); the text around
  them is description and is ignored.
- **main note**: the first wikilink in the "Vault folder" cell (`[[Areas/X/X\\|X]]`,
  the pipe escaped inside a table), as a vault path with `.md`. A row whose
  area has no folder yet has none.

The columns are found by their header names (`Related folders`, `Vault
folder`), so adding a column does not break the reading. A table that does
not have them is reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FOLDERS_HEADER = "related folders"
VAULT_HEADER = "vault folder"

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|\\#]+)")
#: A table cell separator: a pipe not escaped with a backslash.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


class AreasIndexError(Exception):
    """The list of areas could not be read as a table with the expected columns."""


@dataclass(frozen=True)
class AreaRow:
    name: str
    folders: list[str] = field(default_factory=list)
    main_note: str | None = None
    #: The "Vault folder" cell as written, for a row with no main note yet.
    vault_cell: str = ""


def _cells(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|") and not inner.endswith("\\|"):
        inner = inner[:-1]
    return [c.strip() for c in _CELL_SPLIT_RE.split(inner)]


def parse(text: str) -> list[AreaRow]:
    """Every row of the first table with both a "Related folders" and a "Vault folder" column."""
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            continue
        headers = [c.casefold() for c in _cells(line)]
        if FOLDERS_HEADER in headers and VAULT_HEADER in headers:
            folders_at, vault_at = headers.index(FOLDERS_HEADER), headers.index(VAULT_HEADER)
            rows = []
            for row_line in lines[i + 2:]:
                if not row_line.lstrip().startswith("|"):
                    break
                cells = _cells(row_line)
                if len(cells) <= max(folders_at, vault_at):
                    continue
                folders = [m.group(1).strip() for m in _CODE_SPAN_RE.finditer(cells[folders_at])
                           if _ABSOLUTE_RE.match(m.group(1).strip())]
                link = _WIKILINK_RE.search(cells[vault_at])
                main = link.group(1).strip() if link else None
                if main and not main.lower().endswith(".md"):
                    main += ".md"
                rows.append(AreaRow(cells[0], folders, main, cells[vault_at]))
            return rows
    raise AreasIndexError(
        f"no table with a {FOLDERS_HEADER!r} and a {VAULT_HEADER!r} column was found")


def normalise_folder(path: str) -> str:
    """A path compared case-insensitively, with `/` for every separator and no trailing one."""
    p = path.strip().strip('"').replace("\\", "/")
    while "//" in p[1:]:
        p = p[0] + p[1:].replace("//", "/")
    return p.rstrip("/").casefold()


def rows_for_folder(rows: list[AreaRow], folder: str) -> list[tuple[AreaRow, str]]:
    """`(row, listed folder)` for every row listing `folder` or a folder above it, deepest first."""
    wanted = normalise_folder(folder)
    hits = []
    for row in rows:
        for listed in row.folders:
            base = normalise_folder(listed)
            if base and (wanted == base or wanted.startswith(base + "/")):
                hits.append((row, listed))
    return sorted(hits, key=lambda hit: -len(normalise_folder(hit[1])))


def row_for_main_note(rows: list[AreaRow], main_note: str) -> AreaRow | None:
    wanted = main_note.casefold()
    return next((r for r in rows if r.main_note and r.main_note.casefold() == wanted), None)


def strip_section(text: str, heading: str) -> tuple[str, bool]:
    """`text` without the `## heading` section (up to the next heading of level 1 or 2).

    Headings inside fenced code are not headings. Returns `(text, removed)`.
    """
    lines = text.split("\n")
    out, skipping, removed, fence = [], False, False, None
    target = heading.strip().casefold()
    for line in lines:
        stripped = line.strip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped)
        if fence is None and fence_match:
            fence = fence_match.group(1)[0]
        elif fence is not None and fence_match and fence_match.group(1)[0] == fence:
            fence = None
        elif fence is None:
            match = re.match(r"(#{1,6})\s+(.*?)\s*#*\s*$", line)
            if match and len(match.group(1)) <= 2:
                if match.group(2).strip().casefold() == target and len(match.group(1)) == 2:
                    skipping, removed = True, True
                    continue
                skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + ("\n" if text.endswith("\n") else ""), removed


def strip_frontmatter(text: str) -> str:
    """`text` without a leading YAML frontmatter block."""
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text
