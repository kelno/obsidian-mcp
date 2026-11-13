"""A rule edit with no proposal ref is pointed out (`rule_change_unlogged:`).

This vault keeps its rules for agents in notes whose lines cite the proposals
that shaped them (`[P95]`): a decided change logs one proposal in the agent
ledger and adds its ref to each changed line. A write to one of those notes
that adds or changes lines outside the frontmatter, none of them carrying a
proposal ref the removed lines did not, gets a nudge in its result. Never a
refusal.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging

from src.services import ledger_citations
from src.services.note_lines import frontmatter_lines

logger = logging.getLogger(__name__)

#: The vault's rule notes: vault-relative globs (fnmatch, `*` crosses `/`).
RULE_FILE_PATTERNS = ("_/agent/*", "CLAUDE.md")
#: Changed rule lines listed in the block.
SHOWN = 5


def is_rule_note(path: str) -> bool:
    return path.lower().endswith(".md") and any(fnmatch.fnmatchcase(path, p) for p in RULE_FILE_PATTERNS)


async def unlogged_lines(path: str, old: str | None, new: str) -> list[tuple[int, str]]:
    """The rule lines `new` adds or changes when none gains a proposal ref; [] when nothing to say.

    Never raises: the nudge never fails the write it follows.
    """
    if not is_rule_note(path):
        return []
    try:
        skip = frontmatter_lines(new.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
        return await asyncio.to_thread(ledger_citations.rule_lines_unlogged, old, new, skip_leading=skip)
    except Exception:  # noqa: BLE001 — a nudge never fails the write it follows
        # debug, not warning: a caller drives this path (the D18 permit rule).
        logger.debug("rule_change_unlogged could not be computed for %s", path, exc_info=True)
        return []


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


TOOL_TEXT = (
    "**Rule edits carry their proposal.** In this vault's rules for agents (the notes under `_/agent/` and the root `CLAUDE.md`), when a "
    "write adds or changes lines (outside the frontmatter) and none of them carries a new proposal ref (`[P95]`), "
    "the result adds `rule_change_unlogged:` with those lines: log an owner's decision with `ledger_add` (kind "
    "proposal) and cite the ref it returns at the end of each changed line; a typo fix or wording that changes no "
    "meaning needs no proposal. Never a refusal."
)

WRITE_FILE_TEXT = "A rule note written with no new proposal ref gets `rule_change_unlogged:` as in `edit_note`."

GUIDE = (
    "## Agent rules (this server)\n\n"
    "This vault's rules for agents are the notes under `_/agent/` and the vault's root `CLAUDE.md`; their lines end with the refs "
    "of the proposals that shaped them (`[P54, P80]`). A write there that adds or changes lines with none of them "
    "carrying a new proposal ref gets `rule_change_unlogged:` in its result, with those lines. If the change "
    "follows a decision of the owner's, log it with `ledger_add` (kind proposal) and add the ref it returns at the "
    "end of each changed line; a typo fix or wording that changes no meaning needs nothing; a change of meaning the "
    "owner hasn't decided waits for their decision."
)
