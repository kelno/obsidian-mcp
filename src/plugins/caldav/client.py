"""A small CalDAV client (RFC 4791) over httpx: discovery, calendars, events, writes.

Only what the calendar tools need: find the login's calendar home, list its
calendars, ask the server for the events in a time range with repeating events
expanded (`calendar-query` with `expand`, RFC 4791 §9.6.5, so the server's own
recurrence engine decides the occurrences), fetch objects, and PUT and DELETE
them with `If-Match` / `If-None-Match` so a change made meanwhile is refused,
never overwritten.

Transport policy, like the core's other outbound clients: no redirects (the
password would follow them; a redirect is reported so `CALDAV_URL` can be
fixed), no proxy or `.netrc` from the environment (`trust_env=False`), the
server's `OUTBOUND_USER_AGENT`, one deadline for all the requests of a call
(`CALDAV_TIMEOUT_SECONDS`), and a cap on how much of a response is read. Every
request goes to `CALDAV_URL`'s own scheme and host, whatever host an `href` in a
response names. Answers are parsed off the event loop (`asyncio.to_thread`): a
large one takes seconds, and the server runs one worker.

One HTTP client per login serves every call (`_Session`): its connections and
the server's session cookies are kept between calls, and until the server's
session is established, requests go one at a time, so calls arriving together
on a cold session share one slow login rather than each paying their own.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from src.config import settings as core_settings
from src.plugins.caldav.config import CalDavSettings

logger = logging.getLogger(__name__)

D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"
APPLE = "http://apple.com/ns/ical/"

#: The most of one response read, in bytes: a year of a busy calendar (about 1,000 occurrences,
#: some 400 KB) is well under it. Parsing 4 MB takes a few seconds and some 85 MB of memory.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# Discovered calendar homes, per (CALDAV_URL, CALDAV_USERNAME): the home never
# moves while the server runs, so the two discovery requests run once.
_HOMES: dict[tuple[str, str], str] = {}

#: How long a calendar list is reused by the calls that only work within it (`recent_calendars`).
CALENDARS_MAX_AGE_SECONDS = 120.0

# The calendar list last fetched, per (CALDAV_URL, CALDAV_USERNAME), with when (`time.monotonic`).
# Dropped whenever a request fails, so an error (a calendar gone, a right withdrawn) is never
# followed by another call working from the same list.
_CALENDARS: dict[tuple[str, str], tuple[float, list["CalendarInfo"]]] = {}

# The repeat rule of each repeating event `list_events` read, per (login, href), with the ETag of
# the version it was read from (None: no rule of its own to show). An ETag names one version of an
# object, and the expanded query's answer carries the stored object's, so a rule kept for the ETag
# that query gives is the rule `calendar-multiget` would give now: that request is then skipped.
RULES: dict[tuple[tuple[str, str], str], tuple[str, str | None]] = {}

#: Requests sent one at a time on a new session before it counts as established anyway.
_OPENING_REQUESTS = 3


@dataclass(eq=False)
class _Session:
    """One login's HTTP client, shared by every call: its connections and the server's session.

    Nextcloud checks a login slowly and a session fast: on a small home server, a request
    that logs in took 8 s and one in a session 0.25 s (2026-11-18), so a call of three
    requests ran out of time on logins alone. The login still goes with every request, so a
    session the server has dropped costs one login, never a refusal. Until a request comes
    back without the server changing the session cookies (or `_OPENING_REQUESTS` came back),
    requests go one at a time: concurrent calls on a cold session then share one login
    rather than each logging in.
    """

    cfg: CalDavSettings
    http: httpx.AsyncClient
    opening: asyncio.Lock = field(default_factory=asyncio.Lock)
    established: bool = False
    answered: int = 0

    def cookies(self) -> dict[tuple[str, str, str], str | None]:
        return {(c.domain, c.path, c.name): c.value for c in self.http.cookies.jar}


# The sessions, per (CALDAV_URL, CALDAV_USERNAME). The cookies are a credential: memory only, never logged.
_SESSIONS: dict[tuple[str, str], _Session] = {}


def _session(cfg: CalDavSettings, login: tuple[str, str]) -> _Session:
    """The login's session, opened on first use (or when a setting changed)."""
    session = _SESSIONS.get(login)
    if session is None or session.cfg != cfg:
        session = _Session(cfg, httpx.AsyncClient(
            auth=httpx.BasicAuth(cfg.username, cfg.password.get_secret_value()),
            timeout=cfg.timeout_seconds, follow_redirects=False, trust_env=False,
            headers={"User-Agent": core_settings.outbound_user_agent}))
        _SESSIONS[login] = session
    return session


def forget_server_state() -> None:
    """Drop the discovered calendar homes, the calendar lists, the kept repeat rules and the sessions
    (tests; a changed setting)."""
    _HOMES.clear()
    _CALENDARS.clear()
    RULES.clear()
    _SESSIONS.clear()


async def close_sessions() -> None:
    """Close every session's HTTP client (the server shutting down)."""
    sessions = list(_SESSIONS.values())
    _SESSIONS.clear()
    for session in sessions:
        await session.http.aclose()


class CalDavError(Exception):
    """A request that did not give what was asked; `code` is the tool's refusal code, `status`
    the server's HTTP status when it answered with an error (None: no answer, or another cause)."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class CalendarInfo:
    """One calendar of the login's calendar home.

    Attributes:
        id: its last path segment, as the server spells it (percent-encoded).
        href: its path on the server, ending in `/`.
        name: its display name (the id when it has none).
        writable: from the server's privileges; None when the server did not say.
        events: whether it holds events (False: a task list).
        color: its colour, as `#rrggbb[aa]`, when set.
        description: what it is for (`calendar-description`), when its owner set one.
    """

    id: str
    href: str
    name: str
    writable: bool | None
    events: bool
    color: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class CalendarObject:
    """One calendar object (an `.ics` resource): its path, ETag and iCalendar text."""

    href: str
    etag: str | None
    data: str


@dataclass(frozen=True)
class _Reply:
    """A response the caller asked for: its status, headers and (capped) body."""

    status: int
    headers: httpx.Headers
    content: bytes


def _el(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


def _body(root: ET.Element) -> bytes:
    return ET.tostring(root, xml_declaration=True, encoding="utf-8")


def _propfind(*props: tuple[str, str]) -> bytes:
    root = ET.Element(_el(D, "propfind"))
    prop = ET.SubElement(root, _el(D, "prop"))
    for ns, name in props:
        ET.SubElement(prop, _el(ns, name))
    return _body(root)


def _utc_stamp(moment: datetime.datetime) -> str:
    """An aware time as CalDAV's time ranges take it: UTC, `YYYYMMDDTHHMMSSZ`."""
    return moment.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Client:
    """One call's use of the login's session; use as `async with Client(cfg) as client:`."""

    def __init__(self, cfg: CalDavSettings):
        self._cfg = cfg
        parts = urlsplit(cfg.url.strip())
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._root = parts.path or "/"
        self._login = (cfg.url.strip(), cfg.username)
        self._session: _Session | None = None
        self._deadline = 0.0

    @property
    def login(self) -> tuple[str, str]:
        """`(CALDAV_URL, CALDAV_USERNAME)`: what the server's state is kept per."""
        return self._login

    async def __aenter__(self) -> "Client":
        # One deadline for the whole call: httpx's timeout is per phase of one request, and a
        # write makes up to five requests, each of which a hung server could hold to the limit.
        self._deadline = asyncio.get_running_loop().time() + self._cfg.timeout_seconds
        self._session = _session(self._cfg, self._login)
        return self

    async def __aexit__(self, *exc) -> None:
        # The session stays open for the next call, a failed or timed-out one's included: the
        # session its requests got spares the next call the slow logins.
        self._session = None

    # ── Requests ────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        """`path` (an href from a response, or our own) on `CALDAV_URL`'s scheme and host only."""
        return self._origin + urlsplit(urljoin(self._origin + self._root, path)).path

    async def _request(self, method: str, path: str, *, body: bytes | None = None,
                       headers: dict[str, str] | None = None, ok: tuple[int, ...] = (200, 207),
                       what: str, event_write: bool = False) -> _Reply:
        """Send one request; its status in `ok`, headers and body, read up to `MAX_RESPONSE_BYTES`.

        Raises:
            CalDavError: `calendar_unavailable` when the server cannot be reached or answers an
                error, `calendar_not_configured` for a refused login or a redirect, and the codes
                of `_status_error` for the rest.
        """
        try:
            return await self._checked(method, path, body=body, headers=headers, ok=ok, what=what,
                                       event_write=event_write)
        except CalDavError:
            # A calendar gone or a right withdrawn shows as an error: the next call asks for the list again.
            _CALENDARS.pop(self._login, None)
            raise

    async def _checked(self, method: str, path: str, *, body: bytes | None, headers: dict[str, str] | None,
                       ok: tuple[int, ...], what: str, event_write: bool) -> _Reply:
        session = self._session
        assert session is not None
        url = self._url(path)
        try:
            async with asyncio.timeout_at(self._deadline):
                if session.established:
                    status, response_headers, content = await _exchange(session, method, url, body, headers, ok,
                                                                        what)
                else:
                    status, response_headers, content = await _opening(session, method, url, body, headers, ok,
                                                                       what)
        except TimeoutError as exc:
            logger.warning("caldav: %s %s ran past the call's deadline", method, urlsplit(url).path)
            raise CalDavError("calendar_unavailable", "The calendar server took longer than "
                              f"{self._cfg.timeout_seconds:g} s in all (CALDAV_TIMEOUT_SECONDS) to answer this "
                              f"call; it was still {what}. Try again later; if it keeps happening, tell the owner.") \
                from exc
        except httpx.HTTPError as exc:
            logger.warning("caldav: %s %s failed: %s", method, urlsplit(url).path, type(exc).__name__)
            raise CalDavError("calendar_unavailable", f"The calendar server could not be reached for {what} "
                              f"({type(exc).__name__}). Try again later; if it keeps failing, tell the owner.") \
                from exc
        if status in ok:
            return _Reply(status, response_headers, content)
        logger.info("caldav: %s %s answered %d", method, urlsplit(url).path, status)
        if status in (301, 302, 303, 307, 308):
            raise CalDavError("calendar_not_configured", f"The calendar server answered {what} with a redirect "
                              f"(HTTP {status}) to {response_headers.get('location', '(no address)')}: the "
                              "owner should set CALDAV_URL to that address.")
        if status == 401:
            raise CalDavError("calendar_not_configured", "The calendar server refused the login (HTTP 401): the "
                              "owner should check CALDAV_USERNAME and CALDAV_PASSWORD (an app password).")
        error = _status_error(status, what, event_write)
        error.status = status
        raise error

    # ── Discovery ───────────────────────────────────────────────────────

    async def _href_prop(self, path: str, ns: str, name: str) -> str | None:
        response = await self._request("PROPFIND", path, body=_propfind((ns, name)),
                                       headers={"Depth": "0", "Content-Type": "application/xml"},
                                       what="finding the calendars")
        for found in _parse(response.content).iter(_el(ns, name)):
            href = found.find(_el(D, "href"))
            if href is not None and href.text:
                return href.text.strip()
        return None

    async def home(self) -> str:
        """The login's calendar home: its path, ending in `/`.

        Raises:
            CalDavError: `calendar_not_configured` when `CALDAV_URL` leads to no calendar home.
        """
        if self._login in _HOMES:
            return _HOMES[self._login]
        principal = await self._href_prop(self._root, D, "current-user-principal") or self._root
        home = await self._href_prop(principal, C, "calendar-home-set")
        if home is None:
            raise CalDavError("calendar_not_configured", "The calendar server named no calendar home for this "
                              "login: the owner should check that CALDAV_URL is the CalDAV address "
                              "(for Nextcloud, https://<host>/remote.php/dav).")
        home = urlsplit(urljoin(self._origin + principal, home)).path
        home = home if home.endswith("/") else home + "/"
        _HOMES[self._login] = home
        return home

    def recent_calendars(self) -> list[CalendarInfo] | None:
        """The calendar list `calendars` fetched in the last `CALENDARS_MAX_AGE_SECONDS`, if any."""
        kept = _CALENDARS.get(self._login)
        if kept is None or time.monotonic() - kept[0] > CALENDARS_MAX_AGE_SECONDS:
            return None
        return list(kept[1])

    async def calendars(self) -> list[CalendarInfo]:
        """The calendars in the login's home (shared ones included), task lists marked: always
        asked of the server, and kept for `recent_calendars`."""
        home = await self.home()
        body = _propfind((D, "resourcetype"), (D, "displayname"), (D, "current-user-privilege-set"),
                         (C, "supported-calendar-component-set"), (C, "calendar-description"),
                         (APPLE, "calendar-color"))
        response = await self._request("PROPFIND", home, body=body,
                                       headers={"Depth": "1", "Content-Type": "application/xml"},
                                       what="listing the calendars")
        found = []
        for href, prop in _responses(response.content):
            if prop is None or prop.find(f"{_el(D, 'resourcetype')}/{_el(C, 'calendar')}") is None:
                continue
            path = urlsplit(urljoin(self._origin + home, href)).path
            path = path if path.endswith("/") else path + "/"
            if not path.startswith(home) or path == home:
                continue
            cal_id = path[len(home):].strip("/")
            if "/" in cal_id:
                continue
            name = (prop.findtext(_el(D, "displayname")) or "").strip() or unquote(cal_id)
            components = {c.get("name", "").upper()
                          for c in prop.iter(_el(C, "comp"))}
            privileges = prop.find(_el(D, "current-user-privilege-set"))
            writable = None
            if privileges is not None:
                granted = {p.tag for p in privileges.iter() if p.tag.startswith("{DAV:}")}
                writable = bool(granted & {_el(D, "write"), _el(D, "write-content"), _el(D, "all")})
            color = (prop.findtext(_el(APPLE, "calendar-color")) or "").strip() or None
            description = (prop.findtext(_el(C, "calendar-description")) or "").strip() or None
            found.append(CalendarInfo(id=cal_id, href=path, name=name, writable=writable,
                                      events=not components or "VEVENT" in components, color=color,
                                      description=description))
        _CALENDARS[self._login] = (time.monotonic(), list(found))
        return found

    # ── Events ──────────────────────────────────────────────────────────

    async def expanded(self, calendar: CalendarInfo, start, end) -> list[CalendarObject]:
        """Every event of `calendar` with an occurrence in `[start, end)`, expanded by the server.

        `start` and `end` are aware datetimes. Each object's `data` holds one VEVENT per
        occurrence, in UTC, with `RECURRENCE-ID` on the occurrences of a repeating event; a
        server that ignores `expand` returns the stored objects instead (the caller checks).
        """
        root = ET.Element(_el(C, "calendar-query"))
        prop = ET.SubElement(root, _el(D, "prop"))
        ET.SubElement(prop, _el(D, "getetag"))
        data = ET.SubElement(prop, _el(C, "calendar-data"))
        ET.SubElement(data, _el(C, "expand"), start=_utc_stamp(start), end=_utc_stamp(end))
        vcalendar = ET.SubElement(ET.SubElement(root, _el(C, "filter")), _el(C, "comp-filter"), name="VCALENDAR")
        vevent = ET.SubElement(vcalendar, _el(C, "comp-filter"), name="VEVENT")
        ET.SubElement(vevent, _el(C, "time-range"), start=_utc_stamp(start), end=_utc_stamp(end))
        response = await self._request("REPORT", calendar.href, body=_body(root),
                                       headers={"Depth": "1", "Content-Type": "application/xml"},
                                       what=f"listing the events of {calendar.name!r}")
        return await asyncio.to_thread(self._objects, response.content, calendar.href)

    async def multiget(self, calendar: CalendarInfo, hrefs: list[str]) -> list[CalendarObject]:
        """The stored objects at `hrefs` (paths in `calendar`), as written, in one request."""
        root = ET.Element(_el(C, "calendar-multiget"))
        prop = ET.SubElement(root, _el(D, "prop"))
        ET.SubElement(prop, _el(D, "getetag"))
        ET.SubElement(prop, _el(C, "calendar-data"))
        for href in hrefs:
            ET.SubElement(root, _el(D, "href")).text = href
        response = await self._request("REPORT", calendar.href, body=_body(root),
                                       headers={"Depth": "1", "Content-Type": "application/xml"},
                                       what=f"reading repeating events of {calendar.name!r}")
        return await asyncio.to_thread(self._objects, response.content, calendar.href)

    def _objects(self, content: bytes, base: str) -> list[CalendarObject]:
        found = []
        for href, prop in _responses(content):
            if prop is None:
                continue
            data = prop.findtext(_el(C, "calendar-data"))
            if not data:
                continue
            path = urlsplit(urljoin(self._origin + base, href)).path
            found.append(CalendarObject(href=path, etag=(prop.findtext(_el(D, "getetag")) or "").strip() or None,
                                        data=data))
        return found

    async def get(self, href: str) -> CalendarObject:
        """One stored object.

        Raises:
            CalDavError: `not_found` when there is none at `href`.
        """
        reply = await self._request("GET", href, what="reading the event", ok=(200,))
        return CalendarObject(href=href, etag=reply.headers.get("etag"),
                              data=reply.content.decode("utf-8", errors="replace"))

    async def put(self, href: str, data: str, *, create: bool = False, etag: str | None = None) -> None:
        """Write `data` at `href`: a new object with `create` (refused if one exists), else a
        replacement of exactly version `etag` (refused if it changed; unconditional only when
        the server gave the object no ETag).

        Raises:
            CalDavError: `calendar_conflict` (HTTP 412), `calendar_read_only` (403),
                `invalid_argument` when the server refuses the event itself.
        """
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        else:
            logger.warning("caldav: %s has no ETag; writing it without a precondition", href)
        await self._request("PUT", href, body=data.encode("utf-8"), headers=headers, ok=(200, 201, 204),
                            what="writing the event", event_write=True)

    async def delete(self, href: str, *, etag: str | None) -> None:
        """Delete the object at `href`, only if it is still at version `etag` (when given).

        Raises:
            CalDavError: as `put`, and `not_found`.
        """
        await self._request("DELETE", href, headers={"If-Match": etag} if etag else None, ok=(200, 204),
                            what="deleting the event")


async def _opening(session: _Session, method: str, url: str, body: bytes | None, headers: dict[str, str] | None,
                   ok: tuple[int, ...], what: str) -> tuple[int, httpx.Headers, bytes]:
    """`_exchange`, one request at a time until the session is established (see `_Session`)."""
    async with session.opening:
        if not session.established:
            before = session.cookies()
            reply = await _exchange(session, method, url, body, headers, ok, what)
            session.answered += 1
            if session.cookies() == before or session.answered >= _OPENING_REQUESTS:
                session.established = True
            return reply
    # Established while this request waited its turn: no need to keep the others waiting.
    return await _exchange(session, method, url, body, headers, ok, what)


async def _exchange(session: _Session, method: str, url: str, body: bytes | None, headers: dict[str, str] | None,
                    ok: tuple[int, ...], what: str) -> tuple[int, httpx.Headers, bytes]:
    """`(status, headers, body)` of one request; the body is read only for a status in `ok`, up to
    `MAX_RESPONSE_BYTES`."""
    async with session.http.stream(method, url, content=body, headers=headers) as response:
        status, response_headers = response.status_code, response.headers
        if status not in ok:
            return status, response_headers, b""   # an error's body is not read: closing the stream drops it
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise CalDavError("calendar_unavailable", f"The calendar server's answer to {what} is larger than "
                                  f"{MAX_RESPONSE_BYTES // (1024 * 1024)} MB; ask for a shorter time range or one "
                                  "calendar.")
            chunks.append(chunk)
        return status, response_headers, b"".join(chunks)


def object_href(calendar: CalendarInfo, name: str) -> str:
    """The path of the object named `name` (one percent-encoded segment) in `calendar`."""
    return calendar.href + name


def new_object_name(uid: str) -> str:
    """The resource name for a new event: its UID, percent-encoded, plus `.ics`."""
    return quote(uid, safe="") + ".ics"


def _status_error(status: int, what: str, event_write: bool) -> CalDavError:
    if status == 403:
        return CalDavError("calendar_read_only", "The calendar server refused the change (HTTP 403): the login "
                           "may only read this calendar. Use list_calendars to see which calendars are writable.")
    if status == 404:
        return CalDavError("not_found", "The calendar server has no such event or calendar (HTTP 404): it may "
                           "have been deleted or moved. List the events again with list_events.")
    if status == 412:
        return CalDavError("calendar_conflict", "The event changed on the calendar server since it was read "
                           "(HTTP 412), so nothing was written. Read it again with list_events, then redo the "
                           "change if it still applies.")
    if status in (400, 409, 415, 422) and event_write:
        return CalDavError("invalid_argument", f"The calendar server refused the event (HTTP {status}); nothing "
                           "was written. Check the values given (dates, the repeat rule).")
    return CalDavError("calendar_unavailable", f"The calendar server answered {what} with HTTP {status}. Try "
                       "again later; if it keeps failing, tell the owner.")


def _parse(content: bytes) -> ET.Element:
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise CalDavError("calendar_unavailable", "The calendar server's answer was not valid XML "
                          f"({exc}).") from exc


def _responses(content: bytes) -> list[tuple[str, ET.Element | None]]:
    """`(href, the 200 propstat's prop)` for each `response` of a multistatus."""
    out = []
    for response in _parse(content).iter(_el(D, "response")):
        href = (response.findtext(_el(D, "href")) or "").strip()
        if not href:
            continue
        prop = None
        for propstat in response.iter(_el(D, "propstat")):
            if " 200 " in (propstat.findtext(_el(D, "status")) or "") + " ":
                prop = propstat.find(_el(D, "prop"))
                break
        out.append((href, prop))
    return out
