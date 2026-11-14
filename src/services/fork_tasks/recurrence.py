"""Recurring tasks (`🔁`): the plugin's wording of a rule, and the next occurrence.

The Tasks plugin hands a rule to rrule 2.8.1 (JavaScript): `RRule.parseText`
reads it, `toText()` writes it back on every rewrite of the line, and the next
occurrence's dates come from rrule's arithmetic. No Python port reproduces
rrule exactly, so the dates come from upstream's `src/services/task_recurrence`
(#428): a closed list of rule shapes whose dates the plugin's documentation and
tests pin, refusing everything else rather than guessing. That module is used
as it is and never edited here, so upstream's fixes reach these tools at each
resync.

What this module adds is the plugin's own code around it, read in the
installed 8.6.0 bundle:

- `canonical_text`: what `Recurrence.toText()` writes for each shape. The
  wording upstream ran through rrule 2.8.1 (its archived plugin reference,
  §2a) is pinned by tests; the rest (`every week`, `every N years`,
  `every month on the last`, several weekdays, which rrule's `toText` lists
  Monday first) follows rrule's `totext.ts` and is not run against rrule here
  (`docs/architecture/tasks.md`, T13).
- `next_occurrence`: `Occurrence.next` (the reference date, in the order the
  setting "remove scheduled date on recurrence" picks; the other dates keep
  their distance from it) and `Task.createNextOccurrence` (the next status,
  the fields a new occurrence drops).

A rule outside the list raises `RecurrenceUnsupported`, so a recurring task is
never ticked without its next occurrence and never written with a wording the
plugin would not write.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from src.services import task_recurrence as _engine

#: The shapes `parse` accepts, as an agent would write them.
SUPPORTED = (
    "every day / every N days / every weekday / every week / every N weeks / "
    "every week on Monday[, Friday…] / every Monday[, Friday…] / every month / every N months / "
    "every month on the 1st…31st / every month on the last / every year / every N years / "
    "every January…December on the 1st…31st; each optionally followed by `when done`"
)


class RecurrenceUnsupported(Exception):
    """A rule, or a rule with these dates, outside what the tools can follow."""


def parse(text: str) -> _engine.Rule:
    """The rule in `text`, or `RecurrenceUnsupported` naming why not."""
    rule = _engine.parse_rule(text)
    if isinstance(rule, _engine.Unsupported):
        raise RecurrenceUnsupported(rule.reason)
    return rule


def _ordinal(k: int) -> str:
    # rrule `ToText.nth`: 1st, 2nd, 3rd, 4th … 11th, 12th, 13th, 21st, 22nd, 23rd, 31st.
    if k in (1, 21, 31):
        return f"{k}st"
    if k in (2, 22):
        return f"{k}nd"
    if k in (3, 23):
        return f"{k}rd"
    return f"{k}th"


def _every(interval: int, unit: str) -> str:
    if interval == 1:
        return f"every {unit}"
    if interval % 100 == 1:
        # rrule's `ToText.plural` is `n % 100 !== 1`, so it writes `every 101
        # day`, a wording upstream's parser does not read back.
        raise RecurrenceUnsupported(f"rrule writes an interval of {interval} in the singular")
    return f"every {interval} {unit}s"


def _meaning(rule: _engine.Rule) -> _engine.Rule:
    """What a rule says, whatever kind expresses it: every field but its text
    (so a field upstream adds to `Rule` later is compared too), with five
    weekdays as `every weekday` and all seven as `every day`, as rrule's
    `toText` writes them."""
    if rule.kind == _engine.WEEK_ON and rule.weekdays in (frozenset(range(5)), frozenset(range(7))):
        kind = _engine.WEEKDAY if len(rule.weekdays) == 5 else _engine.DAY
        rule = replace(rule, kind=kind, weekdays=frozenset())
    return replace(rule, text="")


def canonical_text(rule: _engine.Rule) -> str:
    """`Recurrence.toText()`: rrule's `toText()`, plus ` when done`.

    The wording must read back as the same rule, or the rule is refused: a
    field the wording does not carry (an interval on `every week on Monday`,
    which upstream may add at a resync) would otherwise be dropped from the
    owner's line on the next write, with no error.
    """
    text = _wording(rule)
    back = _engine.parse_rule(text)
    if isinstance(back, _engine.Unsupported) or _meaning(back) != _meaning(rule):
        raise RecurrenceUnsupported("its wording would not keep every part of the rule")
    return text


def _wording(rule: _engine.Rule) -> str:
    if rule.kind == _engine.DAY:
        text = _every(rule.interval, "day")
    elif rule.kind == _engine.WEEKDAY:
        text = "every weekday"
    elif rule.kind == _engine.WEEK:
        text = _every(rule.interval, "week")
    elif rule.kind == _engine.WEEK_ON:
        days = sorted(rule.weekdays)
        if days == [0, 1, 2, 3, 4]:
            text = "every weekday"      # `byweekday.isWeekdays`
        elif days == list(range(7)):
            text = "every day"          # `byweekday.isEveryDay`
        else:
            text = "every week on " + ", ".join(_engine.WEEKDAYS[d].capitalize() for d in days)
    elif rule.kind == _engine.MONTH:
        text = _every(rule.interval, "month")
    elif rule.kind == _engine.MONTH_ON_DAY:
        text = f"every month on the {_ordinal(rule.day)}"
    elif rule.kind == _engine.MONTH_ON_LAST:
        text = "every month on the last"
    elif rule.kind == _engine.YEAR:
        text = _every(rule.interval, "year")
    elif rule.kind == _engine.YEAR_ON:
        text = f"every {_engine.MONTHS[rule.month - 1].capitalize()} on the {_ordinal(rule.day)}"
    else:  # pragma: no cover - the engine's kinds are all handled above
        raise RecurrenceUnsupported(f"rule kind {rule.kind!r}")
    return text + (" when done" if rule.when_done else "")


def canonical(text: str) -> str:
    """The plugin's wording of the rule in `text` (`RecurrenceUnsupported` if none)."""
    return canonical_text(parse(text))


def next_occurrence(task, rule: _engine.Rule, next_status, today: date, settings):
    """`Task.createNextOccurrence(newStatus, recurrence.next(today))`.

    `task` is the task as it was before the status change (its rule already in
    the plugin's wording), `next_status` the registry's
    `getNextRecurrenceStatusOrCreate` of the new status, `today` the owner's
    day, which is also the completion date `when done` counts from.
    """
    from src.services.fork_tasks import format as tf

    if task.scheduled_inferred:
        raise RecurrenceUnsupported("its scheduled date comes from the file name, which the tools do not shift")
    dates = {"start": task.start, "scheduled": task.scheduled, "due": task.due}
    if any(d is not None and d.value is None for d in dates.values()):
        raise RecurrenceUnsupported("one of its dates is not a real calendar date")
    # `Occurrence.getDatePriorityOrder`.
    order = (("due", "start", "scheduled") if settings.remove_scheduled_date_on_recurrence
             else ("due", "scheduled", "start"))
    reference = next((dates[name].value for name in order if dates[name] is not None), None)
    shifted: dict[str, tf.TaskDate | None] = {"start": None, "scheduled": None, "due": None}
    if reference is not None:
        # `Recurrence.nextReferenceDate`: from the reference date, or from today `when done`.
        nxt = _engine.next_date(rule, today if rule.when_done else reference)
        if isinstance(nxt, _engine.Unsupported):
            raise RecurrenceUnsupported(nxt.reason)
        try:
            for name, value in dates.items():
                if value is not None:
                    shifted[name] = tf.TaskDate.of(value.value + (nxt - reference))
        except OverflowError:
            raise RecurrenceUnsupported("the next date is out of range") from None
        # `Occurrence.next`: the setting drops ⏳ only when 🛫 or 📅 is there.
        if settings.remove_scheduled_date_on_recurrence and (task.start is not None or task.due is not None):
            shifted["scheduled"] = None
    return replace(
        task,
        **shifted,
        status=next_status,
        block_link="",
        id="",
        depends_on=(),
        created=tf.TaskDate.of(today) if settings.set_created_date else None,
        cancelled=None,
        done=None,
    )
