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
"""

from __future__ import annotations

from urllib.parse import quote

from src.config import settings

NOTE_SUFFIX = ".md"

#: The field name every result uses for the link (a JSON field in structured
#: results, an `open_url: <url>` label in text ones).
FIELD = "open_url"


def open_note_url(path: str) -> str | None:
    """The link that opens the note at `path`, or None when there is none.

    Args:
        path: The note's vault-relative path, with its `.md` suffix.

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
    return template.replace("{path}", encoded)


def text_suffix(path: str) -> str:
    """` — open_url: <url>` to append to a text result's line for `path`, or ""."""
    url = open_note_url(path)
    return f" — {FIELD}: {url}" if url else ""
