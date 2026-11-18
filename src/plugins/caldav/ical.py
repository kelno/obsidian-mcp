"""The calendar tools' iCalendar work: reading occurrences, building and changing events.

Pure: no network, no settings. Times an agent gives are read in the calendar's
time zone unless they carry an offset, and every time shown is in that zone.

Changes are made on the parsed object and written back whole, so everything
the tools do not know (reminders, categories, a phone app's own `X-` fields,
other occurrences' changes) is kept as it was.

All-day events: iCalendar's `DTEND` is the day *after* the last one. The tools
speak in last days (`end` is the event's last day, included), and convert at
this boundary only.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

import icalendar

#: The `PRODID` of the events the tools create.
PRODID = "-//obsidian-mcp//caldav plugin//EN"

#: A timed event created without an end lasts this long.
DEFAULT_TIMED_LENGTH = dt.timedelta(hours=1)

_MOMENT = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[T ](?P<time>\d{2}:\d{2}(?::\d{2})?)(?P<offset>Z|[+-]\d{2}:?\d{2})?)?")

#: Properties that make an event repeat; an override of one occurrence carries none of them.
_RECURRENCE_PROPS = ("RRULE", "RDATE", "EXDATE", "EXRULE")
#: What makes an event in an expanded answer one the server left unexpanded (an EXDATE alone repeats nothing).
_UNEXPANDED_PROPS = ("RRULE", "RDATE")

#: `CLASS` values for which Nextcloud serves a calendar's sharees a stripped copy, or nothing.
_RESTRICTED_CLASSES = ("CONFIDENTIAL", "PRIVATE")

#: Fields phone and desktop apps keep beside the place and the notes, which would contradict a new one.
_PLACE_SHADOWS = ("X-APPLE-STRUCTURED-LOCATION", "GEO")
_NOTES_SHADOWS = ("X-ALT-DESC",)

Moment = dt.date | dt.datetime


class EventError(ValueError):
    """An argument or an event the tools cannot work with; the message is for the agent."""


# ── Moments ──────────────────────────────────────────────────────────────


def is_timed(value: Moment) -> bool:
    return isinstance(value, dt.datetime)


def parse_moment(text: str, tz: dt.tzinfo, what: str) -> Moment:
    """A date (`YYYY-MM-DD`) or an aware time (`YYYY-MM-DDTHH:MM[:SS]`, with an optional offset).

    A time without an offset is read in `tz`.

    Raises:
        EventError: anything else, named by `what`.
    """
    match = _MOMENT.fullmatch(text.strip())
    if not match:
        raise EventError(f"`{what}` must be a date (YYYY-MM-DD) or a time (YYYY-MM-DDTHH:MM, optionally with an "
                         f"offset such as +01:00 or Z), not {text!r}.")
    try:
        day = dt.date.fromisoformat(match["date"])
        if match["time"] is None:
            return day
        clock = dt.time.fromisoformat(match["time"])
    except ValueError as exc:
        raise EventError(f"`{what}` is not a real date or time: {text!r}.") from exc
    offset = match["offset"]
    if offset is None:
        return dt.datetime.combine(day, clock, tz)
    if offset == "Z":
        return dt.datetime.combine(day, clock, dt.timezone.utc)
    sign = 1 if offset[0] == "+" else -1
    digits = offset[1:].replace(":", "")
    delta = dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    if delta >= dt.timedelta(hours=24):
        raise EventError(f"`{what}` has an offset out of range: {text!r}.")
    return dt.datetime.combine(day, clock, dt.timezone(sign * delta))


def local(value: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    """An aware time in `tz`; a floating (naive) one is taken to be in `tz` already."""
    return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)


def format_moment(value: Moment, tz: dt.tzinfo) -> str:
    """`YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM±HH:MM` in `tz` (seconds only when not zero)."""
    if not is_timed(value):
        return value.isoformat()
    moment = local(value, tz)
    text = moment.isoformat(timespec="seconds" if moment.second else "minutes")
    return text


def day_start(day: dt.date, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(), tz)


def sort_key(value: Moment, tz: dt.tzinfo) -> dt.datetime:
    """When `value` starts, as an aware time; an all-day event at its day's midnight."""
    return local(value, tz) if is_timed(value) else day_start(value, tz)


def same_moment(a: Moment, b: Moment, tz: dt.tzinfo) -> bool:
    if is_timed(a) != is_timed(b):
        return False
    return local(a, tz) == local(b, tz) if is_timed(a) else a == b


# ── Reading ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Occurrence:
    """One event, or one occurrence of a repeating one, as stored or as the server expanded it.

    `end` is exclusive (iCalendar's); `last_day` converts an all-day one.
    """

    summary: str
    start: Moment
    end: Moment
    location: str | None
    description: str | None
    status: str | None
    recurrence_id: Moment | None
    has_attendees: bool
    #: Marked CONFIDENTIAL or PRIVATE: a sharee may see only "Busy" and the times.
    confidential: bool = False


def parse_calendar(data: str) -> icalendar.Calendar:
    """The calendar object's text, parsed.

    Raises:
        EventError: text that is not iCalendar.
    """
    try:
        return icalendar.Calendar.from_ical(data)
    except ValueError as exc:
        raise EventError(f"The event's iCalendar text could not be read ({exc}).") from exc


def _text(component, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def span(component) -> tuple[Moment, Moment]:
    """`(start, exclusive end)` of a VEVENT, from DTEND or DURATION (RFC 5545 defaults otherwise)."""
    start = component.decoded("dtstart")
    if "dtend" in component:
        end = component.decoded("dtend")
    elif "duration" in component:
        end = start + component.decoded("duration")
    else:
        end = start if is_timed(start) else start + dt.timedelta(days=1)
    return start, end


def occurrence_of(component) -> Occurrence:
    start, end = span(component)
    status = _text(component, "status")
    return Occurrence(
        summary=_text(component, "summary") or "",
        start=start, end=end,
        location=_text(component, "location"),
        description=_text(component, "description"),
        status=status.upper() if status and status.upper() != "CONFIRMED" else None,
        recurrence_id=component.decoded("recurrence-id") if "recurrence-id" in component else None,
        has_attendees="attendee" in component,
        confidential=restricted(component),
    )


def occurrences(data: str) -> tuple[list[Occurrence], bool]:
    """The occurrences in a server's expanded answer, and whether it left any event unexpanded.

    An unexpanded event (still carrying a repeat rule) gives no occurrence: listing it as one
    event would show only its first date, which may be years outside the range.

    Raises:
        EventError: an object that cannot be read (not iCalendar, an event without a start).
    """
    calendar = parse_calendar(data)
    events = calendar.walk("VEVENT")
    if any(prop in event for event in events for prop in _UNEXPANDED_PROPS):
        return [], True
    try:
        return [occurrence_of(event) for event in events], False
    except (KeyError, ValueError, TypeError) as exc:
        raise EventError(f"An event could not be read ({type(exc).__name__}).") from exc


def master(calendar: icalendar.Calendar):
    """The event itself: the VEVENT without `RECURRENCE-ID`.

    Raises:
        EventError: there is none (an object holding only changed occurrences).
    """
    for event in calendar.walk("VEVENT"):
        if "recurrence-id" not in event:
            return event
    raise EventError("This calendar object holds no event of its own (only changed occurrences of one): "
                     "change it in the calendar app.")


def repeats(event) -> bool:
    return "rrule" in event or "rdate" in event


def _rules(event) -> list:
    rule = event.get("rrule")
    return [] if rule is None else rule if isinstance(rule, list) else [rule]


def rule_text(event) -> str | None:
    """The event's repeat rule (`FREQ=WEEKLY;BYDAY=TH`), or None."""
    rules = _rules(event)
    return rules[0].to_ical().decode() if rules else None


def bounded(event) -> bool:
    """Whether a repeating event has a last occurrence: every rule has COUNT or UNTIL, or it has
    set dates (RDATE) only."""
    rules = _rules(event)
    return all("COUNT" in rule or "UNTIL" in rule for rule in rules) and (bool(rules) or "rdate" in event)


def drop_overrides_after_end(calendar: icalendar.Calendar, event) -> int:
    """Remove the changed occurrences that fall strictly after the end (`UNTIL`) of `event`'s rule,
    whose occurrences no longer exist (sabre would still list them); how many.

    An all-day series compares dates; a timed one compares instants (`UNTIL` is UTC, a
    `RECURRENCE-ID` in its own zone). One exactly at `UNTIL` is still in the series.
    """
    rules = _rules(event)
    if len(rules) != 1 or "UNTIL" not in rules[0]:
        return 0
    until = rules[0]["UNTIL"][0]
    dropped = 0
    for component in list(calendar.walk("VEVENT")):
        if "recurrence-id" not in component:
            continue
        rid = component.decoded("recurrence-id")
        if is_timed(rid) != is_timed(until) or (is_timed(rid) and (rid.tzinfo is None or until.tzinfo is None)):
            continue   # of another kind, or floating: nothing safe to compare
        if rid > until:
            calendar.subcomponents.remove(component)
            dropped += 1
    return dropped


def has_attendees(calendar: icalendar.Calendar) -> bool:
    return any("attendee" in event for event in calendar.walk("VEVENT"))


def restricted(component) -> bool:
    """Marked CONFIDENTIAL or PRIVATE (`CLASS`)."""
    return str(component.get("class", "")).strip().upper() in _RESTRICTED_CLASSES


def floating(calendar: icalendar.Calendar) -> bool:
    """Whether a time in it carries no time zone (a "floating" time: the same clock time anywhere)."""
    for event in calendar.walk("VEVENT"):
        for name in ("dtstart", "dtend", "recurrence-id"):
            if name in event:
                value = event.decoded(name)
                if is_timed(value) and value.tzinfo is None:
                    return True
    return False


def override(calendar: icalendar.Calendar, recurrence_id: Moment, tz: dt.tzinfo):
    """The VEVENT changing the occurrence `recurrence_id`, if there is one."""
    for event in calendar.walk("VEVENT"):
        if "recurrence-id" in event and same_moment(event.decoded("recurrence-id"), recurrence_id, tz):
            return event
    return None


# ── Arguments ────────────────────────────────────────────────────────────


def end_exclusive(start: Moment, end: Moment | None) -> Moment:
    """The stored end for a given `end` (an all-day event's last day, or a time).

    Raises:
        EventError: `end` of another kind than `start`, or not after it.
    """
    if end is None:
        return start + (DEFAULT_TIMED_LENGTH if is_timed(start) else dt.timedelta(days=1))
    if is_timed(start) != is_timed(end):
        raise EventError("`start` and `end` must be of the same kind: two dates (an all-day event) or two times.")
    if is_timed(start):
        if end <= start:
            raise EventError("`end` must be after `start`.")
        return end
    if end < start:
        raise EventError("`end` (the event's last day) is before `start`.")
    return end + dt.timedelta(days=1)


def last_day(start: dt.date, end_exclusive_day: dt.date) -> dt.date:
    """An all-day event's last day, from its stored (exclusive) end."""
    return max(start, end_exclusive_day - dt.timedelta(days=1))


def parse_repeat(text: str, start: Moment, tz: dt.tzinfo) -> icalendar.vRecur:
    """A repeat rule (RFC 5545 RRULE, `FREQ=WEEKLY;BYDAY=TH`), fitted to `start`.

    `UNTIL` takes the kind of `start`, as the standard requires: a date for an all-day
    event, a UTC time otherwise (a date then means the end of that day, in `tz`).

    Raises:
        EventError: a rule that does not parse or has no `FREQ`, or `COUNT` with `UNTIL`.
    """
    body = text.strip()
    if body.upper().startswith("RRULE:"):
        body = body[len("RRULE:"):]
    try:
        rule = icalendar.vRecur.from_ical(body)
    except ValueError as exc:
        raise EventError(f"`repeat` is not a repeat rule ({exc}). Give an iCalendar RRULE such as "
                         "FREQ=WEEKLY;BYDAY=TH or FREQ=MONTHLY;COUNT=6.") from exc
    if "FREQ" not in rule:
        raise EventError("`repeat` has no FREQ: give an iCalendar RRULE such as FREQ=WEEKLY;BYDAY=TH.")
    if "COUNT" in rule and "UNTIL" in rule:
        raise EventError("`repeat` may have COUNT or UNTIL, not both.")
    if "UNTIL" in rule:
        until = rule["UNTIL"][0]
        if is_timed(start) and not is_timed(until):
            rule["UNTIL"] = [dt.datetime.combine(until, dt.time(23, 59, 59), tz).astimezone(dt.timezone.utc)]
        elif is_timed(start) and until.tzinfo is None:
            rule["UNTIL"] = [until.replace(tzinfo=tz).astimezone(dt.timezone.utc)]
        elif is_timed(start):
            rule["UNTIL"] = [until.astimezone(dt.timezone.utc)]
        elif is_timed(until):
            rule["UNTIL"] = [local(until, tz).date()]
    return rule


# ── Writing ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Changes:
    """What `update_event` was asked to change. None: leave as is; `""` clears location or description."""

    summary: str | None = None
    start: Moment | None = None
    end: Moment | None = None
    location: str | None = None
    description: str | None = None
    repeat: str | None = None

    def empty(self) -> bool:
        return all(v is None for v in (self.summary, self.start, self.end, self.location, self.description,
                                       self.repeat))


def _stored(value: Moment, tz: dt.tzinfo) -> Moment:
    """A time as written: in `tz`, so it carries `tz`'s TZID (and stays right across clock changes)."""
    return local(value, tz) if is_timed(value) else value


def _set(component, name: str, value) -> None:
    component.pop(name, None)
    if value is not None:
        component.add(name, value)


def _stamp(component, now: dt.datetime) -> None:
    _set(component, "dtstamp", now)
    _set(component, "last-modified", now)


def new_calendar(uid: str, summary: str, start: Moment, end: Moment, *, location: str | None,
                 description: str | None, repeat: icalendar.vRecur | None, now: dt.datetime,
                 tz: dt.tzinfo) -> str:
    """A new event's calendar object; `end` exclusive (as `end_exclusive` returns it)."""
    calendar = icalendar.Calendar()
    calendar.add("prodid", PRODID)
    calendar.add("version", "2.0")
    event = icalendar.Event()
    event.add("uid", uid)
    event.add("summary", summary)
    event.add("dtstart", _stored(start, tz))
    event.add("dtend", _stored(end, tz))
    if location:
        event.add("location", location)
    if description:
        event.add("description", description)
    if repeat is not None:
        event.add("rrule", repeat)
    event.add("created", now)
    _stamp(event, now)
    calendar.add_component(event)
    return serialize(calendar)


def serialize(calendar: icalendar.Calendar) -> str:
    """The object's text, with a VTIMEZONE for every TZID its events use."""
    calendar.add_missing_timezones()
    return calendar.to_ical().decode("utf-8")


def apply(component, changes: Changes, tz: dt.tzinfo, now: dt.datetime) -> list[str]:
    """Change `component` (an event or an occurrence's override) as asked; the fields that changed.

    A new `start` without an `end` keeps the event's length; switching between all-day and timed
    without an `end` gives the default length (a day, an hour).

    Raises:
        EventError: an empty summary, a start and end of different kinds or out of order, a bad rule.
    """
    changed: list[str] = []
    if changes.summary is not None:
        summary = changes.summary.strip()
        if not summary:
            raise EventError("`summary` cannot be empty.")
        if summary != (_text(component, "summary") or ""):
            _set(component, "summary", summary)
            changed.append("summary")
    if changes.start is not None or changes.end is not None:
        old_start, old_end = span(component)
        start = changes.start if changes.start is not None else old_start
        if changes.end is not None:
            end = end_exclusive(start, changes.end)
        elif is_timed(start) == is_timed(old_start):
            end = start + (old_end - old_start)
        else:
            end = end_exclusive(start, None)
        if is_timed(start) and is_timed(old_start) and is_timed(end):
            same_start, same_end = local(start, tz) == local(old_start, tz), local(end, tz) == local(old_end, tz)
        else:
            same_start = not is_timed(start) and not is_timed(old_start) and start == old_start
            same_end = not is_timed(end) and not is_timed(old_end) and end == old_end
        if not same_start:
            _set(component, "dtstart", _stored(start, tz))
            changed.append("start")
        if not same_end or not same_start:
            component.pop("duration", None)
            _set(component, "dtend", _stored(end, tz))
            if not same_end:
                changed.append("end")
    for name, value, shadows in (("location", changes.location, _PLACE_SHADOWS),
                                 ("description", changes.description, _NOTES_SHADOWS)):
        if value is None:
            continue
        if value.strip() != (_text(component, name) or ""):
            _set(component, name, value.strip() or None)
            for shadow in shadows:   # an app would show its copy of the old one
                component.pop(shadow, None)
            changed.append(name)
    if changes.repeat is not None:
        start, _end = span(component)
        rule = parse_repeat(changes.repeat, start, tz)
        current = component.get("rrule")
        if isinstance(current, list) or rule.to_ical() != (current.to_ical() if current is not None else None):
            _set(component, "rrule", rule)
            changed.append("repeat")
    if changed:
        _stamp(component, now)
        # Calendar apps compare SEQUENCE to tell a newer version of an event from an older one.
        _set(component, "sequence", int(component.get("sequence", 0) or 0) + 1)
    return changed


def texts(component) -> dict[str, str | None]:
    """The title, place and notes of an event, to compare its occurrences' with."""
    return {name: _text(component, name) for name in ("summary", "location", "description")}


def carry_to_overrides(calendar: icalendar.Calendar, before: dict[str, str | None], changes: Changes,
                       tz: dt.tzinfo, now: dt.datetime) -> int:
    """Give the series' changed occurrences the series' new title, place and notes, each where the
    occurrence still had the series' old one (`before`, from `texts`); how many changed.

    An occurrence changed on purpose (its own title, its own place) keeps it.
    """
    count = 0
    for event in calendar.walk("VEVENT"):
        if "recurrence-id" not in event:
            continue
        own = texts(event)
        carried = Changes(**{name: getattr(changes, name) for name in before
                             if getattr(changes, name) is not None and own[name] == before[name]})
        if not carried.empty() and apply(event, carried, tz, now):
            count += 1
    return count


def recurrence_value(event, moment: Moment, tz: dt.tzinfo) -> Moment:
    """`moment` in the form the event's `DTSTART` uses, for `RECURRENCE-ID` and `EXDATE`.

    Raises:
        EventError: a date for a timed event, or a time for an all-day one.
    """
    start = event.decoded("dtstart")
    if is_timed(start) != is_timed(moment):
        kind = "a time (YYYY-MM-DDTHH:MM)" if is_timed(start) else "a date (YYYY-MM-DD)"
        raise EventError(f"This event's occurrences are named by {kind}, as list_events gives them in "
                         "`occurrence`.")
    if not is_timed(start):
        return moment
    if start.tzinfo is None:
        return local(moment, tz).replace(tzinfo=None)
    return moment.astimezone(start.tzinfo)


def override_for(calendar: icalendar.Calendar, event, recurrence_id: Moment, tz: dt.tzinfo):
    """The override changing occurrence `recurrence_id` of `event`: the existing one, or a new
    copy of the event at that occurrence (its reminders and other fields included)."""
    existing = override(calendar, recurrence_id, tz)
    if existing is not None:
        return existing
    copy = icalendar.Event.from_ical(event.to_ical())
    start, end = span(event)
    rid = recurrence_value(event, recurrence_id, tz)
    for name in (*_RECURRENCE_PROPS, "DTSTART", "DTEND", "DURATION", "RECURRENCE-ID"):
        copy.pop(name, None)
    copy.add("dtstart", rid)
    copy.add("dtend", rid + (end - start))
    copy.add("recurrence-id", rid)
    calendar.add_component(copy)
    return copy


def exclude(calendar: icalendar.Calendar, event, recurrence_id: Moment, tz: dt.tzinfo, now: dt.datetime) -> None:
    """Drop occurrence `recurrence_id` from `event`: an `EXDATE`, and its override, if any, removed."""
    existing = override(calendar, recurrence_id, tz)
    if existing is not None:
        calendar.subcomponents.remove(existing)
    event.add("exdate", recurrence_value(event, recurrence_id, tz))
    _stamp(event, now)
