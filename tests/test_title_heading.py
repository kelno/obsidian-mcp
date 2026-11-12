"""A first heading repeating the note's name is dropped from what a write adds."""
import pytest

from src.services import title_heading as th


@pytest.mark.parametrize("new,expected", [
    ("# Dog food\n\nBody\n", "Body\n"),
    ("## dog FOOD ##\nBody\n", "Body\n"),                         # any level, any case, closed heading
    ("   # Dog food  \n\n\nBody", "Body"),
    ("\n\n# Dog food\nBody\n", "\n\nBody\n"),                     # leading blank lines are kept as they were
    ("---\ntags: [x]\n---\n# Dog food\n\nBody\n", "---\ntags: [x]\n---\nBody\n"),
    ("# Dog food\n", ""),
    ("# Dog food\r\n\r\nBody\r\n", "Body\r\n"),
])
def test_a_title_heading_the_write_adds_is_removed(new, expected):
    text, removed = th.strip(None, new, "Areas/Dog/Dog food.md")
    assert text == expected and removed is not None


@pytest.mark.parametrize("new", [
    "# Food for the dog\nBody\n",                  # not the name
    "# **Dog food**\nBody\n",                      # formatting is not stripped
    "# [[Dog food]]\nBody\n",
    "Intro\n# Dog food\n",                         # not the first non-blank line
    "Dog food\n===\nBody\n",                       # setext: left alone
    "#Dog food\nBody\n",                           # not a heading without the space
    "```\n# Dog food\n```\n",
])
def test_left_alone(new):
    assert th.strip(None, new, "Areas/Dog/Dog food.md") == (new, None)


def test_a_title_heading_the_write_leaves_as_it_was_stays():
    old = "# Dog food\n\nBody\n"
    new = "# Dog food\n\nBody, longer\n"
    assert th.strip(old, new, "Dog food.md") == (new, None)


def test_part_of_a_dated_name_is_not_the_name():
    new = "# Call\nNotes\n"
    assert th.strip(None, new, "Log/2026-11-12 Call with the vet.md") == (new, None)


def test_render():
    assert th.render(None) == ""
    assert "title_heading_removed: the first line, `# Dog food`" in th.render("# Dog food")
    assert "would be removed" in th.render("# Dog food", dry_run=True)


def test_review_cases():
    # A BOM on the existing first line does not make the resent heading new.
    assert th.strip("﻿# B\nBody\n", "# B\nBody2\n", "B.md") == ("# B\nBody2\n", None)
    # A heading at the very end keeps the line break before it.
    assert th.strip("---\r\na: 1\r\n---\r\nOld\r\n", "---\r\na: 1\r\n---\r\n# G", "G.md")[0] == "---\r\na: 1\r\n---\r\n"
    # A BOM before a new heading.
    assert th.strip(None, "﻿# Dog food\nBody\n", "Dog food.md")[1] is not None
