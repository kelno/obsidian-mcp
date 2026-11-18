"""The calendar tools' structured results.

Event text (titles, places, descriptions) sits in fields of its own, apart
from what the server says (`notes`, `error`), as in `read_note`'s result.
Times are in the calendar's time zone (`time_zone`), with their offset.
"""

from __future__ import annotations

from pydantic import PrivateAttr

from src.mcp_server.read_result import _OmitNone
from src.services.tool_outcomes import BodyOutcome


class CalendarEntry(_OmitNone):
    #: The calendar's name; give it (or `id`) as `calendar`.
    name: str
    #: Its id on the server, unique where names may not be.
    id: str
    #: False: this login may only read it. Absent: the server did not say.
    writable: bool | None = None
    #: True on the calendar `create_event` uses when no `calendar` is given.
    default_for_new_events: bool | None = None
    #: What the calendar is for, as its owner described it on the server.
    description: str | None = None
    color: str | None = None


class CalendarListResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    error: str | None = None
    #: The zone every time is shown in, and times without an offset are read in.
    time_zone: str | None = None
    calendars: list[CalendarEntry] | None = None
    notes: list[str] | None = None


class EventEntry(_OmitNone):
    #: The calendar's name.
    calendar: str
    #: The event's id, for `update_event` and `delete_event`: `<calendar id>/<name>`.
    event: str
    #: Set on an occurrence of a repeating event: which one (its original start), for
    #: `update_event` and `delete_event`. Stays the same when that occurrence is moved.
    occurrence: str | None = None
    summary: str
    #: The day of the week `start` falls on (Monday … Sunday), in the calendar's time zone.
    weekday: str
    #: `YYYY-MM-DD` for an all-day event, else `YYYY-MM-DDTHH:MM±HH:MM`.
    start: str
    #: An all-day event's last day (included), or the time it ends.
    end: str
    all_day: bool | None = None
    location: str | None = None
    description: str | None = None
    #: The description was cut to fit; the rest is in the calendar app.
    description_truncated: bool | None = None
    #: The repeat rule (RFC 5545 RRULE, such as `FREQ=WEEKLY;BYDAY=TH`), on a repeating event.
    repeat: str | None = None
    #: TENTATIVE or CANCELLED; absent for a confirmed event.
    status: str | None = None
    #: The event has attendees: the calendar tools leave changing or deleting it to the owner.
    has_attendees: bool | None = None
    #: The owner marked it confidential or private: this login may see only "Busy" and its times,
    #: and the calendar tools leave changing or deleting it to the owner.
    confidential: bool | None = None


class EventListResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    error: str | None = None
    time_zone: str | None = None
    #: The range listed: dates are whole days, `end` the last one listed; a time `end` is excluded.
    start: str | None = None
    end: str | None = None
    #: Sorted by start; all-day events first on their day.
    events: list[EventEntry] | None = None
    #: True when `limit` cut the list; `total` is then how many there were.
    truncated: bool | None = None
    total: int | None = None
    notes: list[str] | None = None


class EventWriteResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    error: str | None = None
    #: created, updated or deleted.
    action: str | None = None
    #: The event (or occurrence) as written, or as it was before being deleted.
    event: EventEntry | None = None
    #: The fields an update changed (`start`, `end`, `summary`, …); empty when nothing differed.
    changed: list[str] | None = None
    notes: list[str] | None = None
