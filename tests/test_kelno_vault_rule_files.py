"""The vault's one definition of a rule file (`kelno_vault.rule_files`)."""

import pytest

from src.plugins.kelno_vault import rule_files

XDIST_PER_TEST = True

NOTE = """---
description: x
---
What counts as a rule file.

```rule-files
_/agent/*
CLAUDE.md

_/claude-code-hooks/*.py
## For agents
```

```
not/read/*
```
"""


def test_parse_reads_patterns_and_section_headings():
    definition = rule_files.parse(NOTE)
    assert definition.patterns == ("_/agent/*", "CLAUDE.md", "_/claude-code-hooks/*.py")
    assert definition.sections == ("For agents",)


def test_parse_takes_the_rule_files_block_wherever_it_is_and_any_fence():
    text = "```python\nx = 1\n```\n\n~~~~ Rule-Files\na/*\n~~~~\n"
    assert rule_files.parse(text).patterns == ("a/*",)


@pytest.mark.parametrize("text,why", [
    ("no block here\n", "no `rule-files` code block"),
    ("```rule-files\n\n```\n", "empty"),
    ("```rule-files\na/*\n", "not closed"),
    ("```rule-files\n# Title\n```\n", "neither a level-2 heading"),
    ("```rule-files\n### Deep\n```\n", "neither a level-2 heading"),
    # Typos that would otherwise match nothing, silently.
    ("```rule-files\n##For agents\n```\n", "neither a level-2 heading"),
    ("```rule-files\n- _/agent/*\n```\n", "no leading `-` or `/`"),
    ("```rule-files\n/CLAUDE.md\n```\n", "no leading `-` or `/`"),
    ("```rule-files\n_/agent/\n```\n", "no trailing `/`"),
    # A shorter fence, or one with text after it, does not close the block.
    ("````rule-files\na/*\n```\n", "not closed"),
    ("```rule-files\na/*\n``` x\n", "not closed"),
])
def test_parse_refuses_a_missing_or_unusable_block(text, why):
    with pytest.raises(rule_files.RuleFilesError, match=why):
        rule_files.parse(text)


@pytest.mark.parametrize("path,expected", [
    ("_/agent/Common-rules.md", True),
    ("_/agent/guides/coding-practices.md", True),
    ("_/Agent/Common-rules.md", True),        # case ignored
    ("CLAUDE.md", True),
    ("claude.md", True),
    ("Areas/CLAUDE.md", False),               # the root one only
    ("_/claude-code-hooks/vault-edit-guard.py", True),
    ("_/claude-code-hooks/notes.md", False),
    ("_/Rule files.md", False),
    ("Areas/Home/Home.md", False),
])
def test_is_rule_file(path, expected):
    assert rule_files.parse(NOTE).is_rule_file(path) is expected


def test_section_ranges_end_at_the_next_heading_of_level_one_or_two_and_skip_fences():
    lines = ["---", "## For agents", "---", "intro", "## For agents", "- a", "### Sub", "- b",
             "```", "## For agents", "```", "## Current position", "- c", "# For agents", "## for AGENTS", "- d"]
    # Frontmatter lines are not read; a level-3 heading stays inside; a fenced heading is no heading;
    # a level-1 heading of the same name is not a section; the last section runs to the end.
    assert rule_files.section_ranges(lines, ("For agents",), start=3) == [(5, 11), (15, 16)]


def test_section_ranges_read_fences_as_commonmark_does():
    # A longer fence holding a shorter one, and an inline triple-backtick span, don't end the section early.
    lines = ["## For agents", "````", "```", "## Inner", "```", "````", "- a", "```code``` inline",
             "## Not inside", "- z"]
    assert rule_files.section_ranges(lines, ("For agents",)) == [(1, 8)]
    # A fence with text after it doesn't close; a tilde fence isn't closed by backticks.
    lines = ["## For agents", "~~~", "```", "## Inner", "~~~ x", "~~~", "- a", "## Other"]
    assert rule_files.section_ranges(lines, ("For agents",)) == [(1, 7)]


def test_split_sections_takes_every_matching_section_out():
    text = "Top.\n\n## For agents\n\n- Rule.\n\n## Current position\n\n- Now.\n\n## For agents\n- Two.\n"
    rest, bodies = rule_files.split_sections(text, ("For agents",))
    assert rest == "Top.\n\n## Current position\n\n- Now.\n"
    assert bodies == ["\n- Rule.\n", "- Two.\n"]
    assert rule_files.split_sections("No sections.\n", ("For agents",)) == ("No sections.\n", [])
