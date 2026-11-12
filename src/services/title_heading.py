"""A first heading that only repeats the note's name is dropped from what a write adds.

The vault's rule: don't start a note with a `# Title` heading repeating its
name; Obsidian already shows the file name as the title. So the note writers
(`edit_note`, `create_note`, `write_file` on a `.md`) remove, from the text
they are about to write:

- the body's first non-blank line (after the frontmatter), when it is an ATX
  heading of any level (`#` to `######`, up to three spaces before it; the
  closing `#`s of a closed heading are not part of its text) whose text
  equals the note's file name without `.md`, ignoring case and surrounding
  spaces, and when this write adds or changes that line (the lines are
  compared as the link rewrite compares them: `link_paths.changed_lines`);
- and the blank lines right after it, so the body does not start blank.

The text must equal the name exactly: no Markdown formatting is stripped
(`# **Dog food**` and `# [[Dog food]]` stay), and a heading that only
contains the name (`# Call` in `2026-11-12 Call with the vet`) stays. Setext
headings (`Title` over `===`) are left alone, and so are the body's later
headings and a first heading the write leaves as it was. The write is never
refused for this.
"""

from __future__ import annotations

import posixpath
import re

from src.services.link_paths import changed_lines, frontmatter_lines

_ATX_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(?P<text>.*?))?(?:[ \t]+#+)?[ \t]*$")


def strip(old: str | None, new: str, path: str) -> tuple[str, str | None]:
    """`(new without a title heading, the heading line removed or None)`."""
    stem = posixpath.basename(path)
    if not stem.lower().endswith(".md"):
        return new, None
    stem = stem[:-3].strip().lower()
    lines = new.split("\n")
    i = frontmatter_lines(lines)
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return new, None
    line = lines[i].rstrip("\r")
    match = _ATX_RE.match(line.lstrip("﻿"))
    if not match or (match.group("text") or "").strip().lower() != stem:
        return new, None
    if i not in changed_lines(old, new):
        return new, None
    end = i + 1
    while end < len(lines) and not lines[end].strip() and end < len(lines) - 1:
        end += 1
    # When the heading ran to the end, keep the line break after what precedes
    # it (a CRLF note would otherwise end on a bare CR).
    return "\n".join(lines[:i] + (lines[end:] or [""])), line


def render(removed: str | None, *, dry_run: bool = False) -> str:
    """The result's block, in the style of `links_rewritten:`; "" when nothing was removed."""
    if removed is None:
        return ""
    verb = "would be removed" if dry_run else "was removed"
    return (f"\n\ntitle_heading_removed: the first line, `{removed.strip()}`, only repeated the note's name, "
            f"which Obsidian already shows as its title, so it {verb} (with the blank lines after it).")
