"""The agent-edit stamp: a frontmatter property set on every agent write.

When `LAST_AI_EDIT_FIELD` is set, every tool that writes a note's text passes
the text it is about to publish through `stamp`, which sets that property to
the write's date, time and UTC offset in `TASKS_TIMEZONE`
(`2026-11-10T00:45:12+01:00`). A reader can then tell an agent's last write
from a person's: when the file's modification time is close to the stamp, the
last edit was the agent's (the vault's `Recent` note filters on exactly that).

The stamp is a **line edit**, never a re-serialisation: only the property's own
line (and its continuation lines) is replaced, or one line is appended, so the
block's comments, quoting and key order are kept. "A valid block" means what
it means everywhere else in this server: `vault.parse_frontmatter_diagnose`.

It refuses to guess. A note whose block is malformed (unclosed fence, YAML
error, not a mapping) is returned unchanged, and so is any result that does not
re-parse to the same mapping plus the stamp: a stamp is bookkeeping, and no
bookkeeping is worth a damaged note. See "The agent-edit stamp" in
docs/architecture/vault-tools.md.
"""

from __future__ import annotations

import fnmatch
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from src.config import settings
from src.services.vault import parse_frontmatter_diagnose

_MISSING = object()


def stamp_value(now: datetime | None = None) -> str:
    """The stamp for `now` (default: this instant), in the owner's zone,
    to the second."""
    zone = ZoneInfo(settings.tasks_timezone)
    moment = now.astimezone(zone) if now is not None else datetime.now(zone)
    return moment.replace(microsecond=0).isoformat()


def is_excluded(path: str) -> bool:
    """Whether the vault-relative `path` matches `LAST_AI_EDIT_EXCLUDE`.

    `fnmatch` semantics on the POSIX form, case-sensitive: `_/*` covers
    everything under `_/`, `*` crossing `/` as fnmatch does.
    """
    rel = path.replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in settings.last_ai_edit_exclude)


def enabled_for(path: str) -> bool:
    return bool(settings.last_ai_edit_field) and not is_excluded(path)


def stamp(text: str, path: str, *, now: datetime | None = None) -> str:
    """`text` with the agent-edit property set, or `text` unchanged.

    Unchanged when the stamp is off, `path` is excluded, the note's
    frontmatter is malformed, or the edited block would not re-parse to the
    original mapping plus the stamp.
    """
    if not enabled_for(path):
        return text
    return _stamp_text(text, settings.last_ai_edit_field, stamp_value(now))


def _newline_of(text: str) -> str:
    m = re.search(r"\r\n|\r|\n", text)
    return m.group(0) if m else "\n"


def _stamp_text(text: str, field: str, value: str) -> str:
    fm, body, diagnosis = parse_frontmatter_diagnose(text)
    if not diagnosis.valid:
        if diagnosis.defect is not None:
            return text  # a broken block: never stamp over it
        nl = _newline_of(text)
        candidate = f"---{nl}{field}: {value}{nl}---{nl}{text}"
        return candidate if _same_plus_stamp(candidate, {}, text, field, value) else text

    block = diagnosis.block
    first = re.match(r"---[^\S\r\n]*(\r\n|\r|\n)", block)
    if first is None:
        return text
    nl = first.group(1)
    start = first.end()
    yaml_text = block[start:start + len(diagnosis.yaml_text)]
    if yaml_text != diagnosis.yaml_text:
        return text  # the block is not laid out as expected: leave it be
    rest = block[start + len(yaml_text):]

    lines = yaml_text.splitlines(keepends=True)
    key = re.compile(re.escape(field) + r"[^\S\r\n]*:(?:[^\S\r\n]|$)")
    new_line = f"{field}: {value}{nl}"
    for i, line in enumerate(lines):
        if key.match(line):
            end = i + 1
            # The value's continuation lines (a block scalar, a nested list):
            # indented ones directly below it.
            while end < len(lines) and lines[end][:1] in (" ", "\t"):
                end += 1
            lines[i:end] = [new_line]
            break
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += nl
        lines.append(new_line)

    candidate = block[:start] + "".join(lines) + rest + body
    return candidate if _same_plus_stamp(candidate, fm, body, field, value) else text


def _same_plus_stamp(candidate: str, old_fm: dict, old_body: str, field: str, value: str) -> bool:
    """The candidate re-parses as a valid block whose mapping is the old one
    with `field` set to `value`, over the same body."""
    fm, body, diagnosis = parse_frontmatter_diagnose(candidate)
    if not diagnosis.valid or body != old_body or not _is_value(fm.get(field, _MISSING), value):
        return False
    rest_new = {k: v for k, v in fm.items() if k != field}
    rest_old = {k: v for k, v in old_fm.items() if k != field}
    return rest_new == rest_old


def _is_value(parsed, value: str) -> bool:
    """Whether the parsed property is the stamp just written. YAML reads it as
    a timestamp; the frontmatter scrub may hand it back as a string."""
    if isinstance(parsed, datetime):
        return parsed == datetime.fromisoformat(value)
    return isinstance(parsed, str) and parsed.replace(" ", "T", 1) == value
