"""Calendar tools over CalDAV, as a plugin (`PLUGINS=["caldav"]`).

Generic: any CalDAV server (written against Nextcloud), any vault. It adds
five tools (`tools`): `list_calendars`, `list_events` (repeating events
expanded by the server into occurrences), `create_event`, `update_event` and
`delete_event`. It touches no note and no hook of the note writers.

Settings (`config`): `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` (an
app password), and optionally `CALDAV_CALENDARS` (which calendars agents see,
in order; the first writable one takes new events), `CALDAV_TIMEZONE` (else
`TASKS_TIMEZONE`) and `CALDAV_TIMEOUT_SECONDS`. A missing setting makes each
call refuse with `calendar_not_configured`; the server still starts. For a
single-user deployment only. See docs/architecture/calendar.md.
"""

from src import plugins
from src.plugins.caldav import tools

GUIDE = (
    "## Calendar (this server)\n\n"
    "The calendar tools (`list_calendars`, `list_events`, `create_event`, `update_event`, `delete_event`) read and "
    "change calendars on a calendar server (CalDAV), separate from the vault. Use them only when the user asks: "
    "about their calendar or schedule, or to add, change or remove an event. Never on your own initiative, even "
    "where a calendar would help (planning tasks, say). An event you add shows up in the "
    "owner's calendar apps at their next sync. Times are shown in the calendar's time zone (`time_zone` in the "
    "results), and times you give without an offset are read in it. An all-day event takes dates, and its `end` "
    "is its last day. Put a new event in the calendar whose name and description (`list_calendars`) fit it; the "
    "default calendar is for events that fit no other. Find an event with `list_events` (`text` searches titles, places and descriptions) and "
    "change or delete it by its `event` id. A repeating event lists as occurrences: to change or delete one, "
    "give its `occurrence`; for the whole series, all_occurrences=true. Tell the owner what you added, changed "
    "or deleted, with its date."
)

plugin = plugins.Plugin(name="caldav", tools=tools.TOOLS, guide_text=GUIDE)
