"""The plugin's wording of a `🔁` rule (`src/services/fork_tasks/recurrence.py`).

The Tasks plugin writes a rule back as rrule 2.8.1's `toText()` whenever it
rewrites a line. `RRULE_RUN` is what upstream ran through rrule 2.8.1 (its
archived plugin reference, `openspec/changes/archive/2026-11-10-task-tools/
references/tasks-plugin-reference.md`, §2a, and the plugin's own tests and
docs it cites); `FROM_TOTEXT_SOURCE` follows rrule's `totext.ts` without having
been run here (declared as T13 in docs/architecture/tasks.md).
"""

import dataclasses

import pytest

from src.services import task_recurrence as engine
from src.services.fork_tasks import recurrence

RRULE_RUN = [
    ("every day", "every day"),
    ("every 3 days", "every 3 days"),
    ("every weekday", "every weekday"),
    ("every week on Tuesday, Friday", "every week on Tuesday, Friday"),
    ("every 2 weeks", "every 2 weeks"),
    ("every month", "every month"),
    ("every month on the 15th", "every month on the 15th"),
    ("every 6 months", "every 6 months"),
    ("every January on the 15th", "every January on the 15th"),
    ("every year", "every year"),
    ("every monday", "every week on Monday"),
    ("Every Day", "every day"),
    ("every Sunday when done", "every week on Sunday when done"),   # RecurrenceField.test.ts
    ("every day when done", "every day when done"),
]

FROM_TOTEXT_SOURCE = [
    ("every week", "every week"),
    ("every 2 years", "every 2 years"),
    ("every month on the last", "every month on the last"),
    ("every month on the 1st", "every month on the 1st"),
    ("every month on the 22nd", "every month on the 22nd"),
    ("every month on the 23rd", "every month on the 23rd"),
    ("every month on the 11th", "every month on the 11th"),
    ("every Friday, Tuesday", "every week on Tuesday, Friday"),           # Monday first
    ("every week on Monday, Tuesday, Wednesday, Thursday, Friday", "every weekday"),
    ("every week on Monday, Tuesday, Wednesday, Thursday, Friday, Saturday, Sunday", "every day"),
    ("every 10 days WHEN DONE", "every 10 days when done"),
]


@pytest.mark.parametrize("given,written", RRULE_RUN + FROM_TOTEXT_SOURCE)
def test_rule_is_written_in_the_plugins_wording(given, written):
    assert recurrence.canonical(given) == written


@pytest.mark.parametrize("given,written", RRULE_RUN + FROM_TOTEXT_SOURCE)
def test_the_plugins_wording_reads_back_as_itself(given, written):
    assert recurrence.canonical(written) == written


@pytest.mark.parametrize("rule", [
    "every weekend",                  # rrule writes it back as `every week`: lossy
    "every 1 day",
    "every 3 weeks on Friday",
    "every month on the last Friday",
    "every month on the 2nd Wednesday",
    "every April and December on the 1st and 24th",
    "every day for 3 times",
    "every month on the 22th",        # wrong suffix
    "every 101 days",                 # rrule writes `every 101 day`, which upstream's parser can't read back
    "every 201 weeks",
    "every 301 months",
    "every February on the 30th",
    "each day",
    "",
])
def test_rules_outside_the_list_are_refused(rule):
    with pytest.raises(recurrence.RecurrenceUnsupported):
        recurrence.canonical(rule)


@pytest.mark.parametrize("rule", [
    engine.Rule(engine.WEEK_ON, interval=2, weekdays=frozenset({0})),
    engine.Rule(engine.MONTH_ON_DAY, interval=3, day=15),
    engine.Rule(engine.YEAR_ON, interval=2, day=15, month=1),
    engine.Rule(engine.WEEKDAY, interval=2),
    engine.Rule(engine.MONTH_ON_LAST, interval=2),
    engine.Rule(engine.DAY, interval=1, weekdays=frozenset({1})),
], ids=["2 weeks on Monday", "3 months on the 15th", "2 years on Jan 15", "2 weekdays", "2 months on the last",
        "day with a weekday"])
def test_a_rule_whose_wording_would_drop_a_field_is_refused(rule):
    # Upstream may give its kinds more fields at a resync (its tier 2:
    # `every N weeks on D`); a wording that dropped one would silently change
    # the owner's rule on the next write.
    with pytest.raises(recurrence.RecurrenceUnsupported):
        recurrence.canonical_text(rule)


def test_a_field_upstream_adds_to_rule_itself_is_kept_or_refused():
    # Its tier 2 ("every month on the 2nd Wednesday") may add a field to
    # Rule and reuse a kind; the wording carries none of it, so it is refused.
    @dataclasses.dataclass(frozen=True)
    class RuleWithNth(engine.Rule):
        nth: int | None = None

    with pytest.raises(recurrence.RecurrenceUnsupported):
        recurrence.canonical_text(RuleWithNth(engine.MONTH_ON_DAY, day=15, nth=2))


def test_every_canonical_wording_fits_the_plugins_rule_characters():
    # The serializer reads `🔁 *([a-zA-Z0-9, !]+)$`: the wording must stay inside it.
    for _, written in RRULE_RUN + FROM_TOTEXT_SOURCE:
        assert all(ch.isascii() and (ch.isalnum() or ch in ", !") for ch in written)
