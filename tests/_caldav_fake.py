"""A stand-in CalDAV server for the `caldav` plugin's tests, shaped on Nextcloud's (sabre/dav).

It answers what the plugin sends: the three discovery PROPFINDs, `calendar-query`
with `expand` (repeating events expanded the way RFC 4791 §9.6.5 and sabre do it:
one VEVENT per occurrence, times in UTC, `RECURRENCE-ID` on each occurrence of a
repeating event, no RRULE/EXDATE/VTIMEZONE), `calendar-multiget`, GET, PUT
(`If-Match` / `If-None-Match`, 403 on a read-only calendar, 412 on a stale ETag)
and DELETE. Requests go through `respx`, so the plugin's own httpx client is
exercised unchanged.

Two of Nextcloud's ways are modelled because the plugin must survive them: an
all-day event is matched against a time range at UTC midnight (sabre takes a
date's start in UTC), and on a calendar shared with the login an event marked
`CLASS:CONFIDENTIAL` is served stripped, as "Busy" without place, notes or
reminders, under the ETag of the whole one.

The expansion uses python-dateutil, which icalendar brings; the plugin itself
never expands, it asks the server to.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import icalendar
import respx
from dateutil import rrule as dateutil_rrule

BASE = "https://cloud.example.test"
ROOT = "/remote.php/dav"
USER = "agent"
PASSWORD = "app-password-not-for-logs"
SESSION_COOKIE = "nc_session"

D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"
A = "http://apple.com/ns/ical/"
CS = "http://calendarserver.org/ns/"
UTC = dt.timezone.utc


@dataclass
class FakeCalendar:
    id: str
    name: str
    writable: bool = True
    components: tuple[str, ...] = ("VEVENT", "VTODO")
    color: str | None = "#0082c9"
    description: str | None = None


@dataclass
class FakeCalDav:
    """The server's state; `handle` answers one request."""

    calendars: dict[str, FakeCalendar] = field(default_factory=dict)
    #: href → (etag, iCalendar text)
    objects: dict[str, tuple[str, str]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    #: When set, every request is answered with this status.
    fail_status: int | None = None
    #: When set, the next PUT or DELETE is answered 412 (someone changed the event meanwhile).
    conflict_next_write: bool = False
    #: False: `expand` is ignored, as by a server that does not support it.
    expands: bool = True
    #: When set, every PUT and DELETE is answered with this status.
    refuse_writes_with: int | None = None
    #: Calendars (by id) whose queries are answered 500.
    failing_calendars: set[str] = field(default_factory=set)
    #: href → text served as is in every query of its calendar: an object the server holds but cannot be read.
    garbled: dict[str, str] = field(default_factory=dict)
    #: How long each answer takes, in seconds: a slow server.
    delay_seconds: float = 0.0
    #: Session ids handed out, as Nextcloud does on a login: a request carrying one is in its session.
    sessions: set[str] = field(default_factory=set)
    #: Requests that logged in (the login checked, no valid session): the slow kind on Nextcloud.
    logins: int = 0

    principal: str = f"{ROOT}/principals/users/{USER}/"
    home: str = f"{ROOT}/calendars/{USER}/"

    def add_calendar(self, cal_id: str, name: str, **kw) -> FakeCalendar:
        cal = FakeCalendar(cal_id, name, **kw)
        self.calendars[cal_id] = cal
        return cal

    def href(self, cal_id: str, name: str) -> str:
        return f"{self.home}{cal_id}/{name}"

    def put_object(self, cal_id: str, name: str, text: str) -> str:
        href = self.href(cal_id, name)
        self.objects[href] = (self._etag(text), text)
        return href

    def text(self, cal_id: str, name: str) -> str:
        return self.objects[self.href(cal_id, name)][1]

    def objects_in(self, cal_id: str) -> dict[str, str]:
        prefix = f"{self.home}{cal_id}/"
        return {h: t for h, (_e, t) in self.objects.items() if h.startswith(prefix)}

    @staticmethod
    def _etag(text: str) -> str:
        return '"' + hashlib.sha256(text.encode()).hexdigest()[:16] + '"'

    # ── HTTP ────────────────────────────────────────────────────────────

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_status is not None:
            return httpx.Response(self.fail_status, text="failing on purpose")
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="Nextcloud"'})
        new_session = None
        if self.session_of(request) not in self.sessions:
            # As Nextcloud: a login is checked (slowly, there) and a session cookie handed out.
            self.logins += 1
            new_session = f"s{self.logins}"
            self.sessions.add(new_session)
        response = self._answer(request)
        if new_session is not None:
            response.headers["Set-Cookie"] = f"{SESSION_COOKIE}={new_session}; Path=/; Secure; HttpOnly"
        return response

    @staticmethod
    def session_of(request: httpx.Request) -> str | None:
        """The session id a request carries, if any."""
        for part in request.headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return None

    def _answer(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path
        method = request.method
        if method == "PROPFIND":
            return self._propfind(path, request)
        if method == "REPORT":
            return self._report(path, request)
        if method == "GET":
            if path not in self.objects:
                return httpx.Response(404)
            etag, text = self.objects[path]
            text = self._as_served(self._calendar_of(path), text)
            return httpx.Response(200, text=text, headers={"ETag": etag, "Content-Type": "text/calendar"})
        if method in ("PUT", "DELETE"):
            return self._write(method, path, request)
        return httpx.Response(405)

    @staticmethod
    def _as_served(cal: FakeCalendar | None, text: str) -> str:
        """The object as this login gets it: on a calendar shared with it, a confidential event stripped.

        Nextcloud's CalendarObject::get() keeps only these fields and sets the title to "Busy".
        """
        if cal is None or "_shared_by_" not in cal.id or "CLASS:CONFIDENTIAL" not in text:
            return text
        kept = {"UID", "DTSTAMP", "DTSTART", "DTEND", "DURATION", "RRULE", "RDATE", "EXDATE", "EXRULE",
                "RECURRENCE-ID", "CREATED", "CLASS"}
        cal_obj = icalendar.Calendar.from_ical(text)
        for event in cal_obj.walk("VEVENT"):
            for key in [k for k in event if k not in kept]:
                del event[key]
            event.subcomponents = []
            event.add("summary", "Busy")
        return cal_obj.to_ical().decode()

    def _calendar_of(self, path: str) -> FakeCalendar | None:
        if not path.startswith(self.home):
            return None
        return self.calendars.get(path[len(self.home):].split("/", 1)[0])

    def _write(self, method: str, path: str, request: httpx.Request) -> httpx.Response:
        cal = self._calendar_of(path)
        if cal is None:
            return httpx.Response(404)
        if not cal.writable:
            return httpx.Response(403, text="read-only calendar")
        if self.refuse_writes_with is not None:
            return httpx.Response(self.refuse_writes_with, text="refused on purpose")
        if self.conflict_next_write:
            self.conflict_next_write = False
            return httpx.Response(412)
        current = self.objects.get(path)
        if_match = request.headers.get("if-match")
        if request.headers.get("if-none-match") == "*" and current is not None:
            return httpx.Response(412)
        if if_match is not None and (current is None or current[0] != if_match):
            return httpx.Response(412)
        if method == "DELETE":
            if current is None:
                return httpx.Response(404)
            del self.objects[path]
            return httpx.Response(204)
        text = request.content.decode()
        icalendar.Calendar.from_ical(text)   # a server refuses what it cannot parse
        etag = self._etag(text)
        self.objects[path] = (etag, text)
        return httpx.Response(201 if current is None else 204, headers={"ETag": etag})

    # ── PROPFIND ─────────────────────────────────────────────────────────

    def _propfind(self, path: str, request: httpx.Request) -> httpx.Response:
        depth = request.headers.get("depth", "0")
        if path.rstrip("/") == ROOT:
            return _multistatus([(f"{ROOT}/", [_el(D, "current-user-principal", _el(D, "href", text=self.principal))])])
        if path == self.principal:
            return _multistatus([(self.principal, [_el(C, "calendar-home-set", _el(D, "href", text=self.home))])])
        if path == self.home and depth == "1":
            responses = [(self.home, [_el(D, "resourcetype", _el(D, "collection"))])]
            for cal in self.calendars.values():
                privileges = [_el(D, "privilege", _el(D, "read"))]
                if cal.writable:
                    privileges += [_el(D, "privilege", _el(D, "write")), _el(D, "privilege", _el(D, "write-content"))]
                props = [
                    _el(D, "resourcetype", _el(D, "collection"), _el(C, "calendar")),
                    _el(D, "displayname", text=cal.name),
                    _el(D, "current-user-privilege-set", *privileges),
                    _el(C, "supported-calendar-component-set",
                        *[_el(C, "comp", attrib={"name": c}) for c in cal.components]),
                ]
                if cal.color:
                    props.append(_el(A, "calendar-color", text=cal.color))
                if cal.description:
                    props.append(_el(C, "calendar-description", text=cal.description))
                responses.append((f"{self.home}{cal.id}/", props))
            # What Nextcloud also lists in a calendar home, none of it a calendar.
            responses.append((f"{self.home}inbox/", [_el(D, "resourcetype", _el(D, "collection"),
                                                       _el(C, "schedule-inbox"))]))
            responses.append((f"{self.home}trashbin/", [_el(D, "resourcetype", _el(D, "collection"),
                                                          _el("http://nextcloud.com/ns", "trash-bin"))]))
            return _multistatus(responses)
        return httpx.Response(404)

    # ── REPORT ───────────────────────────────────────────────────────────

    def _report(self, path: str, request: httpx.Request) -> httpx.Response:
        cal = self._calendar_of(path)
        if cal is None:
            return httpx.Response(404)
        body = ET.fromstring(request.content)
        if body.tag == f"{{{C}}}calendar-multiget":
            hrefs = [h.text for h in body.iter(f"{{{D}}}href")]
            return _multistatus([(h, self._object_props(h, self.objects[h][1])) for h in hrefs if h in self.objects])
        if body.tag != f"{{{C}}}calendar-query":
            return httpx.Response(400)
        if cal.id in self.failing_calendars:
            return httpx.Response(500, text="failing on purpose")
        expand = body.find(f".//{{{C}}}expand")
        time_range = body.find(f".//{{{C}}}time-range")
        start = _utc(time_range.get("start"))
        end = _utc(time_range.get("end"))
        responses = []
        for href, text in sorted(self.objects_in(cal.id).items()):
            text = self._as_served(cal, text)
            cal_obj = icalendar.Calendar.from_ical(text)
            instances = _instances(cal_obj, start, end)
            if not instances:
                continue
            if expand is not None and self.expands:
                out = icalendar.Calendar()
                for key in ("VERSION", "PRODID"):
                    if key in cal_obj:
                        out.add(key.lower(), cal_obj[key])
                for inst in instances:
                    out.add_component(inst)
                responses.append((href, self._object_props(href, out.to_ical().decode())))
            else:
                responses.append((href, self._object_props(href, text)))
        for href, text in self.garbled.items():
            if self._calendar_of(href) is cal:
                responses.append((href, [_el(D, "getetag", text='"garbled"'), _el(C, "calendar-data", text=text)]))
        return _multistatus(responses)

    def _object_props(self, href: str, data: str) -> list[ET.Element]:
        return [_el(D, "getetag", text=self.objects[href][0]), _el(C, "calendar-data", text=data)]


# ── Expansion, as sabre does it ────────────────────────────────────────────


def _utc(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _as_utc(value):
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("Europe/Brussels"))   # floating: the fake's own zone
        return value.astimezone(UTC)
    return value


def _span(component) -> tuple:
    start = component.decoded("dtstart")
    if "dtend" in component:
        end = component.decoded("dtend")
    elif "duration" in component:
        end = start + component.decoded("duration")
    else:
        end = start + dt.timedelta(days=1) if not isinstance(start, dt.datetime) else start
    return start, end


def _overlaps(start, end, range_start: dt.datetime, range_end: dt.datetime) -> bool:
    if not isinstance(start, dt.datetime):
        # As sabre: a date is taken at midnight UTC, whatever the calendar's zone.
        start = dt.datetime.combine(start, dt.time(), UTC)
        end = dt.datetime.combine(end, dt.time(), UTC)
    start, end = _as_utc(start), _as_utc(end)
    return start < range_end and (end > range_start if end > start else start >= range_start)


def _instances(cal_obj, range_start: dt.datetime, range_end: dt.datetime) -> list:
    events = [e for e in cal_obj.walk("VEVENT")]
    master = next((e for e in events if "recurrence-id" not in e), None)
    overrides = {_as_utc(_dt(e.decoded("recurrence-id"))): e for e in events if "recurrence-id" in e}
    out = []
    if master is not None and "rrule" not in master and "rdate" not in master:
        start, end = _span(master)
        return [_flatten(master, start, end, None)] if _overlaps(start, end, range_start, range_end) else []
    if master is not None:
        start, end = _span(master)
        length = end - start
        all_day = not isinstance(start, dt.datetime)
        anchor = dt.datetime.combine(start, dt.time()) if all_day else start
        rule = dateutil_rrule.rrulestr(master["rrule"].to_ical().decode(), dtstart=anchor, forceset=True)
        for exdate in _listify(master.get("exdate")):
            for value in exdate.dts:
                rule.exdate(dt.datetime.combine(value.dt, dt.time()) if all_day else value.dt)
        for occurrence in rule.between(anchor - dt.timedelta(days=400), range_end.replace(tzinfo=None)
                                       if all_day else range_end, inc=True):
            original = occurrence.date() if all_day else occurrence
            key = _as_utc(_dt(original))
            if key in overrides:
                continue
            o_start, o_end = original, original + length
            if _overlaps(o_start, o_end, range_start, range_end):
                out.append(_flatten(master, o_start, o_end, original))
    for key, override in overrides.items():
        start, end = _span(override)
        if _overlaps(start, end, range_start, range_end):
            out.append(_flatten(override, start, end, override.decoded("recurrence-id")))
    return out


def _dt(value):
    return value if isinstance(value, dt.datetime) else dt.datetime.combine(value, dt.time(), ZoneInfo("Europe/Brussels"))


def _listify(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _flatten(source, start, end, recurrence_id):
    """One occurrence as sabre's expand returns it: UTC times, no rules, RECURRENCE-ID on a series' occurrences."""
    event = icalendar.Event()
    for key, value in source.property_items(recursive=False):
        if key in ("BEGIN", "END", "DTSTART", "DTEND", "DURATION", "RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
            continue
        event.add(key, value, encode=False)
    for sub in source.subcomponents:
        event.add_component(sub)
    event.add("dtstart", _as_utc(start))
    event.add("dtend", _as_utc(end))
    if recurrence_id is not None:
        event.add("recurrence-id", _as_utc(recurrence_id))
    return event


# ── XML helpers ───────────────────────────────────────────────────────────


def _el(ns: str, name: str, *children: ET.Element, text: str | None = None, attrib: dict | None = None) -> ET.Element:
    el = ET.Element(f"{{{ns}}}{name}", attrib or {})
    el.text = text
    el.extend(children)
    return el


def _multistatus(responses: list[tuple[str, list[ET.Element]]]) -> httpx.Response:
    root = ET.Element(f"{{{D}}}multistatus")
    for href, props in responses:
        response = ET.SubElement(root, f"{{{D}}}response")
        ET.SubElement(response, f"{{{D}}}href").text = href
        propstat = ET.SubElement(response, f"{{{D}}}propstat")
        prop = ET.SubElement(propstat, f"{{{D}}}prop")
        prop.extend(props)
        ET.SubElement(propstat, f"{{{D}}}status").text = "HTTP/1.1 200 OK"
    return httpx.Response(207, content=ET.tostring(root, xml_declaration=True, encoding="utf-8"),
                          headers={"Content-Type": "application/xml; charset=utf-8"})


@contextmanager
def serving(fake: FakeCalDav):
    """Route every request to `BASE` to `fake` for the duration."""
    async def answer(request: httpx.Request) -> httpx.Response:
        if fake.delay_seconds:
            await asyncio.sleep(fake.delay_seconds)
        return fake.handle(request)

    with respx.mock(assert_all_called=False) as router:
        router.route(host=urlsplit(BASE).hostname).mock(side_effect=answer)
        yield router


def event_text(uid: str, summary: str, start: str, end: str | None = None, *, extra: str = "",
               rrule: str | None = None, tzid: str | None = "Europe/Brussels", components: str = "") -> str:
    """An iCalendar object as a phone's sync app would upload it, `start`/`end` as `20261120T190000` or `20261120`."""
    def prop(name: str, value: str) -> str:
        if len(value) == 8:
            return f"{name};VALUE=DATE:{value}"
        if value.endswith("Z") or tzid is None:
            return f"{name}:{value}"
        return f"{name};TZID={tzid}:{value}"

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fake phone//EN", "BEGIN:VEVENT", f"UID:{uid}",
             "DTSTAMP:20261101T120000Z", f"SUMMARY:{summary}", prop("DTSTART", start)]
    if end:
        lines.append(prop("DTEND", end))
    if rrule:
        lines.append(f"RRULE:{rrule}")
    lines += [line for line in extra.split("\n") if line]
    lines.append("END:VEVENT")
    lines += [line for line in components.split("\n") if line]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def nextcloud_like() -> FakeCalDav:
    """The deployment the plugin was written for: the agents' own empty calendars, and the owner's shared ones."""
    fake = FakeCalDav()
    fake.add_calendar("personal", "Personal")
    fake.add_calendar("contact_birthdays", "Contact birthdays", writable=False, components=("VEVENT",))
    fake.add_calendar("patipa_shared_by_owner", "Patipa")
    fake.add_calendar("tasks", "Tasks", components=("VTODO",))
    return fake


