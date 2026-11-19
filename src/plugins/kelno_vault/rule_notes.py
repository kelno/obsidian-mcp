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

A second nudge (`pointer_or_mention:`): a rule line the write adds or changes
that links to another rule note in none of the pointer shapes the vault sets
(`agent_paths.pointer_moment`, ledger P259) is read by the path tools as a
mention, which sends no agent to read the note; the result asks which it is.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field

from src.plugins.kelno_vault import agent_paths, conventions, rule_files
from src.services import ledger_citations
from src.services.note_lines import frontmatter_lines

logger = logging.getLogger(__name__)

#: Changed rule lines listed in a block.
SHOWN = 5


@dataclass
class RuleCheck:
    """What a write's rule lines get pointed out for, on the text as written; 1-based line numbers."""

    #: Rule lines added or changed, none gaining a proposal ref.
    unlogged: list[tuple[int, str]] = field(default_factory=list)
    #: Rule lines added or changed linking to another rule note in none of the pointer shapes: (line, links, text).
    unshaped: list[tuple[int, list[str], str]] = field(default_factory=list)
    #: A result block when the definition of a rule file could not be read, so nothing was checked.
    failure: str = ""


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


def _changed_rule_lines(path: str, old: str | None, new: str, definition: rule_files.RuleFiles) -> list[int]:
    """The 0-based numbers of the non-blank rule lines `new` holds that `old` did not (surrounding spaces ignored),
    outside the frontmatter and code fences."""
    lines = _lines(new)
    if definition.is_rule_file(path):
        numbers = list(range(frontmatter_lines(lines), len(lines)))
        old_lines = _lines(old) if old is not None else []
    else:
        if not definition.sections:
            return []
        _, numbers = _rule_part(new, definition)
        old_part, _ = _rule_part(old, definition)
        old_lines = old_part.split("\n") if old_part else []
    fenced, inside = set(), False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            inside = not inside
            fenced.add(i)
        elif inside:
            fenced.add(i)
    left = Counter(line.strip() for line in old_lines)
    out = []
    for i in numbers:
        key = lines[i].strip()
        if left[key] > 0:
            left[key] -= 1
        elif key and i not in fenced:
            out.append(i)
    return out


def _unshaped(path: str, old: str | None, new: str, definition: rule_files.RuleFiles) -> list[tuple[int, list[str], str]]:
    """The changed rule lines linking to another rule note (a rule file, or a rule section by its heading) in none
    of the pointer shapes; a link written as code, or an embed, isn't one."""
    lines = _lines(new)
    sections = {s.casefold() for s in definition.sections}
    own = path.replace("\\", "/").lstrip("/").casefold()
    out = []
    for i in _changed_rule_lines(path, old, new, definition):
        line = lines[i]
        code = [(m.start(), m.end()) for m in agent_paths.INLINE_CODE.finditer(line)]
        found = []
        for m in agent_paths.WIKILINK.finditer(line):
            if any(a <= m.start() < b for a, b in code) or line[max(m.start() - 1, 0):m.start()] == "!":
                continue
            target = m.group(1).strip()
            note = target if target.lower().endswith(".md") else f"{target}.md"
            heading = (m.group(2) or "").lstrip("#").strip().casefold()
            if note.replace("\\", "/").lstrip("/").casefold() == own:
                continue  # a link within the note
            if not (definition.is_rule_file(note) or heading in sections):
                continue
            sentence, at = agent_paths.sentence_at(line, m.start())
            if agent_paths.pointer_moment(line.strip(), sentence) is None and target not in found:
                found.append(target)
        if found:
            out.append((i + 1, found, line))
    return out


async def check(path: str, old: str | None, new: str, user_id) -> RuleCheck:
    """What the rule lines `new` adds or changes get pointed out for (`RuleCheck`).

    Never raises: a nudge never fails the write it follows.
    """
    if not path.lower().endswith(".md"):
        return RuleCheck()
    if path.replace("\\", "/").lstrip("/").casefold() == rule_files.RULE_FILES_PATH.casefold():
        # The definition itself: check the text being written, not the one it replaces.
        try:
            rule_files.parse(new)
        except rule_files.RuleFilesError as exc:
            return RuleCheck(failure=(
                f"\n\nrule_files_broken: after this write, the vault's list of rule files cannot be read "
                f"({exc}): until it is fixed, no note write is checked for rule changes. Fix it now."))
        return RuleCheck()
    try:
        definition = await asyncio.to_thread(rule_files.read, user_id)
    except Exception as exc:  # noqa: BLE001 — a nudge never fails the write it follows; the result says so
        why = str(exc) if isinstance(exc, rule_files.RuleFilesError) else type(exc).__name__
        # debug, not warning: a caller drives this path (the D18 permit rule); the result says so.
        logger.debug("rule files unreadable: %s", why)
        return RuleCheck(failure=(
            f"\n\nrule_files_unreadable: the vault's list of rule files ({rule_files.RULE_FILES_PATH}) "
            f"could not be read ({why}), so this write was not checked for rule changes. Tell the owner."))
    out = RuleCheck()
    try:
        out.unlogged = await asyncio.to_thread(_unlogged, path, old, new, definition)
    except Exception:  # noqa: BLE001 — a nudge never fails the write it follows
        # debug, not warning: a caller drives this path (the D18 permit rule).
        logger.debug("rule_change_unlogged could not be computed for %s", path, exc_info=True)
    try:
        out.unshaped = await asyncio.to_thread(_unshaped, path, old, new, definition)
    except Exception:  # noqa: BLE001 — a nudge never fails the write it follows
        logger.debug("pointer_or_mention could not be computed for %s", path, exc_info=True)
    return out


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


def render_unshaped(lines: list[tuple[int, list[str], str]], shift: int) -> str:
    """The result's `pointer_or_mention:` block, line numbers moved by `shift`; "" for no lines."""
    if not lines:
        return ""
    shown = "\n".join(f"- line {n + shift}, {', '.join(f'[[{t}]]' for t in targets)}: "
                      f"{text.strip()[:120]}{'…' if len(text.strip()) > 120 else ''}"
                      for n, targets, text in lines[:SHOWN])
    more = f"\n- and {len(lines) - SHOWN} more" if len(lines) > SHOWN else ""
    return ("\n\npointer_or_mention: these rule lines link to another rule note in none of the pointer shapes, so "
            "they are read as mentions, which send no agent to read the note:\n" + shown + more
            + "\nIs each a pointer or a mention? A mention needs nothing. A pointer opens its bullet or sentence with "
              "At the start, While, When or Before (the first word counts), or takes the guide index's shape, "
              "`[[note]]: when to read it`: reword the line. The shapes: "
              f"\"{conventions.RULE_WRITING}\" in `{conventions.RULES_NOTE}`.")


GUIDE = (
    "## Agent rules (this server)\n\n"
    f"This vault's rules for agents are the rule files `{rule_files.RULE_FILES_PATH}` lists: whole notes, such as "
    "the notes under `_/agent/` and the vault's root `CLAUDE.md`, and a `## For agents` section in any note (an "
    "area's, a folder's, a machine's or a person's), whose lines alone are rules. How their lines cite the "
    f"proposals that shaped them is the bullet \"{conventions.RULE_REFS}\" under "
    f"\"{conventions.RULE_REFS_UNDER}\" in `{conventions.RULES_NOTE}`. A write that adds "
    "or changes rule lines with none of them carrying a new proposal ref gets `rule_change_unlogged:` in its "
    "result, with those lines and what to do. One whose added or changed rule lines link to another rule note in "
    f"none of the pointer shapes (\"{conventions.RULE_WRITING}\" in `{conventions.RULES_NOTE}`) gets "
    "`pointer_or_mention:`, asking which each link is: the agent path tools read such a link as a mention."
)
