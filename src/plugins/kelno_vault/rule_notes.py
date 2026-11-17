"""A rule edit with no proposal ref is pointed out (`rule_change_unlogged:`).

This vault keeps its rules for agents in rule files whose lines cite the
proposals that shaped them (`[P95]`): a decided change logs one proposal in
the agent ledger and adds its ref to each changed line. What a rule file is
comes from the vault's own definition (`rule_files`, read at each write): a
note matching one of its patterns is a rule note as a whole; in any other
note, only the lines of a listed section (`## For agents`) are rules. A write
that adds or changes rule lines outside the frontmatter, none of them carrying
a proposal ref the removed rule lines did not, gets a nudge in its result.
Never a refusal.
"""

from __future__ import annotations

import asyncio
import logging

from src.plugins.kelno_vault import conventions, rule_files
from src.services import ledger_citations
from src.services.note_lines import frontmatter_lines

logger = logging.getLogger(__name__)

#: Changed rule lines listed in the block.
SHOWN = 5


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _rule_part(text: str | None, definition: rule_files.RuleFiles) -> tuple[str | None, list[int]]:
    """`(the lines of text's rule sections, joined; each one's 0-based line number in text)`."""
    if text is None:
        return None, []
    lines = _lines(text)
    numbers = [i for first, end in rule_files.section_ranges(lines, definition.sections, frontmatter_lines(lines))
               for i in range(first, end)]
    return "\n".join(lines[i] for i in numbers), numbers


def _unlogged(path: str, old: str | None, new: str, definition: rule_files.RuleFiles) -> list[tuple[int, str]]:
    if definition.is_rule_file(path):
        return ledger_citations.rule_lines_unlogged(old, new, skip_leading=frontmatter_lines(_lines(new)))
    if not definition.sections:
        return []
    old_part, _ = _rule_part(old, definition)
    new_part, numbers = _rule_part(new, definition)
    if not numbers:
        return []
    return [(numbers[n - 1] + 1, line) for n, line in ledger_citations.rule_lines_unlogged(old_part, new_part)]


async def unlogged_lines(path: str, old: str | None, new: str, user_id) -> tuple[list[tuple[int, str]], str]:
    """`(lines, failure)`: the rule lines `new` adds or changes when none gains a proposal ref.

    `failure` is a result block when the definition of a rule file could not
    be read, so nothing was checked; "" otherwise. Never raises: the nudge
    never fails the write it follows.
    """
    if not path.lower().endswith(".md"):
        return [], ""
    if path.replace("\\", "/").lstrip("/").casefold() == rule_files.RULE_FILES_PATH.casefold():
        # The definition itself: check the text being written, not the one it replaces.
        try:
            rule_files.parse(new)
        except rule_files.RuleFilesError as exc:
            return [], (f"\n\nrule_files_broken: after this write, the vault's list of rule files cannot be read "
                        f"({exc}): until it is fixed, no note write is checked for rule changes. Fix it now.")
        return [], ""
    try:
        definition = await asyncio.to_thread(rule_files.read, user_id)
    except Exception as exc:  # noqa: BLE001 — a nudge never fails the write it follows; the result says so
        why = str(exc) if isinstance(exc, rule_files.RuleFilesError) else type(exc).__name__
        # debug, not warning: a caller drives this path (the D18 permit rule); the result says so.
        logger.debug("rule files unreadable: %s", why)
        return [], (f"\n\nrule_files_unreadable: the vault's list of rule files ({rule_files.RULE_FILES_PATH}) "
                    f"could not be read ({why}), so this write was not checked for rule changes. Tell the owner.")
    try:
        return await asyncio.to_thread(_unlogged, path, old, new, definition), ""
    except Exception:  # noqa: BLE001 — a nudge never fails the write it follows
        # debug, not warning: a caller drives this path (the D18 permit rule).
        logger.debug("rule_change_unlogged could not be computed for %s", path, exc_info=True)
        return [], ""


def render(lines: list[tuple[int, str]], shift: int) -> str:
    """The result's `rule_change_unlogged:` block, line numbers moved by `shift`; "" for no lines."""
    if not lines:
        return ""
    shown = "\n".join(f"- line {n + shift}: {text[:120]}{'…' if len(text) > 120 else ''}"
                      for n, text in lines[:SHOWN])
    more = f"\n- and {len(lines) - SHOWN} more" if len(lines) > SHOWN else ""
    return ("\n\nrule_change_unlogged: these rule lines were added or changed, and none of the lines this write "
            "added or changed carries a proposal ref it didn't have before (a new one beside an old one would "
            "count):\n" + shown + more
            + "\nIf this change follows a decision of the owner's, log it with ledger_add (kind proposal) and add "
              "the ref it returns at the end of each changed line (`[P95]`, or `[P35, P95]` beside an existing "
              "one). A typo fix, or wording that changes no meaning, needs no proposal: nothing to do then. A "
              "change of meaning the owner hasn't decided waits for their decision: ask first.")


_SEE = conventions.see(conventions.RULE_REFS, conventions.RULES_NOTE)

TOOL_TEXT = (
    f"**Rule edits carry their proposal** {_SEE}. In this vault's rules for agents (the rule files "
    f"`{rule_files.RULE_FILES_PATH}` lists, such as the notes under `_/agent/` and the root `CLAUDE.md`, and a "
    "`## For agents` section in any note, whose lines alone count), when a write adds or changes rule lines "
    "(outside the frontmatter) and none of them carries a new proposal ref (`[P95]`), the result adds "
    "`rule_change_unlogged:` with those lines and what to do. Never a refusal."
)

WRITE_FILE_TEXT = "A rule note written with no new proposal ref gets `rule_change_unlogged:` as in `edit_note`."

GUIDE = (
    "## Agent rules (this server)\n\n"
    f"This vault's rules for agents are the rule files `{rule_files.RULE_FILES_PATH}` lists: whole notes, such as "
    "the notes under `_/agent/` and the vault's root `CLAUDE.md`, and a `## For agents` section in any note (an "
    "area's, a folder's, a machine's or a person's), whose lines alone are rules. How their lines cite the "
    f"proposals that shaped them is \"{conventions.RULE_REFS}\" in `{conventions.RULES_NOTE}`. A write that adds "
    "or changes rule lines with none of them carrying a new proposal ref gets `rule_change_unlogged:` in its "
    "result, with those lines and what to do."
)
