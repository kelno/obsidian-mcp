"""A note's one-line description, read from its frontmatter in the index, rendered safely.

`list_notes` shows it after each note when `LIST_NOTES_DESCRIPTION_FIELD`
names the frontmatter field to read (off by default: the core serves any
vault). The `kelno_vault` plugin's `open_area` uses the same helpers for its
map of an area's notes. See "Note descriptions" in
docs/architecture/vault-tools.md.

The value is note text, so it is rendered as one JSON string literal
(`change_feed.quote`): a newline or control character in it cannot start a
line of its own and pass for the server's.
"""

from __future__ import annotations

import re

from src.services import change_feed

#: A description longer than this is cut, ending with `…`.
DESCRIPTION_MAX_CHARS = 300

_SPACES = re.compile(r"\s+")


def one_line(value: object) -> str | None:
    """The description a frontmatter value gives, or None.

    Only a string counts (a date, a list or a number is not a description);
    whitespace runs become one space, and an empty result is None.
    """
    if not isinstance(value, str):
        return None
    text = _SPACES.sub(" ", value).strip()
    if not text:
        return None
    if len(text) > DESCRIPTION_MAX_CHARS:
        text = text[:DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
    return text


def render(text: str) -> str:
    """`text` as one JSON string literal, safe to put on a result line."""
    return change_feed.quote(text)
