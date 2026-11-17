"""Open-note links: an http(s) link that opens a note in the Obsidian app.

Agents' replies often reach the user where only http(s) links are clickable,
not `obsidian://` ones, and an agent building and percent-encoding such a URL
by hand gets it wrong. When `OPEN_NOTE_URL` is set (a template with `{path}`
exactly once, validated in `src.config`), the tools that name a note for the
agent to show the user carry the ready-made link as `open_url`. What serves
the link (typically a page redirecting to `obsidian://open?vault=…&file=…`) is
the deployment's business; this app only fills the template.

`{path}` becomes the note's vault-relative path without `.md`, percent-encoded
as UTF-8 with `/` kept as is: `Areas/Café & co #1.md` gives
`Areas/Caf%C3%A9%20%26%20co%20%231`. Every other reserved character (`?`, `#`,
`&`, `%`, `+`, `=`) is encoded, so the path cannot end the query parameter it
sits in or start a fragment.

A link to a block appends the block id as `%23%5E<id>` to `{path}`, which a
page redirecting to `obsidian://open?file=<that value>` passes through: the
app then opens the note on the line that ends with `^<id>`. Obsidian's URI
has no line parameter, so a line without a block id gets the note's link.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from src.config import settings

NOTE_SUFFIX = ".md"

#: The field name every result uses for the link (a JSON field in structured
#: results, an `open_url: <url>` label in text ones).
FIELD = "open_url"

#: An Obsidian block id: what may follow `^` at the end of a line.
BLOCK_ID_RE = re.compile(r"[A-Za-z0-9-]+")
#: A line's block link, at its end (trailing whitespace ignored), as Obsidian reads one.
_LINE_BLOCK_LINK_RE = re.compile(r"(?:^|\s)\^([A-Za-z0-9-]+)\s*\Z")


def open_note_url(path: str, block: str | None = None) -> str | None:
    """The link that opens the note at `path`, or None when there is none.

    With `block`, the link opens the note at that block (`Note#^block`,
    percent-encoded as Obsidian's URI documentation shows: `Note%23%5Eblock`),
    so a link to a task or list item lands on its line. A `block` that is
    not a valid block id is ignored: the link opens the note.

    Args:
        path: The note's vault-relative path, with its `.md` suffix.
        block: A block id, without its `^`.

    Returns:
        The filled template; None when `OPEN_NOTE_URL` is unset, when `path`
        is not a note (no `.md` suffix, or nothing before it), or when it
        holds a lone surrogate (an undecodable file name), which has no UTF-8
        encoding to percent-encode.
    """
    template = settings.open_note_url
    if not template or not path.endswith(NOTE_SUFFIX) or len(path) == len(NOTE_SUFFIX):
        return None
    try:
        encoded = quote(path[: -len(NOTE_SUFFIX)], safe="/")
    except UnicodeEncodeError:
        return None
    if block and BLOCK_ID_RE.fullmatch(block):
        encoded += "%23%5E" + block
    return template.replace("{path}", encoded)


def line_block_id(line: str | None) -> str | None:
    """The block id `line` (its first line) ends with (`… ^abc`), or None."""
    if not line:
        return None
    match = _LINE_BLOCK_LINK_RE.search(line.split("\n", 1)[0])
    return match.group(1) if match else None


def open_line_url(path: str, line: str | None) -> str | None:
    """The link that opens the note at `path` on `line` when the line ends
    with a block id, at the top of the note otherwise; None with no link."""
    return open_note_url(path, line_block_id(line))


def text_suffix(path: str) -> str:
    """` — open_url: <url>` to append to a text result's line for `path`, or ""."""
    url = open_note_url(path)
    return f" — {FIELD}: {url}" if url else ""
