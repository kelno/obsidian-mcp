"""The calendar tools: `list_calendars`, `list_events`, `create_event`, `update_event`, `delete_event`.

Tools of the `caldav` plugin (`Plugin.tools`), registered by the core, which
wraps each in `_tracked` (rate buckets, quota, usage row); the three writers
draw on the write budget and need write permission like the note writers.

Each call reads the settings (`config`), opens one connection to the CalDAV
server (`client`), lists the calendars this login sees, keeps those
`CALDAV_CALENDARS` names (all when unset), and works on those only: an event
id naming another calendar is refused before any request reaches it.

A repeating event is never changed or deleted whole by accident: on one, the
writers need `occurrence` (that occurrence only) or `all_occurrences=true`.
An event with attendees is left to the owner, since changing it from a shared
calendar may send its attendees mail.

This module must not use `from __future__ import annotations`: the server
reads the tools' signatures through the core's wrapper (docs/architecture/plugins.md).
"""

import asyncio
import dataclasses
import datetime as dt
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import unquote

from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from src import plugins
from src.mcp_server.tools import _require_write
from src.plugins.caldav import client, config, ical
from src.plugins.caldav.result import (
    CalendarEntry,
    CalendarListResult,
    EventEntry,
    EventListResult,
    EventWriteResult,
)
from src.services.tool_outcomes import body_refusal

logger = logging.getLogger(__name__)

#: `list_events` without `end` lists this many days from `start`.
DEFAULT_RANGE_DAYS = 7
#: The longest range one `list_events` call may ask for.
MAX_RANGE_DAYS = 366
#: `list_events`' `limit`: default and maximum.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
#: A description in `list_events` is cut past this many characters.
DESCRIPTION_MAX_CHARS = 2000

#: One object name in an event id: a single path segment as the server spells it (percent-encoded).
_OBJECT_NAME = re.compile(r"[A-Za-z0-9._~%@+=,:;!$&'()*-]+")


def _now() -> dt.datetime:
    """Now, in UTC (tests replace it)."""
    return dt.datetime.now(dt.timezone.utc)


class _Refusal(Exception):
    """A refusal raised inside a tool body: its code and the message for the agent."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _refused(result_type: type, code: str, message: str):
    outcome = body_refusal(message, code)
    result = result_type(error=str(outcome))
    result._body_outcome = outcome
    return result


async def _guarded(result_type: type, body):
    """Run a tool body; a `_Refusal`, a CalDAV error or an unreadable event becomes its refusal."""
    try:
        return await body()
    except _Refusal as exc:
        return _refused(result_type, exc.code, exc.message)
    except client.CalDavError as exc:
        return _refused(result_type, exc.code, exc.message)
    except ical.EventError as exc:
        return _refused(result_type, "invalid_argument", str(exc))


# ── Settings and the connection ─────────────────────────────────────────────


def _settings() -> tuple[config.CalDavSettings, dt.tzinfo]:
    """The settings and time zone, or a `calendar_not_configured` refusal naming what is wrong."""
    try:
        cfg = config.current()
    except ValidationError as exc:
        fields = ", ".join(sorted({"CALDAV_" + str(e["loc"][0]).upper() for e in exc.errors() if e.get("loc")}))
        raise _Refusal("calendar_not_configured", f"The calendar tools are not set up: {fields or 'a setting'} "
                       "has a value of the wrong type. Tell the owner.") from exc
    found = config.problems(cfg)
    if found:
        raise _Refusal("calendar_not_configured", "The calendar tools are not set up on this server: "
                       + "; ".join(found) + ". Tell the owner; nothing was read or written.")
    return cfg, config.zone(cfg)


@dataclass
class _View:
    """The calendars this call works with, in order, and what the result should say about the rest."""

    calendars: list[client.CalendarInfo]
    default: client.CalendarInfo | None
    notes: list[str]


def _names(calendars) -> str:
    return ", ".join(c.name for c in calendars) or "none"


#: A trailing ` (…)` a server adds to a shared calendar's name: Nextcloud shows "Patipa (kelno)".
_OWNER_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")


def _matching(calendars: list[client.CalendarInfo], given: str) -> list[client.CalendarInfo]:
    """The calendars `given` names: its id; else its name (any case); else its name without the
    ` (owner)` a shared calendar's name carries, so "Patipa" finds "Patipa (kelno)"."""
    text = given.strip()
    for found in ([c for c in calendars if c.id == text],
                  [c for c in calendars if c.name.casefold() == text.casefold()],
                  [c for c in calendars if _OWNER_SUFFIX.sub("", c.name).casefold() == text.casefold()]):
        if found:
            return found
    return []


def _view(found: list[client.CalendarInfo], wanted: list[str]) -> _View:
    events = [c for c in found if c.events]
    task_lists = [c for c in found if not c.events]
    notes = []
    if task_lists:
        notes.append(f"Left out because they hold only tasks, not events: {_names(task_lists)}.")
    if not wanted:
        shown = sorted(events, key=lambda c: (c.name.casefold(), c.id))
    else:
        shown, missing = [], []
        for name in wanted:
            found = _matching(events, name)
            if not found:
                missing.append(name)
            shown += [c for c in found if c not in shown]
        if missing:
            notes.append(f"The server's CALDAV_CALENDARS setting names {', '.join(missing)}, not found among this "
                         "login's calendars (not shared with it, or renamed): tell the owner if one was expected.")
        hidden = len(events) - len(shown)
        if hidden:
            notes.append(f"{hidden} other calendar{'s' if hidden > 1 else ''} of this login "
                         f"{'are' if hidden > 1 else 'is'} hidden by the server's CALDAV_CALENDARS setting.")
    writable = [c for c in shown if c.writable is not False]
    default = (writable[0] if writable else None) if wanted else (writable[0] if len(writable) == 1 else None)
    return _View(shown, default, notes)


async def _open_view(dav: client.Client, cfg: config.CalDavSettings) -> _View:
    return _view(await dav.calendars(), cfg.calendars)


def _pick(view: _View, given: str) -> client.CalendarInfo:
    """The calendar `given` names (`_matching`), refused when none or several do."""
    text = given.strip()
    named = _matching(view.calendars, text)
    if len(named) > 1:
        raise _Refusal("invalid_argument", f"Several calendars are named {text!r}: give one by its id "
                       f"({', '.join(c.id for c in named)}).")
    if not named:
        raise _Refusal("not_found", f"No calendar named {text!r}. The calendars are: {_names(view.calendars)}.")
    return named[0]


def _writable(view: _View, calendar: client.CalendarInfo) -> None:
    if calendar.writable is False:
        others = [c for c in view.calendars if c.writable is not False]
        raise _Refusal("calendar_read_only", f"The calendar {calendar.name!r} is read-only for this login; "
                       f"nothing was written. Writable calendars: {_names(others)}.")


def _event_id(calendar: client.CalendarInfo, href: str) -> str:
    return f"{calendar.id}/{href[len(calendar.href):]}"


def _check_event_id(event: str) -> tuple[str, str]:
    """`(calendar id, object name)` from an event id, checked before any request."""
    parts = event.strip().split("/")
    if len(parts) != 2 or not all(parts) or any(p in (".", "..") for p in parts) \
            or not _OBJECT_NAME.fullmatch(parts[1]) or unquote(parts[1]) in (".", "..") \
            or any(ch in unquote(parts[1]) for ch in "/\\"):
        raise _Refusal("invalid_argument", f"`event` must be an event id as list_events gives it "
                       f"(`<calendar id>/<name>`), not {event!r}.")
    return parts[0], parts[1]


def _locate(view: _View, cal_id: str, name: str) -> tuple[client.CalendarInfo, str]:
    calendar = next((c for c in view.calendars if c.id == cal_id), None)
    if calendar is None:
        raise _Refusal("not_found", f"No calendar with the id {cal_id!r} among this login's calendars "
                       f"({', '.join(c.id for c in view.calendars) or 'none'}). Use the `event` list_events gives.")
    return calendar, client.object_href(calendar, name)


# ── Entries ──────────────────────────────────────────────────────────────


def _entry(occ: ical.Occurrence, calendar: client.CalendarInfo, event_id: str, tz: dt.tzinfo, *,
           repeat: str | None = None, occurrence: ical.Moment | None = None, cut: bool = False) -> EventEntry:
    all_day = not ical.is_timed(occ.start)
    end = ical.last_day(occ.start, occ.end) if all_day and not ical.is_timed(occ.end) else occ.end
    description, truncated = occ.description, None
    if cut and description and len(description) > DESCRIPTION_MAX_CHARS:
        description, truncated = description[:DESCRIPTION_MAX_CHARS - 1] + "…", True
    rid = occurrence if occurrence is not None else occ.recurrence_id
    return EventEntry(
        calendar=calendar.name, event=event_id,
        occurrence=ical.format_moment(rid, tz) if rid is not None else None,
        summary=occ.summary, weekday=ical.sort_key(occ.start, tz).strftime("%A"),
        start=ical.format_moment(occ.start, tz), end=ical.format_moment(end, tz),
        all_day=True if all_day else None, location=occ.location, description=description,
        description_truncated=truncated, repeat=repeat, status=occ.status,
        has_attendees=True if occ.has_attendees else None, confidential=True if occ.confidential else None)


def _matches(occ: ical.Occurrence, text: str) -> bool:
    needle = text.casefold()
    return any(needle in (field or "").casefold() for field in (occ.summary, occ.location, occ.description))


# ── list_calendars ───────────────────────────────────────────────────────


async def list_calendars_impl() -> CalendarListResult:
    async def body():
        cfg, _tz = _settings()
        async with client.Client(cfg) as dav:
            view = await _open_view(dav, cfg)
        notes = list(view.notes)
        if view.default is None:
            notes.append("No calendar takes new events by default: create_event needs `calendar`.")
        if not cfg.calendars:
            # Nextcloud gives every new user an empty calendar of its own ("Personal").
            notes.append("The server's CALDAV_CALENDARS setting is not set, so every calendar of this login is "
                         "listed. If this login is not the owner's own account, events in its own calendars are "
                         "seen by no one else: ask the owner which calendar to use before adding an event, and "
                         "suggest they set CALDAV_CALENDARS.")
        return CalendarListResult(
            time_zone=config.zone_name(cfg),
            calendars=[CalendarEntry(name=c.name, id=c.id, writable=c.writable, color=c.color,
                                     description=c.description,
                                     default_for_new_events=True if c is view.default else None)
                       for c in view.calendars],
            notes=notes or None)

    return await _guarded(CalendarListResult, body)


# ── list_events ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Range:
    """The range to list: aware times for the query, and how to show it (dates stay dates)."""

    begin: dt.datetime
    finish: dt.datetime
    shown_start: str
    shown_end: str


def _range(start: str | None, end: str | None, tz: dt.tzinfo) -> _Range:
    """Dates are whole days, `end`'s day included; a time `end` is excluded."""
    first = ical.parse_moment(start, tz, "start") if start is not None else _now().astimezone(tz).date()
    begin = first if ical.is_timed(first) else ical.day_start(first, tz)
    if end is None:
        last = first + dt.timedelta(days=DEFAULT_RANGE_DAYS) if ical.is_timed(first) \
            else first + dt.timedelta(days=DEFAULT_RANGE_DAYS - 1)
    else:
        last = ical.parse_moment(end, tz, "end")
    finish = last if ical.is_timed(last) else ical.day_start(last + dt.timedelta(days=1), tz)
    if finish <= begin:
        raise _Refusal("invalid_argument", "`end` is before `start` (a date `end` is the last day listed).")
    if finish - begin > dt.timedelta(days=MAX_RANGE_DAYS):
        raise _Refusal("invalid_argument", f"The range is longer than {MAX_RANGE_DAYS} days: list it in parts.")
    return _Range(begin, finish, ical.format_moment(first, tz), ical.format_moment(last, tz))


#: How far past each end of a range the server is asked. sabre (Nextcloud) matches an all-day event
#: against a range at midnight UTC, not in the calendar's zone: asked for a day in Brussels, it also
#: answers with the day before's, and west of UTC it misses an evening's. The tools ask wider and
#: keep what really overlaps the range (`_within`).
_SERVER_MARGIN = dt.timedelta(days=1)


def _within(occ: ical.Occurrence, begin: dt.datetime, finish: dt.datetime, tz: dt.tzinfo) -> bool:
    """Whether `occ` overlaps `[begin, finish)` as RFC 4791 §9.9 has it, an all-day event by `tz`'s days."""
    start, end = ical.sort_key(occ.start, tz), ical.sort_key(occ.end, tz)
    return start < finish and (end > begin if end > start else start >= begin)


@dataclass
class _Found:
    """What the server's expansion of one calendar gave over a range."""

    occurrences: list[tuple[ical.Occurrence, str]]   # (occurrence, its object's href)
    unexpanded: int = 0     # objects the server left unexpanded (a repeat rule still in them)
    unreadable: int = 0     # objects that could not be read


async def _occurrences(dav: client.Client, calendar: client.CalendarInfo, begin: dt.datetime,
                       finish: dt.datetime, tz: dt.tzinfo) -> _Found:
    """The occurrences in `calendar` overlapping `[begin, finish)`, as the server expands them."""
    objects = await dav.expanded(calendar, begin - _SERVER_MARGIN, finish + _SERVER_MARGIN)
    # Reading thousands of events takes seconds: off the event loop, so no other call waits on it.
    return await asyncio.to_thread(_read_found, objects, begin, finish, tz)


def _read_found(objects: list[client.CalendarObject], begin: dt.datetime, finish: dt.datetime,
                tz: dt.tzinfo) -> _Found:
    found = _Found([])
    for obj in objects:
        try:
            occs, left = ical.occurrences(obj.data)
        except ical.EventError:
            found.unreadable += 1
            continue
        found.unexpanded += left
        found.occurrences += [(occ, obj.href) for occ in occs if _within(occ, begin, finish, tz)]
    return found


async def _calendar_events(dav: client.Client, calendar: client.CalendarInfo, begin: dt.datetime,
                           finish: dt.datetime, tz: dt.tzinfo) -> tuple[_Found, dict[str, str]]:
    """One calendar's occurrences over the range, and `{href: repeat rule}` for the repeating ones."""
    found = await _occurrences(dav, calendar, begin, finish, tz)
    repeating = sorted({href for occ, href in found.occurrences if occ.recurrence_id is not None})
    rules = {}
    if repeating:
        for obj in await dav.multiget(calendar, repeating):
            try:
                rule = ical.rule_text(ical.master(ical.parse_calendar(obj.data)))
            except ical.EventError:
                rule = None   # only changed occurrences stored, or unreadable: they still list, without a rule
            if rule:
                rules[obj.href] = rule
    return found, rules


async def list_events_impl(start: str | None = None, end: str | None = None, calendar: str | None = None,
                           text: str | None = None, limit: int = DEFAULT_LIMIT) -> EventListResult:
    async def body():
        cfg, tz = _settings()
        if not 1 <= limit <= MAX_LIMIT:
            raise _Refusal("invalid_argument", f"`limit` must be 1 to {MAX_LIMIT}.")
        span = _range(start, end, tz)
        begin, finish = span.begin, span.finish
        async with client.Client(cfg) as dav:
            view = await _open_view(dav, cfg)
            chosen = [_pick(view, calendar)] if calendar is not None else view.calendars
            per_calendar = await asyncio.gather(*(_calendar_events(dav, c, begin, finish, tz) for c in chosen),
                                                return_exceptions=True)
        failed = [(cal, exc) for cal, exc in zip(chosen, per_calendar) if isinstance(exc, BaseException)]
        for _cal, exc in failed:
            if not isinstance(exc, client.CalDavError):
                raise exc
        if failed and len(failed) == len(chosen):
            raise failed[0][1]
        notes = [f"The calendar {cal.name!r} could not be read, so its events are missing here: {exc.message}"
                 for cal, exc in failed]   # which calendars are left out on purpose is list_calendars' to say
        entries, unexpanded = [], 0
        for cal, outcome in zip(chosen, per_calendar):
            if isinstance(outcome, BaseException):
                continue
            found, rules = outcome
            unexpanded += found.unexpanded
            if found.unreadable:
                notes.append(f"{found.unreadable} event{'s' if found.unreadable > 1 else ''} in {cal.name!r} could "
                             "not be read and " + ("are" if found.unreadable > 1 else "is") + " left out: tell the "
                             "owner if one is missing.")
            for occ, href in found.occurrences:
                if text and not _matches(occ, text):
                    continue
                entry = _entry(occ, cal, _event_id(cal, href), tz, repeat=rules.get(href), cut=True)
                entries.append((ical.sort_key(occ.start, tz), ical.is_timed(occ.start), occ.summary.casefold(),
                                entry))
        entries.sort(key=lambda item: item[:3])
        if unexpanded:
            notes.append(f"{unexpanded} repeating event{'s' if unexpanded > 1 else ''} could not be listed: the "
                         "calendar server did not expand them into occurrences (CalDAV `expand`). Tell the owner.")
        total = len(entries)
        if total > limit:
            notes.append(f"Only the first {limit} of {total} events are listed (`limit`): narrow the range, or "
                         "list the rest from the last one's start.")
        return EventListResult(
            time_zone=config.zone_name(cfg), start=span.shown_start, end=span.shown_end,
            events=[e[3] for e in entries[:limit]],
            truncated=True if total > limit else None, total=total if total > limit else None,
            notes=notes or None)

    return await _guarded(EventListResult, body)


# ── create_event ─────────────────────────────────────────────────────────


def _write_refusal() -> EventWriteResult | None:
    outcome = _require_write()
    if outcome is None:
        return None
    result = EventWriteResult(error=str(outcome))
    result._body_outcome = outcome
    return result


async def _duplicate(dav: client.Client, calendar: client.CalendarInfo, summary: str, start: ical.Moment,
                     end: ical.Moment, tz: dt.tzinfo) -> EventEntry | None:
    """The event in `calendar` with the same title starting at the same moment, if any."""
    begin = ical.sort_key(start, tz)
    finish = max(ical.sort_key(end, tz), begin + dt.timedelta(minutes=1))
    for occ, href in (await _occurrences(dav, calendar, begin, finish, tz)).occurrences:
        if occ.summary.strip().casefold() == summary.casefold() and ical.same_moment(occ.start, start, tz):
            return _entry(occ, calendar, _event_id(calendar, href), tz)
    return None


def _said(entry: EventEntry) -> str:
    """An event in one phrase: `'Plumber' on Tuesday 2026-11-17T09:00+01:00 to …, at Home`."""
    place = f", at {entry.location}" if entry.location else ""
    return f"{entry.summary!r} on {entry.weekday} {entry.start} to {entry.end}{place}"


async def create_event_impl(summary: str, start: str, end: str | None = None, calendar: str | None = None,
                            location: str | None = None, description: str | None = None,
                            repeat: str | None = None) -> EventWriteResult:
    refusal = _write_refusal()
    if refusal is not None:
        return refusal

    async def body():
        cfg, tz = _settings()
        title = summary.strip()
        if not title:
            raise _Refusal("invalid_argument", "`summary` (the event's title) is empty.")
        first = ical.parse_moment(start, tz, "start")
        stored_end = ical.end_exclusive(first, ical.parse_moment(end, tz, "end") if end is not None else None)
        rule = ical.parse_repeat(repeat, first, tz) if repeat is not None and repeat.strip() else None
        async with client.Client(cfg) as dav:
            view = await _open_view(dav, cfg)
            if calendar is not None:
                target = _pick(view, calendar)
            elif view.default is not None:
                target = view.default
            else:
                raise _Refusal("invalid_argument", "No calendar was named and none takes new events by default: "
                               "give `calendar`. Writable calendars: "
                               f"{_names([c for c in view.calendars if c.writable is not False])}.")
            _writable(view, target)
            twin = await _duplicate(dav, target, title, first, stored_end, tz)
            if twin is not None:
                raise _Refusal("duplicate_event", f"{target.name!r} already has {_said(twin)} (event {twin.event}); "
                               "nothing was written. If it is the one you meant, it is already there; otherwise "
                               "change it with update_event.")
            uid = str(uuid.uuid4())
            href = client.object_href(target, client.new_object_name(uid))
            data = ical.new_calendar(uid, title, first, stored_end, location=(location or "").strip() or None,
                                     description=(description or "").strip() or None, repeat=rule, now=_now(),
                                     tz=tz)
            await dav.put(href, data, create=True)
        written = ical.master(ical.parse_calendar(data))
        logger.info("caldav: created an event in %s", target.id)
        return EventWriteResult(action="created", event=_entry(ical.occurrence_of(written), target,
                                                               _event_id(target, href), tz,
                                                               repeat=ical.rule_text(written)))

    return await _guarded(EventWriteResult, body)


# ── update_event and delete_event ───────────────────────────────────────


async def _occurrence_exists(dav: client.Client, calendar: client.CalendarInfo, href: str, stored,
                             moment: ical.Moment, tz: dt.tzinfo) -> bool:
    """Whether the repeating event at `href` has an occurrence starting (originally) at `moment`.

    Asked of the server, whose expansion `list_events` shows, around that moment; an
    occurrence already moved elsewhere is found by its override in the stored object.
    """
    if ical.override(stored, moment, tz) is not None:
        return True
    around = ical.sort_key(moment, tz)
    found = await _occurrences(dav, calendar, around - dt.timedelta(days=1), around + dt.timedelta(days=2), tz)
    return any(found_href == href and occ.recurrence_id is not None
               and ical.same_moment(occ.recurrence_id, moment, tz) for occ, found_href in found.occurrences)


@dataclass
class _Target:
    """The event a write works on, read from the server."""

    dav: client.Client
    view: _View
    calendar: client.CalendarInfo
    href: str
    etag: str | None
    stored: object          # the parsed icalendar.Calendar
    event: object           # its own VEVENT (the series, for a repeating one)

    def refuse_if_floating(self, what: str) -> None:
        """Refuse to `what` (change it, delete one of its occurrences) an event whose times have no zone.

        The server reads such a time in a zone of its choosing (the calendar's, else UTC), so an
        `EXDATE` or `RECURRENCE-ID` written in the calendar's zone may miss, silently.
        """
        if ical.floating(self.stored):
            raise _Refusal("event_not_editable", "This event's times carry no time zone (\"floating\" times), so "
                           f"the calendar server and the calendar tools may not agree on when it is: they do not {what}. "
                           "Ask the owner to change it in their calendar app. Nothing was changed.")


async def _read_target(dav: client.Client, cfg: config.CalDavSettings, cal_id: str, name: str) -> _Target:
    view = await _open_view(dav, cfg)
    calendar, href = _locate(view, cal_id, name)
    _writable(view, calendar)
    obj = await dav.get(href)
    try:
        stored = ical.parse_calendar(obj.data)
        event = ical.master(stored)
    except ical.EventError as exc:
        raise _Refusal("invalid_argument", str(exc)) from exc
    if any(ical.restricted(component) for component in stored.walk("VEVENT")):
        # Nextcloud serves a sharee a confidential event stripped (as "Busy", no place, notes or
        # reminders) under the whole event's ETag: a PUT of it would erase the rest, and be accepted.
        raise _Refusal("event_not_editable", "The owner marked this event confidential or private: the calendar "
                       "server shows this login only part of it (as \"Busy\", without its place, notes or "
                       "reminders), and writing it back would erase the rest. The calendar tools leave it alone; "
                       "ask the owner to change it in their calendar app. Nothing was changed.")
    if ical.has_attendees(stored):
        raise _Refusal("event_has_attendees", "This event has attendees: changing or deleting it from here may "
                       "send them mail from the calendar server, so the calendar tools leave it alone. Ask the "
                       "owner to change it in their calendar app.")
    return _Target(dav, view, calendar, href, obj.etag, stored, event)


def _scope(target: _Target, occurrence: str | None, all_occurrences: bool, tz: dt.tzinfo,
           verb: str) -> ical.Moment | None:
    """The occurrence a write is about (None: the whole event), refused when the call is unclear."""
    if occurrence is not None and all_occurrences:
        raise _Refusal("invalid_argument", "Give `occurrence` or `all_occurrences`, not both.")
    if not ical.repeats(target.event):
        if occurrence is not None or all_occurrences:
            raise _Refusal("invalid_argument", "This event does not repeat: leave out `occurrence` and "
                           "`all_occurrences`.")
        return None
    if occurrence is None and not all_occurrences:
        how = ical.rule_text(target.event) or "on set dates"
        stop = (" To stop it from a date on and keep its past, use update_event with all_occurrences=true and "
                "`repeat` set to its rule with UNTIL=<its last day> (in place of any COUNT)."
                if verb == "delete" and ical.rule_text(target.event) else "")
        raise _Refusal("occurrence_required", f"This event repeats ({how}): to {verb} one occurrence, give "
                       f"`occurrence` (as list_events gives it); to {verb} every occurrence, past ones included, "
                       f"give all_occurrences=true.{stop} Nothing was changed.")
    if all_occurrences:
        return None
    return ical.parse_moment(occurrence, tz, "occurrence")


async def _check_occurrence(target: _Target, moment: ical.Moment, tz: dt.tzinfo) -> None:
    ical.recurrence_value(target.event, moment, tz)   # a date for a timed event, or the reverse, is refused here
    if not await _occurrence_exists(target.dav, target.calendar, target.href, target.stored, moment, tz):
        raise _Refusal("not_found", f"This event has no occurrence starting at {ical.format_moment(moment, tz)} "
                       "(or it was deleted). list_events gives each occurrence's `occurrence`.")


async def update_event_impl(event: str, occurrence: str | None = None, all_occurrences: bool = False,
                            summary: str | None = None, start: str | None = None, end: str | None = None,
                            location: str | None = None, description: str | None = None,
                            repeat: str | None = None) -> EventWriteResult:
    refusal = _write_refusal()
    if refusal is not None:
        return refusal

    async def body():
        cal_id, name = _check_event_id(event)
        cfg, tz = _settings()
        changes = ical.Changes(
            summary=summary, location=location, description=description, repeat=repeat,
            start=ical.parse_moment(start, tz, "start") if start is not None else None,
            end=ical.parse_moment(end, tz, "end") if end is not None else None)
        if changes.empty():
            raise _Refusal("invalid_argument", "Nothing to change: give at least one of summary, start, end, "
                           "location, description or repeat.")
        async with client.Client(cfg) as dav:
            target = await _read_target(dav, cfg, cal_id, name)
            target.refuse_if_floating("change it")
            moment = _scope(target, occurrence, all_occurrences, tz, "change")
            carried = 0
            if moment is not None:
                if changes.repeat is not None:
                    raise _Refusal("invalid_argument", "`repeat` changes the whole series: give "
                                   "all_occurrences=true instead of `occurrence`.")
                await _check_occurrence(target, moment, tz)
                component = ical.override_for(target.stored, target.event, moment, tz)
            else:
                if ical.repeats(target.event) and (changes.start is not None or changes.end is not None):
                    raise _Refusal("invalid_argument", "Moving every occurrence of a repeating event, or every one "
                                   "from a date on, is not supported: change one occurrence (`occurrence`), or tell "
                                   "the owner to change the series in their calendar app (ending it and creating a "
                                   "new one would not carry over its reminders). Nothing was changed.")
                component = target.event
            before = ical.texts(component)
            now = _now()
            changed = ical.apply(component, changes, tz, now)
            dropped = 0
            if moment is None and ical.repeats(target.event):
                # Occurrences changed earlier are VEVENTs of their own: they take the series' new
                # title, place and notes where they still had its old ones.
                carried = ical.carry_to_overrides(target.stored, before, changes, tz, now)
                if "repeat" in changed:
                    # A series ended earlier: its changed occurrences after the new end no longer exist.
                    dropped = ical.drop_overrides_after_end(target.stored, target.event)
            if changed or carried:
                await dav.put(target.href, ical.serialize(target.stored), etag=target.etag)
                logger.info("caldav: updated an event in %s (%s)", target.calendar.id, ", ".join(changed))
        entry = _entry(ical.occurrence_of(component), target.calendar, _event_id(target.calendar, target.href), tz,
                       repeat=ical.rule_text(component) if moment is None else None, occurrence=moment)
        notes = []
        if carried:
            notes.append(f"{carried} occurrence{'s' if carried > 1 else ''} changed earlier took the change too; "
                         "a title, place or notes given to one occurrence on purpose is kept.")
        if dropped:
            notes.append(f"{dropped} occurrence{'s' if dropped > 1 else ''} changed earlier fell after the series' "
                         f"new end and {'were' if dropped > 1 else 'was'} removed.")
        if not changed and not carried:
            notes.append("Nothing differed from the event as it is, so nothing was written.")
        return EventWriteResult(action="updated", event=entry, changed=changed, notes=notes or None)

    return await _guarded(EventWriteResult, body)


async def delete_event_impl(event: str, occurrence: str | None = None,
                            all_occurrences: bool = False) -> EventWriteResult:
    refusal = _write_refusal()
    if refusal is not None:
        return refusal

    async def body():
        cal_id, name = _check_event_id(event)
        cfg, tz = _settings()
        async with client.Client(cfg) as dav:
            target = await _read_target(dav, cfg, cal_id, name)
            moment = _scope(target, occurrence, all_occurrences, tz, "delete")
            event_id = _event_id(target.calendar, target.href)
            if moment is None:
                entry = _entry(ical.occurrence_of(target.event), target.calendar, event_id, tz,
                               repeat=ical.rule_text(target.event))
                await dav.delete(target.href, etag=target.etag)
            else:
                target.refuse_if_floating("delete one of its occurrences (deleting the whole series, with "
                                          "all_occurrences=true, works)")
                await _check_occurrence(target, moment, tz)
                moved = ical.override(target.stored, moment, tz)
                if moved is not None:
                    occ = ical.occurrence_of(moved)
                else:
                    first, after = ical.span(target.event)
                    rid = ical.recurrence_value(target.event, moment, tz)
                    occ = dataclasses.replace(ical.occurrence_of(target.event), start=rid, end=rid + (after - first),
                                              recurrence_id=rid)
                entry = _entry(occ, target.calendar, event_id, tz, occurrence=moment)
                ical.exclude(target.stored, target.event, moment, tz, _now())
                try:
                    await dav.put(target.href, ical.serialize(target.stored), etag=target.etag)
                except client.CalDavError as exc:
                    # Nextcloud answers a series left with no occurrence at all with HTTP 500, which only
                    # a series with a last occurrence can come to; any other failure gets no such hint.
                    if exc.status != 500 or not ical.bounded(target.event):
                        raise
                    raise client.CalDavError(exc.code, exc.message + " If this was the series' last remaining "
                                             "occurrence, the server may refuse to leave it empty: tell the owner, "
                                             "and delete the whole series (all_occurrences=true) only if they "
                                             "agree.", exc.status) from exc
            logger.info("caldav: deleted %s in %s", "an occurrence" if moment is not None else "an event",
                        target.calendar.id)
        return EventWriteResult(action="deleted", event=entry)

    return await _guarded(EventWriteResult, body)


# ── The tools ───────────────────────────────────────────────────────────

_A_START = Field(description="From this date (YYYY-MM-DD, the whole day) or time (YYYY-MM-DDTHH:MM, optionally "
                             "with an offset such as +01:00). Default: today.")
_A_END = Field(description="Up to this date, included (YYYY-MM-DD), or time, excluded. Default: `start` and the "
                           "6 days after it. At most 366 days in all.")
_A_CALENDAR_FILTER = Field(description="Only this calendar (its name or id, from list_calendars). Default: all.")
_A_TEXT = Field(description="Only events whose title, place or description contains this text (any case).")
_A_LIMIT = Field(description="At most this many events (1 to 500), the earliest first.")
_A_SUMMARY = Field(description="The event's title.")
_A_EVENT_START = Field(description="A date (YYYY-MM-DD) for an all-day event, or a time (YYYY-MM-DDTHH:MM) read "
                                   "in the calendar's time zone unless it has an offset (+01:00, Z).")
_A_EVENT_END = Field(description="Of the same kind as `start`: an all-day event's last day (included), or the "
                                 "time it ends. Default: one day, or one hour.")
_A_CALENDAR = Field(description="The calendar's name or id, chosen by what list_calendars says each holds. "
                                "Default: the calendar marked default_for_new_events, when there is one.")
_A_LOCATION = Field(description="Where it happens.")
_A_DESCRIPTION = Field(description="Notes about the event.")
_A_REPEAT = Field(description="To repeat it: an iCalendar RRULE, such as FREQ=WEEKLY;BYDAY=TH, "
                              "FREQ=MONTHLY;BYMONTHDAY=1;COUNT=6 or FREQ=YEARLY;UNTIL=20301231.")
_A_EVENT = Field(description="The event's id: `event` from list_events.")
_A_OCCURRENCE = Field(description="For a repeating event: which occurrence, as `occurrence` in list_events (its "
                                  "original start, unchanged when that occurrence was moved; the same moment in "
                                  "another offset also works).")
_A_ALL = Field(description="For a repeating event: true to act on the whole series, every occurrence, past ones "
                           "included.")
_A_U_SUMMARY = Field(description="New title.")
_A_U_START = Field(description="New start (date or time, as in create_event); without `end`, the event keeps "
                               "its length.")
_A_U_END = Field(description="New end: an all-day event's last day (included), or a time.")
_A_U_LOCATION = Field(description="New place; an empty string removes it.")
_A_U_DESCRIPTION = Field(description="New notes; an empty string removes them.")
_A_U_REPEAT = Field(description="New repeat rule (iCalendar RRULE) for the whole series, with "
                                "all_occurrences=true; on an event that does not repeat, makes it repeat. To stop "
                                "a series after a date and keep its past, give its rule with UNTIL=<its last day> "
                                "in place of any COUNT.")


async def list_calendars() -> CalendarListResult:
    """List the user's calendars; use only when the user asks about their calendar.

    Never on your own initiative, even where it would help. The calendar
    tools reach a calendar server (CalDAV), not the vault, and show which
    calendar takes new events. Each
    calendar has a `name` and an `id` (give either as `calendar`), a
    `description` of what it holds when its owner set one, `writable: false`
    when this login may only read it, and `default_for_new_events` on the one
    `create_event` uses when no `calendar` is given. Pick the calendar for a
    new event by its name and description; the default is for events that
    fit no other. `time_zone` is the zone every time is shown in, and the one
    times without an offset are read in. `notes` say which calendars are left
    out, and why.
    """
    return await list_calendars_impl()


async def list_events(
    start: Annotated[str | None, _A_START] = None,
    end: Annotated[str | None, _A_END] = None,
    calendar: Annotated[str | None, _A_CALENDAR_FILTER] = None,
    text: Annotated[str | None, _A_TEXT] = None,
    limit: Annotated[int, _A_LIMIT] = DEFAULT_LIMIT,
) -> EventListResult:
    """List calendar events in a date range; only when the user asks about their schedule.

    Never on your own initiative, even where it would help. Read-only;
    repeating events are listed as single occurrences. Without dates, today
    and the 6 days after it. Each event gives its
    `calendar`, its id `event` (for `update_event` and `delete_event`), its
    `summary` (title), the `weekday` it starts on, `start` and `end` in the
    calendar's time zone (`time_zone`), with `all_day: true` on an all-day
    event, whose `end` is its last day. An occurrence of a repeating event
    also gives `occurrence`, which names it in `update_event` and
    `delete_event`, and `repeat`, the series' rule. `has_attendees: true`
    or `confidential: true` (the owner marked it confidential: its title may
    show only as "Busy", its place and notes hidden): `update_event` and
    `delete_event` refuse it; the owner changes it in their calendar app.
    Sorted by start; `notes` say when the list is cut (`limit`) or an event
    or a calendar could not be read.
    """
    return await list_events_impl(start=start, end=end, calendar=calendar, text=text, limit=limit)


async def create_event(
    summary: Annotated[str, _A_SUMMARY],
    start: Annotated[str, _A_EVENT_START],
    end: Annotated[str | None, _A_EVENT_END] = None,
    calendar: Annotated[str | None, _A_CALENDAR] = None,
    location: Annotated[str | None, _A_LOCATION] = None,
    description: Annotated[str | None, _A_DESCRIPTION] = None,
    repeat: Annotated[str | None, _A_REPEAT] = None,
) -> EventWriteResult:
    """Add a calendar event, only when the user asks to add that event.

    Never on your own initiative, even where it would help. An all-day event
    takes dates, a timed one times. Requires a read-write connection.
    Choose `calendar` by the names and
    descriptions `list_calendars` gives; without it, the event goes to the
    default calendar. Without `end`, an all-day event lasts one day and a
    timed one an hour. Refused, writing nothing, when the calendar is
    read-only (`calendar_read_only`), when no calendar is named and none is
    the default, or when the calendar already has an event with the same
    title starting at the same moment (`duplicate_event`, which describes it:
    check whether it is the one you meant). The result is the event as
    written, with its id.
    """
    return await create_event_impl(summary=summary, start=start, end=end, calendar=calendar, location=location,
                                   description=description, repeat=repeat)


async def update_event(
    event: Annotated[str, _A_EVENT],
    occurrence: Annotated[str | None, _A_OCCURRENCE] = None,
    all_occurrences: Annotated[bool, _A_ALL] = False,
    summary: Annotated[str | None, _A_U_SUMMARY] = None,
    start: Annotated[str | None, _A_U_START] = None,
    end: Annotated[str | None, _A_U_END] = None,
    location: Annotated[str | None, _A_U_LOCATION] = None,
    description: Annotated[str | None, _A_U_DESCRIPTION] = None,
    repeat: Annotated[str | None, _A_U_REPEAT] = None,
) -> EventWriteResult:
    """Change a calendar event or one occurrence, only when the user asks to change it.

    Never on your own initiative, even where it would help. Requires a
    read-write connection. Only the fields given change; everything else the
    event holds (reminders, fields set by other apps) is kept. On a repeating
    event, give `occurrence` to change that occurrence only (its time
    included), or all_occurrences=true for the whole series: its title,
    place, notes or rule, but not its time. Moving the time of a whole
    series, or of every occurrence from a date on, is refused
    (`invalid_argument`): tell the owner to do it in their calendar app,
    rather than ending the series and creating a new one. With neither
    `occurrence` nor `all_occurrences` on a repeating event, it is refused
    (`occurrence_required`). Refused, writing nothing, when the event changed
    on the server between this call's own read and its write
    (`calendar_conflict`: list it again), has attendees
    (`event_has_attendees`), or is confidential or has times without a time
    zone (`event_not_editable`, which says why); the owner changes those in
    their calendar app. `changed` lists what changed.
    """
    return await update_event_impl(event=event, occurrence=occurrence, all_occurrences=all_occurrences,
                                   summary=summary, start=start, end=end, location=location,
                                   description=description, repeat=repeat)


async def delete_event(
    event: Annotated[str, _A_EVENT],
    occurrence: Annotated[str | None, _A_OCCURRENCE] = None,
    all_occurrences: Annotated[bool, _A_ALL] = False,
) -> EventWriteResult:
    """Delete a calendar event or one occurrence, only when the user asks to remove it.

    Never on your own initiative, even where it would help. Requires a
    read-write connection. On a repeating event, give `occurrence` to delete
    that occurrence only, or all_occurrences=true to delete the whole
    series, past occurrences included; with neither, it is refused
    (`occurrence_required`). To stop a repeating event from a date on and
    keep its past, don't delete it: use update_event with
    all_occurrences=true and `repeat` set to its rule with UNTIL=<its last
    day>. Refused when the event has attendees (`event_has_attendees`), or
    is confidential or, for one occurrence, has times without a time zone
    (`event_not_editable`, which says why). These tools cannot restore a
    deleted event. The result shows what was deleted, to tell the owner.
    """
    return await delete_event_impl(event=event, occurrence=occurrence, all_occurrences=all_occurrences)


def _tool(fn, title: str, logged: tuple[str, ...], result_type: type, *, writes: bool,
          destructive: bool = False) -> plugins.PluginTool:
    """A tool as the core registers it. `logged`: the arguments its usage row keeps (never titles or notes).

    Hints as the core's (#343): all four set; open-world, since the calendar server is outside the vault.
    """
    return plugins.PluginTool(
        name=fn.__name__, fn=fn, title=title, logged_args=logged, result_type=result_type,
        refusal_result=lambda message: result_type(error=message), write_class=writes,
        annotations=ToolAnnotations(readOnlyHint=not writes, destructiveHint=destructive, idempotentHint=not writes,
                                    openWorldHint=True))


TOOLS = (
    _tool(list_calendars, "List calendars", (), CalendarListResult, writes=False),
    _tool(list_events, "List events", ("start", "end", "calendar", "limit"), EventListResult, writes=False),
    _tool(create_event, "Create event", ("calendar", "start", "end"), EventWriteResult, writes=True),
    _tool(update_event, "Update event", ("event", "occurrence", "all_occurrences"), EventWriteResult, writes=True,
          destructive=True),
    _tool(delete_event, "Delete event", ("event", "occurrence", "all_occurrences"), EventWriteResult, writes=True,
          destructive=True),
)
