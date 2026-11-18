"""The `caldav` plugin: calendar tools over CalDAV, against a stand-in Nextcloud.

The tools run as the server registers them (`plugin_tool_impls`, so through
`_tracked`), with `_log_usage` captured and no database. The CalDAV server is
`tests/_caldav_fake.py`, reached through `respx`, so the plugin's own HTTP
client, XML and iCalendar handling all run unchanged.

What matters most: a write never loses what it did not mean to change (fields
from the phone, other occurrences of a repeating event); a repeating event is
never changed or deleted whole by accident; a read-only calendar, a missing
setting, a refused login or an unreachable server each give their own refusal;
the password never appears in a result or a log line.
"""

import datetime as dt
import logging
import os
import tempfile
import time

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from zoneinfo import ZoneInfo  # noqa: E402

import httpx  # noqa: E402
import icalendar  # noqa: E402
import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src import plugins  # noqa: E402
from src.mcp_server.auth import current_api_key_id, current_permission  # noqa: E402
from src.plugins.caldav import client as caldav_client  # noqa: E402
from src.plugins.caldav import config as caldav_config  # noqa: E402
from src.plugins.caldav import ical as caldav_ical  # noqa: E402
from src.plugins.caldav import tools as caldav_tools  # noqa: E402
from src.services import concurrency  # noqa: E402
from tests import _caldav_fake as fake_server  # noqa: E402
from tests._caldav_fake import BASE, PASSWORD, ROOT, USER, event_text  # noqa: E402

TOOL_NAMES = ("list_calendars", "list_events", "create_event", "update_event", "delete_event")
TRACKED = {spec.name: fn for spec, fn in tools.plugin_tool_impls(plugins.load(("caldav",)))}
SHARED = "patipa_shared_by_owner"
NOW = dt.datetime(2026, 11, 14, 9, 30, tzinfo=dt.timezone.utc)   # a Saturday


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def usage_log(monkeypatch):
    entries = []

    async def _capture(tool, params, duration_ms, response_size):
        entries.append({"tool": tool, "params": params})
        return True

    monkeypatch.setattr(tools, "_log_usage", _capture)
    return entries


@pytest.fixture(autouse=True)
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def settings_env(monkeypatch):
    """A configured plugin; tests drop or change one setting to see the refusal."""
    for name in list(os.environ):
        if name.startswith("CALDAV_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CALDAV_URL", BASE + ROOT)
    monkeypatch.setenv("CALDAV_USERNAME", USER)
    monkeypatch.setenv("CALDAV_PASSWORD", PASSWORD)
    monkeypatch.setenv("CALDAV_TIMEZONE", "Europe/Brussels")
    monkeypatch.setattr(caldav_tools, "_now", lambda: NOW)
    caldav_config.current.cache_clear()
    caldav_client.forget_server_state()
    yield monkeypatch
    caldav_config.current.cache_clear()
    caldav_client.forget_server_state()


def reconfigure(monkeypatch, **values):
    for key, value in values.items():
        name = "CALDAV_" + key.upper()
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    caldav_config.current.cache_clear()
    caldav_client.forget_server_state()


@pytest.fixture(autouse=True)
def readwrite():
    """A credential that may write; `readonly` below narrows it."""
    perm = current_permission.set("readwrite")
    try:
        yield
    finally:
        current_permission.reset(perm)


@pytest.fixture
def server():
    fake = fake_server.nextcloud_like()
    with fake_server.serving(fake):
        yield fake


@pytest.fixture
def readonly():
    perm = current_permission.set("read")
    key = current_api_key_id.set(11)
    try:
        yield
    finally:
        current_permission.reset(perm)
        current_api_key_id.reset(key)


def call(name):
    return TRACKED[name]


def code(result) -> str | None:
    return None if result._body_outcome is None else result._body_outcome.marker


def assert_no_password(*texts):
    for text in texts:
        assert PASSWORD not in str(text)


YOGA = event_text("yoga-1", "Yoga", "20261015T190000", "20261015T200000", rrule="FREQ=WEEKLY;BYDAY=TH")
DENTIST = event_text("dentist-1", "Dentist, Dr Peeters", "20261118T140000", "20261118T143000",
                     extra="LOCATION:Rue Haute 12\\, Brussels\nDESCRIPTION:Bring the X-ray")
HOLIDAY = event_text("holiday-1", "Ardennes weekend", "20261121", "20261123")
PHONE = event_text("phone-1", "Call the vet", "20261117T090000", "20261117T091500",
                   extra="X-PHONE-SPECIAL;X-PARAM=1:keep me\nCATEGORIES:Dog",
                   components="")
PHONE_WITH_ALARM = PHONE.replace("END:VEVENT", "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Vet\r\n"
                                 "TRIGGER:-PT15M\r\nEND:VALARM\r\nEND:VEVENT")


@pytest.fixture
def owner_events(server):
    server.put_object(SHARED, "yoga.ics", YOGA)
    server.put_object(SHARED, "dentist.ics", DENTIST)
    server.put_object(SHARED, "holiday.ics", HOLIDAY)
    server.put_object(SHARED, "phone.ics", PHONE_WITH_ALARM)
    return server


# ── The plugin itself ────────────────────────────────────────────────────


def test_the_plugin_adds_five_tools_each_with_an_admission_class():
    (plugin,) = plugins.load(("caldav",))
    assert tuple(t.name for t in plugin.tools) == TOOL_NAMES
    for spec in plugin.tools:
        assert concurrency.TOOL_CLASSES[spec.name] == "light"
        assert spec.result_type is not None and spec.refusal_result is not None
    assert {s.name for s in plugin.tools if s.write_class} == {"create_event", "update_event", "delete_event"}
    assert "## Calendar (this server)" in plugin.guide_text


def test_the_plugin_loads_without_settings(monkeypatch):
    """A missing setting is a refusal on each call, never a server that does not start."""
    reconfigure(monkeypatch, url=None, username=None, password=None)
    (plugin,) = plugins.load(("caldav",))
    assert plugin.name == "caldav"


def test_each_description_has_a_first_line_that_stands_alone():
    for spec in plugins.load(("caldav",))[0].tools:
        first = spec.fn.__doc__.strip().split("\n")[0]
        assert first.endswith(".") and len(first) <= 90, first
        # The owner's decision (2026-11-14): the calendar is used only when the user asks, said where
        # claude.ai shows a tool before any search, its first line, and again in the guide.
        assert "only when the user asks" in first, first
    assert "only when the user asks" in plugins.load(("caldav",))[0].guide_text


# ── Settings and the connection ──────────────────────────────────────────


async def test_missing_settings_are_named_and_nothing_is_sent(monkeypatch, server):
    reconfigure(monkeypatch, password=None, username=None)
    result = await call("list_calendars")()
    assert code(result) == "calendar_not_configured"
    assert "CALDAV_USERNAME" in result.error and "CALDAV_PASSWORD" in result.error
    assert server.requests == []


async def test_a_plain_http_url_is_refused_unless_it_is_loopback(monkeypatch, server):
    reconfigure(monkeypatch, url="http://cloud.example.test/remote.php/dav")
    result = await call("list_events")()
    assert code(result) == "calendar_not_configured" and "https" in result.error
    assert server.requests == []
    reconfigure(monkeypatch, url="http://127.0.0.1:8080/remote.php/dav")
    assert caldav_config.problems(caldav_config.current()) == []


async def test_an_unknown_time_zone_is_refused(monkeypatch, server):
    reconfigure(monkeypatch, timezone="Europe/Atlantis")
    result = await call("list_calendars")()
    assert code(result) == "calendar_not_configured" and "Europe/Atlantis" in result.error


async def test_the_time_zone_defaults_to_the_task_tools_one(monkeypatch, server):
    reconfigure(monkeypatch, timezone=None)
    monkeypatch.setattr(tools.settings, "tasks_timezone", "America/Montreal")
    result = await call("list_calendars")()
    assert result.time_zone == "America/Montreal"


async def test_multi_user_mode_is_refused(monkeypatch, server):
    """One CalDAV login serves the whole deployment: with several users it would show one person's calendar to all."""
    monkeypatch.setattr(tools.settings, "multi_user_mode", True)
    result = await caldav_tools.list_calendars_impl()
    assert code(result) == "calendar_not_configured" and "single-user" in result.error
    assert server.requests == []


async def test_a_refused_login_says_to_check_the_settings_without_the_password(monkeypatch, server, caplog):
    reconfigure(monkeypatch, password="wrong-password-123")
    with caplog.at_level(logging.DEBUG):
        result = await call("list_calendars")()
    assert code(result) == "calendar_not_configured" and "401" in result.error
    assert "CALDAV_USERNAME" in result.error
    assert_no_password(result.error, caplog.text)
    assert "wrong-password-123" not in result.error + caplog.text


async def test_an_unreachable_server_is_calendar_unavailable(server):
    server.fail_status = 503
    result = await call("list_events")()
    assert code(result) == "calendar_unavailable" and "503" in result.error


async def test_a_connection_error_is_calendar_unavailable(monkeypatch):
    with fake_server.respx.mock(assert_all_called=False) as router:
        router.route(host="cloud.example.test").mock(side_effect=httpx.ConnectError("refused"))
        result = await call("list_calendars")()
    assert code(result) == "calendar_unavailable" and "ConnectError" in result.error


async def test_a_redirect_is_not_followed(monkeypatch):
    with fake_server.respx.mock(assert_all_called=False) as router:
        router.route(host="cloud.example.test").mock(
            return_value=httpx.Response(301, headers={"Location": "https://elsewhere.example/dav"}))
        result = await call("list_calendars")()
    assert code(result) == "calendar_not_configured"
    assert "https://elsewhere.example/dav" in result.error and "CALDAV_URL" in result.error


async def test_discovery_runs_once_per_process(server):
    await call("list_calendars")()
    await call("list_calendars")()
    principal_lookups = [r for r in server.requests if r.method == "PROPFIND" and r.url.path.rstrip("/") == ROOT]
    assert len(principal_lookups) == 1


async def test_the_session_is_kept_between_calls(server):
    """Nextcloud checks a login slowly (8 s a request, measured on a home server) and a session fast
    (0.25 s): after the first request, every request of every call rides the session it handed out."""
    await call("list_calendars")()
    first_call = len(server.requests)
    await call("list_events")()
    later = server.requests[first_call:]
    assert later and all(server.session_of(r) in server.sessions for r in later)
    assert server.logins == 1


async def test_a_failed_call_still_keeps_its_session(server):
    """The first call on a slow server may run out of time; the session it got is kept for the next one."""
    server.failing_calendars = set(server.calendars)
    assert code(await call("list_events")()) == "calendar_unavailable"
    server.failing_calendars = set()
    assert (await call("list_events")()).error is None
    assert server.logins == 1


async def test_an_expired_session_logs_in_again(server):
    """The login goes with every request, so a session the server dropped costs one login, never a refusal."""
    await call("list_calendars")()
    server.sessions.clear()
    assert (await call("list_calendars")()).error is None
    assert server.logins == 2


async def test_a_session_serves_only_its_own_login(monkeypatch, server):
    await call("list_calendars")()
    monkeypatch.setenv("CALDAV_USERNAME", "someone-else")
    caldav_config.current.cache_clear()
    before = len(server.requests)
    assert code(await call("list_calendars")()) == "calendar_not_configured"   # the fake knows one login only
    assert all(server.session_of(r) is None for r in server.requests[before:])


# ── list_calendars ───────────────────────────────────────────────────────


async def test_list_calendars_shows_event_calendars_with_their_rights(server):
    result = await call("list_calendars")()
    assert result.error is None
    by_name = {c.name: c for c in result.calendars}
    assert set(by_name) == {"Personal", "Contact birthdays", "Patipa"}   # "Tasks" holds only tasks
    assert by_name["Contact birthdays"].writable is False
    assert by_name["Patipa"].writable is True and by_name["Patipa"].id == SHARED
    assert result.time_zone == "Europe/Brussels"
    assert any("Tasks" in n and "task" in n for n in result.notes)
    # Two writable calendars and no CALDAV_CALENDARS: no default, and the notes say so.
    assert not any(c.default_for_new_events for c in result.calendars)
    assert any("create_event" in n for n in result.notes)
    # A calendar of the login's own (Nextcloud's "Personal") may be seen by no one else.
    assert any("CALDAV_CALENDARS" in n and "ask the owner" in n for n in result.notes)


async def test_caldav_calendars_picks_orders_and_sets_the_default(monkeypatch, server):
    reconfigure(monkeypatch, calendars="Contact birthdays, Patipa,RDV")
    result = await call("list_calendars")()
    assert [c.name for c in result.calendars] == ["Contact birthdays", "Patipa"]
    # The first writable one in the list takes new events.
    assert [c.name for c in result.calendars if c.default_for_new_events] == ["Patipa"]
    assert any("RDV" in n and "not found" in n for n in result.notes)
    assert any("1 other calendar" in n for n in result.notes)   # Personal, hidden
    assert not any("ask the owner" in n for n in result.notes)


async def test_a_shared_calendar_is_found_without_its_owner_suffix(monkeypatch, owner_events):
    """Nextcloud names a calendar shared with the login "Patipa (kelno)" (seen on Nextcloud 31)."""
    owner_events.calendars[SHARED].name = "Patipa (kelno)"
    reconfigure(monkeypatch, calendars="Patipa")
    listed = await call("list_calendars")()
    assert [c.name for c in listed.calendars] == ["Patipa (kelno)"] and listed.calendars[0].default_for_new_events
    events = await call("list_events")(start="2026-11-18", end="2026-11-18", calendar="patipa")
    assert [e.summary for e in events.events] == ["Dentist, Dr Peeters"]
    owner_events.add_calendar("patipa_shared_by_other", "Patipa (other)")
    reconfigure(monkeypatch, calendars=None)
    both = await call("list_events")(calendar="Patipa")
    assert code(both) == "invalid_argument" and SHARED in both.error and "patipa_shared_by_other" in both.error


async def test_a_calendars_description_says_what_it_holds(server):
    """What lets an agent put a medical appointment in the medical calendar rather than the default."""
    server.add_calendar("rdv_shared_by_owner", "RDV", description="Medical appointments")
    result = await call("list_calendars")()
    by_name = {c.name: c for c in result.calendars}
    assert by_name["RDV"].description == "Medical appointments" and by_name["Patipa"].description is None


async def test_one_writable_calendar_is_the_default(server):
    del server.calendars["personal"]
    result = await call("list_calendars")()
    assert [c.name for c in result.calendars if c.default_for_new_events] == ["Patipa"]


# ── list_events ──────────────────────────────────────────────────────────


async def test_a_weekly_event_keeps_its_local_time_across_the_clock_change(owner_events):
    result = await call("list_events")(start="2026-10-20", end="2026-11-05")
    yoga = [e for e in result.events if e.summary == "Yoga"]
    # 22 October is summer time (+02:00), 29 October and after winter time (+01:00).
    assert [e.start for e in yoga] == ["2026-10-22T19:00+02:00", "2026-10-29T19:00+01:00", "2026-11-05T19:00+01:00"]
    assert all(e.repeat == "FREQ=WEEKLY;BYDAY=TH" for e in yoga)
    assert [e.occurrence for e in yoga] == [e.start for e in yoga]
    assert all(e.event == f"{SHARED}/yoga.ics" and e.calendar == "Patipa" for e in yoga)


async def test_dates_are_whole_days_and_the_end_date_is_included(owner_events):
    result = await call("list_events")(start="2026-11-18", end="2026-11-18")
    assert [e.summary for e in result.events] == ["Dentist, Dr Peeters"]
    dentist = result.events[0]
    assert (dentist.start, dentist.end) == ("2026-11-18T14:00+01:00", "2026-11-18T14:30+01:00")
    assert dentist.location == "Rue Haute 12, Brussels" and dentist.description == "Bring the X-ray"
    assert dentist.occurrence is None and dentist.repeat is None and dentist.all_day is None
    assert (result.start, result.end) == ("2026-11-18", "2026-11-18")   # dates stay dates: the last day listed
    assert dentist.weekday == "Wednesday"


async def test_an_all_day_event_shows_its_last_day_not_the_day_after(owner_events):
    result = await call("list_events")(start="2026-11-21", end="2026-11-22")
    (holiday,) = [e for e in result.events if e.summary == "Ardennes weekend"]
    assert (holiday.start, holiday.end, holiday.all_day) == ("2026-11-21", "2026-11-22", True)


async def test_without_dates_the_week_from_today_is_listed(owner_events):
    result = await call("list_events")()
    assert (result.start, result.end) == ("2026-11-14", "2026-11-20")
    assert [e.summary for e in result.events] == ["Call the vet", "Dentist, Dr Peeters", "Yoga"]


async def test_events_are_sorted_with_all_day_events_first_on_their_day(owner_events):
    owner_events.put_object(SHARED, "late.ics", event_text("late-1", "Late dinner", "20261121T200000",
                                                           "20261121T220000"))
    result = await call("list_events")(start="2026-11-21", end="2026-11-21")
    assert [e.summary for e in result.events] == ["Ardennes weekend", "Late dinner"]


async def test_text_filters_on_title_place_and_description(owner_events):
    by_title = await call("list_events")(start="2026-11-01", end="2026-12-31", text="dentist")
    by_place = await call("list_events")(start="2026-11-01", end="2026-12-31", text="rue haute")
    by_notes = await call("list_events")(start="2026-11-01", end="2026-12-31", text="x-ray")
    assert [e.summary for e in by_title.events] == [e.summary for e in by_place.events] \
        == [e.summary for e in by_notes.events] == ["Dentist, Dr Peeters"]


async def test_one_calendar_by_name_or_id(owner_events):
    owner_events.put_object("personal", "mine.ics", event_text("m-1", "Agent's own", "20261118T100000",
                                                               "20261118T110000"))
    by_name = await call("list_events")(start="2026-11-18", end="2026-11-18", calendar="personal")
    by_id = await call("list_events")(start="2026-11-18", end="2026-11-18", calendar="PATIPA")
    assert [e.summary for e in by_name.events] == ["Agent's own"]
    assert [e.summary for e in by_id.events] == ["Dentist, Dr Peeters"]


async def test_an_unknown_calendar_is_not_found_and_names_the_calendars(owner_events):
    result = await call("list_events")(calendar="RDV")
    assert code(result) == "not_found"
    assert "Patipa" in result.error and "Personal" in result.error


async def test_a_calendar_hidden_by_the_setting_is_not_found(monkeypatch, owner_events):
    reconfigure(monkeypatch, calendars="Patipa")
    result = await call("list_events")(calendar="Personal")
    assert code(result) == "not_found"


@pytest.mark.parametrize("args, words", [
    ({"start": "next week"}, "YYYY-MM-DD"),
    ({"start": "2026-11-20", "end": "2026-11-19"}, "before"),
    ({"start": "2026-01-01", "end": "2027-06-01"}, "366"),
    ({"limit": 0}, "limit"),
])
async def test_bad_arguments_are_refused_before_any_request(server, args, words):
    result = await call("list_events")(**args)
    assert code(result) == "invalid_argument" and words in result.error
    assert server.requests == []


async def test_the_limit_cuts_the_list_and_says_how_many_there_were(owner_events):
    result = await call("list_events")(start="2026-10-01", end="2026-12-31", limit=3)
    assert len(result.events) == 3 and result.truncated is True and result.total > 3
    assert any("limit" in n for n in result.notes)


async def test_a_long_description_is_cut_and_marked(server):
    long = "word " * 1000
    server.put_object(SHARED, "long.ics", event_text("long-1", "Long", "20261118T100000", "20261118T110000",
                                                     extra=f"DESCRIPTION:{long.strip()}"))
    result = await call("list_events")(start="2026-11-18", end="2026-11-18")
    (event,) = result.events
    assert len(event.description) <= 2000 and event.description_truncated is True


async def test_a_server_that_does_not_expand_is_named(server):
    server.expands = False
    server.put_object(SHARED, "yoga.ics", YOGA)
    result = await call("list_events")(start="2026-11-01", end="2026-11-30")
    assert any("expand" in n for n in result.notes)


async def test_a_day_lists_nothing_from_the_day_before(server):
    # sabre matches an all-day event at midnight UTC: asked for 20 November in Brussels (from
    # 19 November 23:00 UTC), it also answers with the 19th's. The tools keep only what is in the range.
    server.put_object(SHARED, "eve.ics", event_text("eve-1", "Day before", "20261119", "20261120"))
    server.put_object(SHARED, "late.ics", event_text("late-1", "Late on the 19th", "20261119T233000",
                                                     "20261119T235900"))
    server.put_object(SHARED, "day.ics", event_text("day-1", "The day", "20261120", "20261121"))
    server.put_object(SHARED, "after.ics", event_text("after-1", "Day after", "20261121", "20261122"))
    result = await call("list_events")(start="2026-11-20", end="2026-11-20")
    assert [e.summary for e in result.events] == ["The day"]


async def test_an_evening_west_of_utc_lists_that_days_all_day_event(monkeypatch, server):
    # 20:00 in New York is 01:00 UTC the next day: past the 20th at midnight UTC, where sabre ends it.
    reconfigure(monkeypatch, timezone="America/New_York")
    server.put_object(SHARED, "day.ics", event_text("day-1", "The day", "20261120", "20261121"))
    result = await call("list_events")(start="2026-11-20T20:00", end="2026-11-20T23:00")
    assert [e.summary for e in result.events] == ["The day"]


async def test_a_calendar_that_fails_is_named_and_the_others_list(owner_events):
    owner_events.failing_calendars.add("personal")
    result = await call("list_events")(start="2026-11-18", end="2026-11-18")
    assert result.error is None and [e.summary for e in result.events] == ["Dentist, Dr Peeters"]
    assert any("Personal" in n and "could not be read" in n for n in result.notes)


async def test_when_every_calendar_fails_it_is_a_refusal(owner_events):
    owner_events.failing_calendars.update(owner_events.calendars)
    result = await call("list_events")(start="2026-11-18", end="2026-11-18")
    assert code(result) == "calendar_unavailable"


async def test_an_event_that_cannot_be_read_is_left_out_with_a_note(monkeypatch, owner_events):
    owner_events.garbled[owner_events.href(SHARED, "no-start.ics")] = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:b\r\nSUMMARY:No start\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n")
    owner_events.garbled[owner_events.href(SHARED, "garbage.ics")] = "not iCalendar at all"
    result = await call("list_events")(start="2026-11-18", end="2026-11-18")
    assert [e.summary for e in result.events] == ["Dentist, Dr Peeters"]
    assert any("could not be read" in n and "Patipa" in n for n in result.notes)
    # Nor does it stop a create, whose duplicate check reads the same range.
    reconfigure(monkeypatch, calendars="Patipa")
    created = await call("create_event")(summary="Haircut", start="2026-11-18T10:00")
    assert created.error is None


async def test_a_reading_credential_may_list(readonly, owner_events):
    result = await call("list_events")(start="2026-11-18", end="2026-11-18")
    assert result.error is None and len(result.events) == 1


async def test_list_events_logs_no_title_or_text(owner_events, usage_log):
    await call("list_events")(start="2026-11-18", end="2026-11-18", text="dentist")
    (row,) = usage_log
    assert row["tool"] == "list_events" and "dentist" not in str(row["params"])


# ── create_event ─────────────────────────────────────────────────────────


async def test_create_a_timed_event_lasts_an_hour_by_default(monkeypatch, server):
    reconfigure(monkeypatch, calendars="Patipa")
    result = await call("create_event")(summary="Haircut", start="2026-11-19T10:30")
    assert result.error is None and result.action == "created"
    assert (result.event.start, result.event.end) == ("2026-11-19T10:30+01:00", "2026-11-19T11:30+01:00")
    assert result.event.calendar == "Patipa" and result.event.event.startswith(SHARED + "/")
    (text,) = server.objects_in(SHARED).values()
    assert "DTSTART;TZID=Europe/Brussels:20261119T103000" in text
    assert "BEGIN:VTIMEZONE" in text and "TZID:Europe/Brussels" in text
    assert icalendar.Calendar.from_ical(text).walk("VEVENT")[0]["uid"] in result.event.event
    put = [r for r in server.requests if r.method == "PUT"][0]
    assert put.headers["if-none-match"] == "*"


async def test_create_with_an_offset_keeps_the_moment(monkeypatch, server):
    reconfigure(monkeypatch, calendars="Patipa")
    result = await call("create_event")(summary="Call with Montreal", start="2026-11-19T15:00-05:00",
                                         end="2026-11-19T16:00-05:00")
    assert (result.event.start, result.event.end) == ("2026-11-19T21:00+01:00", "2026-11-19T22:00+01:00")


async def test_create_an_all_day_event_stores_the_day_after_as_its_end(monkeypatch, server):
    reconfigure(monkeypatch, calendars="Patipa")
    result = await call("create_event")(summary="Conference", start="2026-12-01", end="2026-12-03",
                                        location="Ghent", description="Badge at the desk")
    assert (result.event.start, result.event.end, result.event.all_day) == ("2026-12-01", "2026-12-03", True)
    (text,) = server.objects_in(SHARED).values()
    assert "DTSTART;VALUE=DATE:20261201" in text and "DTEND;VALUE=DATE:20261204" in text
    one_day = await call("create_event")(summary="Market", start="2026-12-05")
    assert (one_day.event.start, one_day.event.end) == ("2026-12-05", "2026-12-05")


async def test_create_a_repeating_event_lists_its_occurrences(monkeypatch, server):
    reconfigure(monkeypatch, calendars="Patipa")
    result = await call("create_event")(summary="Choir", start="2026-11-17T20:00", end="2026-11-17T22:00",
                                        repeat="RRULE:FREQ=WEEKLY;BYDAY=TU;COUNT=3")
    assert result.event.repeat == "FREQ=WEEKLY;COUNT=3;BYDAY=TU"   # icalendar's order
    listed = await call("list_events")(start="2026-11-01", end="2026-12-31", text="choir")
    assert [e.start for e in listed.events] == ["2026-11-17T20:00+01:00", "2026-11-24T20:00+01:00",
                                                "2026-12-01T20:00+01:00"]


async def test_text_with_commas_and_new_lines_comes_back_as_written(monkeypatch, server):
    reconfigure(monkeypatch, calendars="Patipa")
    await call("create_event")(summary="Lunch; Anna, Bert", start="2026-11-19T12:00",
                               description="Line one\nLine two, with a comma")
    listed = await call("list_events")(start="2026-11-19", end="2026-11-19")
    (event,) = listed.events
    assert event.summary == "Lunch; Anna, Bert" and event.description == "Line one\nLine two, with a comma"


async def test_with_several_writable_calendars_and_no_setting_one_must_be_named(server):
    result = await call("create_event")(summary="Haircut", start="2026-11-19T10:30")
    assert code(result) == "invalid_argument"
    assert "Patipa" in result.error and "Personal" in result.error and "calendar" in result.error
    assert not [r for r in server.requests if r.method == "PUT"]
    named = await call("create_event")(summary="Haircut", start="2026-11-19T10:30", calendar="Patipa")
    assert named.error is None and named.event.calendar == "Patipa"


async def test_a_read_only_calendar_is_refused_before_writing(server):
    result = await call("create_event")(summary="Party", start="2026-11-19", calendar="Contact birthdays")
    assert code(result) == "calendar_read_only" and "Patipa" in result.error
    assert not [r for r in server.requests if r.method == "PUT"]


async def test_a_403_from_the_server_is_calendar_read_only(server):
    """Listed as writable, but the share was made read-only meanwhile."""
    server.refuse_writes_with = 403
    result = await call("create_event")(summary="Party", start="2026-11-19", calendar="Patipa")
    assert code(result) == "calendar_read_only"


async def test_an_event_the_server_refuses_is_named(server):
    server.refuse_writes_with = 415
    result = await call("create_event")(summary="Party", start="2026-11-19", calendar="Patipa")
    assert code(result) == "invalid_argument" and "415" in result.error


async def test_the_same_title_at_the_same_start_is_refused_as_a_duplicate(monkeypatch, owner_events):
    reconfigure(monkeypatch, calendars="Patipa")
    result = await call("create_event")(summary="dentist, dr peeters", start="2026-11-18T14:00")
    assert code(result) == "duplicate_event" and f"{SHARED}/dentist.ics" in result.error
    assert "Wednesday 2026-11-18T14:00+01:00" in result.error and "Rue Haute 12, Brussels" in result.error
    assert not [r for r in owner_events.requests if r.method == "PUT"]


@pytest.mark.parametrize("args, words", [
    ({"summary": " ", "start": "2026-11-19T10:00"}, "summary"),
    ({"summary": "X", "start": "2026-11-19T10:00", "end": "2026-11-19T09:00"}, "after"),
    ({"summary": "X", "start": "2026-11-19", "end": "2026-11-18"}, "before"),
    ({"summary": "X", "start": "2026-11-19", "end": "2026-11-20T10:00"}, "same kind"),
    ({"summary": "X", "start": "2026-11-19T10:00", "repeat": "every tuesday"}, "repeat"),
    ({"summary": "X", "start": "2026-11-19T10:00", "repeat": "BYDAY=TU"}, "FREQ"),
])
async def test_create_refuses_bad_arguments_before_writing(monkeypatch, server, args, words):
    reconfigure(monkeypatch, calendars="Patipa")
    result = await call("create_event")(**args)
    assert code(result) == "invalid_argument" and words in result.error
    assert not [r for r in server.requests if r.method == "PUT"]


async def test_a_reading_credential_may_not_create(readonly, server):
    result = await call("create_event")(summary="X", start="2026-11-19", calendar="Patipa")
    assert code(result) == "permission_denied"
    assert server.requests == []


async def test_create_logs_the_calendar_not_the_title(monkeypatch, server, usage_log):
    reconfigure(monkeypatch, calendars="Patipa")
    await call("create_event")(summary="Therapy", start="2026-11-19T10:00", description="private words")
    (row,) = usage_log
    assert "Therapy" not in str(row["params"]) and "private words" not in str(row["params"])


# ── update_event ─────────────────────────────────────────────────────────


async def test_moving_an_event_keeps_its_length_and_its_phone_fields(owner_events):
    result = await call("update_event")(event=f"{SHARED}/phone.ics", start="2026-11-17T10:00")
    assert result.error is None and result.action == "updated" and result.changed == ["start", "end"]
    assert (result.event.start, result.event.end) == ("2026-11-17T10:00+01:00", "2026-11-17T10:15+01:00")
    text = owner_events.text(SHARED, "phone.ics")
    for kept in ("X-PHONE-SPECIAL;X-PARAM=1:keep me", "CATEGORIES:Dog", "BEGIN:VALARM", "TRIGGER:-PT15M"):
        assert kept in text
    put = [r for r in owner_events.requests if r.method == "PUT"][0]
    assert put.headers["if-match"]


async def test_an_empty_string_clears_location_and_description(owner_events):
    result = await call("update_event")(event=f"{SHARED}/dentist.ics", location="", description="")
    assert sorted(result.changed) == ["description", "location"]
    text = owner_events.text(SHARED, "dentist.ics")
    assert "LOCATION" not in text and "DESCRIPTION" not in text


async def test_an_all_day_event_can_become_a_timed_one(owner_events):
    result = await call("update_event")(event=f"{SHARED}/holiday.ics", start="2026-11-21T09:00",
                                        end="2026-11-22T18:00")
    assert result.event.all_day is None and result.event.end == "2026-11-22T18:00+01:00"


async def test_nothing_to_change_is_refused(owner_events):
    result = await call("update_event")(event=f"{SHARED}/dentist.ics")
    assert code(result) == "invalid_argument"


async def test_a_repeating_event_needs_an_occurrence_or_all_occurrences(owner_events):
    result = await call("update_event")(event=f"{SHARED}/yoga.ics", summary="Hot yoga")
    assert code(result) == "occurrence_required"
    assert "occurrence" in result.error and "all_occurrences" in result.error
    assert not [r for r in owner_events.requests if r.method == "PUT"]


async def test_one_occurrence_moves_and_the_others_stay(owner_events):
    result = await call("update_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-19T19:00+01:00",
                                        start="2026-11-19T20:00", location="Studio B")
    assert result.error is None
    assert (result.event.start, result.event.occurrence) == ("2026-11-19T20:00+01:00", "2026-11-19T19:00+01:00")
    listed = await call("list_events")(start="2026-11-12", end="2026-11-26", text="yoga")
    assert [(e.start, e.location) for e in listed.events] == [
        ("2026-11-12T19:00+01:00", None), ("2026-11-19T20:00+01:00", "Studio B"), ("2026-11-26T19:00+01:00", None)]
    # Changing the same occurrence again edits its override, not a second one.
    again = await call("update_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-19T19:00+01:00",
                                       summary="Yoga (moved)")
    assert again.error is None
    text = owner_events.text(SHARED, "yoga.ics")
    assert text.count("RECURRENCE-ID") == 1 and "Studio B" in text


async def test_an_occurrence_may_be_named_in_another_offset(owner_events):
    result = await call("update_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-19T18:00Z",
                                        summary="Yin yoga")
    assert result.error is None and result.event.occurrence == "2026-11-19T19:00+01:00"


async def test_an_occurrence_that_is_not_in_the_series_is_not_found(owner_events):
    result = await call("update_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-20T19:00+01:00",
                                        summary="Yoga")
    assert code(result) == "not_found" and "occurrence" in result.error
    assert not [r for r in owner_events.requests if r.method == "PUT"]


async def test_all_occurrences_changes_the_series_but_not_its_time(owner_events):
    renamed = await call("update_event")(event=f"{SHARED}/yoga.ics", all_occurrences=True, summary="Hatha yoga",
                                         repeat="FREQ=WEEKLY;BYDAY=TH;UNTIL=20261231T235959Z")
    assert renamed.error is None and renamed.event.repeat == "FREQ=WEEKLY;UNTIL=20261231T235959Z;BYDAY=TH"
    moved = await call("update_event")(event=f"{SHARED}/yoga.ics", all_occurrences=True, start="2026-11-19T20:00")
    assert code(moved) == "invalid_argument" and "occurrence" in moved.error


async def test_an_occurrence_or_all_occurrences_on_a_single_event_is_refused(owner_events):
    result = await call("update_event")(event=f"{SHARED}/dentist.ics", occurrence="2026-11-18T14:00",
                                        summary="X")
    assert code(result) == "invalid_argument" and "does not repeat" in result.error


async def test_an_event_with_attendees_is_left_to_the_owner(server):
    server.put_object(SHARED, "meeting.ics", event_text(
        "meet-1", "Team meeting", "20261118T100000", "20261118T110000",
        extra="ORGANIZER:mailto:owner@example.test\nATTENDEE;CN=Anna:mailto:anna@example.test"))
    result = await call("update_event")(event=f"{SHARED}/meeting.ics", summary="Moved")
    assert code(result) == "event_has_attendees"
    deleted = await call("delete_event")(event=f"{SHARED}/meeting.ics")
    assert code(deleted) == "event_has_attendees"
    assert not [r for r in server.requests if r.method in ("PUT", "DELETE")]
    listed = await call("list_events")(start="2026-11-18", end="2026-11-18")
    assert listed.events[0].has_attendees is True


CONFIDENTIAL = event_text("conf-1", "Therapy", "20261119T150000", "20261119T160000",
                          extra="CLASS:CONFIDENTIAL\nLOCATION:Dr Martin\nDESCRIPTION:Private notes\n"
                                "BEGIN:VALARM\nACTION:DISPLAY\nDESCRIPTION:Therapy\nTRIGGER:-PT30M\nEND:VALARM")


async def test_a_confidential_event_is_marked_and_never_written_back_stripped(server):
    # Nextcloud gives the login only "Busy" and the times, under the whole event's ETag: writing that
    # back would erase the owner's title, place, notes and reminders.
    server.put_object(SHARED, "conf.ics", CONFIDENTIAL)
    original = server.text(SHARED, "conf.ics")
    listed = await call("list_events")(start="2026-11-19", end="2026-11-19")
    (entry,) = listed.events
    assert entry.summary == "Busy" and entry.confidential is True and entry.location is None
    updated = await call("update_event")(event=f"{SHARED}/conf.ics", start="2026-11-19T16:00")
    deleted = await call("delete_event")(event=f"{SHARED}/conf.ics")
    assert code(updated) == code(deleted) == "event_not_editable"
    assert "confidential" in updated.error and "calendar app" in updated.error
    assert not [r for r in server.requests if r.method in ("PUT", "DELETE")]
    assert server.text(SHARED, "conf.ics") == original


async def test_a_private_event_is_not_changed_either(server):
    server.put_object("personal", "priv.ics", event_text("priv-1", "Private", "20261119T150000",
                                                         "20261119T160000", extra="CLASS:PRIVATE"))
    result = await call("update_event")(event="personal/priv.ics", summary="Changed")
    assert code(result) == "event_not_editable"
    assert not [r for r in server.requests if r.method == "PUT"]


async def test_an_event_with_floating_times_is_not_changed_but_may_be_deleted(server):
    server.put_object(SHARED, "float.ics", event_text("f-1", "Floating", "20261119T100000", "20261119T110000",
                                                      tzid=None))
    server.put_object(SHARED, "float-weekly.ics", event_text("f-2", "Floating weekly", "20261105T100000",
                                                             "20261105T110000", tzid=None,
                                                             rrule="FREQ=WEEKLY;BYDAY=TH"))
    updated = await call("update_event")(event=f"{SHARED}/float.ics", end="2026-11-19T12:00")
    assert code(updated) == "event_not_editable" and "time zone" in updated.error
    one = await call("delete_event")(event=f"{SHARED}/float-weekly.ics", occurrence="2026-11-19T10:00")
    assert code(one) == "event_not_editable"
    assert not [r for r in server.requests if r.method == "PUT"]
    whole = await call("delete_event")(event=f"{SHARED}/float.ics")
    assert whole.error is None


async def test_all_occurrences_carries_a_new_title_and_place_to_changed_occurrences(owner_events):
    await call("update_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-19T19:00+01:00",
                               start="2026-11-19T20:00")
    await call("update_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-26T19:00+01:00",
                               summary="Yoga with Anna")
    result = await call("update_event")(event=f"{SHARED}/yoga.ics", all_occurrences=True, summary="Hatha yoga",
                                        location="Studio B")
    assert result.error is None
    listed = await call("list_events")(start="2026-11-12", end="2026-11-26")
    assert [(e.start, e.summary, e.location) for e in listed.events if e.event == f"{SHARED}/yoga.ics"] == [
        ("2026-11-12T19:00+01:00", "Hatha yoga", "Studio B"),
        ("2026-11-19T20:00+01:00", "Hatha yoga", "Studio B"),        # moved: keeps its time
        ("2026-11-26T19:00+01:00", "Yoga with Anna", "Studio B"),    # keeps its own title
    ]


async def test_a_new_place_or_notes_drop_the_apps_copies_of_the_old_ones(server):
    server.put_object(SHARED, "rich.ics", event_text(
        "rich-1", "Lunch", "20261119T120000", "20261119T130000",
        extra="LOCATION:Old place\nX-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE=Old place:geo:50.85,4.35\n"
              "GEO:50.85;4.35\nDESCRIPTION:Old notes\nX-ALT-DESC;FMTTYPE=text/html:<p>Old notes</p>\n"
              "X-PHONE-SPECIAL:keep me"))
    await call("update_event")(event=f"{SHARED}/rich.ics", summary="Long lunch")
    assert "X-ALT-DESC" in server.text(SHARED, "rich.ics")   # a new title leaves them be
    result = await call("update_event")(event=f"{SHARED}/rich.ics", location="New place", description="New notes")
    assert result.error is None
    text = server.text(SHARED, "rich.ics")
    for gone in ("X-APPLE-STRUCTURED-LOCATION", "GEO:", "X-ALT-DESC", "Old place", "Old notes"):
        assert gone not in text
    assert "X-PHONE-SPECIAL:keep me" in text and "New place" in text and "New notes" in text


async def test_ending_a_series_drops_the_occurrences_changed_after_its_new_end(owner_events):
    await call("update_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-26T19:00+01:00",
                               summary="Yoga with Anna")
    result = await call("update_event")(event=f"{SHARED}/yoga.ics", all_occurrences=True,
                                        repeat="FREQ=WEEKLY;BYDAY=TH;UNTIL=20261120")
    assert result.error is None and result.changed == ["repeat"]
    assert any("1 occurrence" in n and "removed" in n for n in result.notes)
    assert "RECURRENCE-ID" not in owner_events.text(SHARED, "yoga.ics")
    listed = await call("list_events")(start="2026-11-19", end="2026-12-03", text="yoga")
    assert [e.start for e in listed.events] == ["2026-11-19T19:00+01:00"]


async def test_a_series_on_set_dates_is_described_as_such(server):
    server.put_object(SHARED, "dates.ics", event_text(
        "dates-1", "Choir", "20261105T190000", "20261105T200000",
        extra="RDATE;TZID=Europe/Brussels:20261119T190000,20261203T190000"))
    result = await call("update_event")(event=f"{SHARED}/dates.ics", summary="Choir practice")
    assert code(result) == "occurrence_required"
    assert "set dates" in result.error and "None" not in result.error


async def test_one_deadline_bounds_the_whole_call(monkeypatch, owner_events):
    # Each request answers within CALDAV_TIMEOUT_SECONDS, but the call's several requests together do not.
    reconfigure(monkeypatch, timeout_seconds="0.5")
    owner_events.delay_seconds = 0.15
    started = time.monotonic()
    result = await call("update_event")(event=f"{SHARED}/dentist.ics", summary="Dentist (moved)")
    assert code(result) == "calendar_unavailable" and "CALDAV_TIMEOUT_SECONDS" in result.error
    assert time.monotonic() - started < 1.0
    assert not [r for r in owner_events.requests if r.method == "PUT"]


async def test_a_change_made_meanwhile_is_a_conflict(owner_events):
    owner_events.conflict_next_write = True
    result = await call("update_event")(event=f"{SHARED}/dentist.ics", summary="Dentist (moved)")
    assert code(result) == "calendar_conflict" and "list_events" in result.error


@pytest.mark.parametrize("event", ["../../principals/users/x", f"{SHARED}", f"{SHARED}/", "nowhere/a.ics",
                                   f"{SHARED}/../personal/a.ics", f"{SHARED}/a%2Fb.ics", "/abs/a.ics",
                                   f"{SHARED}/%2E%2E", f"{SHARED}/%2e"])
async def test_a_malformed_or_foreign_event_id_is_refused_without_a_write(owner_events, event):
    result = await call("update_event")(event=event, summary="X")
    assert code(result) in ("invalid_argument", "not_found")
    assert not [r for r in owner_events.requests if r.method in ("PUT", "DELETE", "GET")]


async def test_an_event_that_no_longer_exists_is_not_found(owner_events):
    result = await call("update_event")(event=f"{SHARED}/gone.ics", summary="X")
    assert code(result) == "not_found"


# ── delete_event ─────────────────────────────────────────────────────────


async def test_delete_a_single_event_says_what_it_deleted(owner_events):
    result = await call("delete_event")(event=f"{SHARED}/dentist.ics")
    assert result.error is None and result.action == "deleted"
    assert result.event.summary == "Dentist, Dr Peeters" and result.event.start == "2026-11-18T14:00+01:00"
    assert f"{fake_server.FakeCalDav.home}{SHARED}/dentist.ics" not in owner_events.objects
    delete = [r for r in owner_events.requests if r.method == "DELETE"][0]
    assert delete.headers["if-match"]


async def test_delete_one_occurrence_leaves_the_rest(owner_events):
    result = await call("delete_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-19T19:00+01:00")
    assert result.error is None and result.event.occurrence == "2026-11-19T19:00+01:00"
    listed = await call("list_events")(start="2026-11-12", end="2026-11-26", text="yoga")
    assert [e.start for e in listed.events] == ["2026-11-12T19:00+01:00", "2026-11-26T19:00+01:00"]
    assert "EXDATE" in owner_events.text(SHARED, "yoga.ics")


async def test_delete_a_moved_occurrence_removes_its_override_too(owner_events):
    await call("update_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-19T19:00+01:00",
                               start="2026-11-19T21:00")
    await call("delete_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-19T19:00+01:00")
    text = owner_events.text(SHARED, "yoga.ics")
    assert "RECURRENCE-ID" not in text
    listed = await call("list_events")(start="2026-11-19", end="2026-11-19", text="yoga")
    assert listed.events == []


async def test_deleting_a_repeating_event_needs_an_occurrence_or_all_occurrences(owner_events):
    result = await call("delete_event")(event=f"{SHARED}/yoga.ics")
    assert code(result) == "occurrence_required"
    assert "UNTIL" in result.error and "update_event" in result.error   # to stop it from a date on
    whole = await call("delete_event")(event=f"{SHARED}/yoga.ics", all_occurrences=True)
    assert whole.error is None and whole.event.repeat == "FREQ=WEEKLY;BYDAY=TH"
    assert not owner_events.objects_in(SHARED).get(f"{fake_server.FakeCalDav.home}{SHARED}/yoga.ics")


async def test_a_bounded_series_the_server_will_not_empty_suggests_asking_the_owner(server):
    # Nextcloud answers 500 to a series left with no occurrence at all: only possible on a bounded one.
    server.put_object(SHARED, "course.ics", event_text("course-1", "Course", "20261105T190000", "20261105T200000",
                                                       rrule="FREQ=WEEKLY;BYDAY=TH;COUNT=3"))
    server.refuse_writes_with = 500
    result = await call("delete_event")(event=f"{SHARED}/course.ics", occurrence="2026-11-19T19:00+01:00")
    assert code(result) == "calendar_unavailable"
    assert "all_occurrences=true" in result.error and "only if they agree" in result.error


@pytest.mark.parametrize("status", [500, 503, 502])
async def test_a_failing_server_never_suggests_deleting_an_endless_series(owner_events, status):
    owner_events.refuse_writes_with = status
    result = await call("delete_event")(event=f"{SHARED}/yoga.ics", occurrence="2026-11-19T19:00+01:00")
    assert code(result) == "calendar_unavailable" and "all_occurrences" not in result.error


async def test_a_bounded_series_on_a_server_in_maintenance_gets_no_hint(server):
    server.put_object(SHARED, "course.ics", event_text("course-1", "Course", "20261105T190000", "20261105T200000",
                                                       rrule="FREQ=WEEKLY;BYDAY=TH;COUNT=3"))
    server.refuse_writes_with = 503
    result = await call("delete_event")(event=f"{SHARED}/course.ics", occurrence="2026-11-19T19:00+01:00")
    assert code(result) == "calendar_unavailable" and "all_occurrences" not in result.error


async def test_delete_in_a_read_only_calendar_is_refused(server):
    server.put_object("contact_birthdays", "bday.ics", event_text("b-1", "Anna's birthday", "20261120",
                                                                  rrule="FREQ=YEARLY"))
    result = await call("delete_event")(event="contact_birthdays/bday.ics", all_occurrences=True)
    assert code(result) == "calendar_read_only"
    assert not [r for r in server.requests if r.method == "DELETE"]


async def test_a_reading_credential_may_not_delete(readonly, owner_events):
    result = await call("delete_event")(event=f"{SHARED}/dentist.ics")
    assert code(result) == "permission_denied"


# ── The password ─────────────────────────────────────────────────────────


async def test_the_password_is_only_ever_in_the_authorization_header(monkeypatch, owner_events, caplog, usage_log):
    reconfigure(monkeypatch, calendars="Patipa")
    with caplog.at_level(logging.DEBUG):
        results = [await call("list_calendars")(), await call("list_events")(start="2026-11-01", end="2026-11-30"),
                   await call("create_event")(summary="X", start="2026-11-19T10:00")]
    assert_no_password(*(r.model_dump_json() for r in results), caplog.text, usage_log)
    for request in owner_events.requests:
        assert PASSWORD not in str(request.url) and PASSWORD.encode() not in request.content
    assert repr(caldav_config.current()).count(PASSWORD) == 0


# ── The iCalendar work ───────────────────────────────────────────────────


def test_an_event_carrying_only_an_exdate_is_still_listed():
    text = event_text("x-1", "Odd", "20261119T100000", "20261119T110000",
                      extra="EXDATE;TZID=Europe/Brussels:20261126T100000")
    found, unexpanded = caldav_ical.occurrences(text)
    assert [o.summary for o in found] == ["Odd"] and not unexpanded


def test_a_new_rule_replaces_both_rules_of_an_event_with_two():
    text = event_text("two-1", "Two rules", "20261105T100000", "20261105T110000", rrule="FREQ=WEEKLY;BYDAY=TH",
                      extra="RRULE:FREQ=MONTHLY;BYMONTHDAY=1")
    stored = caldav_ical.parse_calendar(text)
    changed = caldav_ical.apply(caldav_ical.master(stored), caldav_ical.Changes(repeat="FREQ=DAILY;COUNT=3"),
                                ZoneInfo("Europe/Brussels"), NOW)
    assert changed == ["repeat"]
    written = caldav_ical.serialize(stored)
    assert written.count("RRULE") == 1 and "FREQ=DAILY;COUNT=3" in written
