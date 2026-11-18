"""The `caldav` plugin's settings, read from the environment (and `.env`) like the core's.

They are read on each call, not at startup: a missing or wrong setting makes
every calendar tool refuse with `calendar_not_configured`, naming what to fix,
while the rest of the server keeps running. Unlike a vault plugin's guards, a
calendar that is not set up yet endangers nothing.

The password is a `SecretStr`: it reaches only the `Authorization` header of
the requests to the CalDAV server, never a result, a log line or an error.
"""

from __future__ import annotations

import functools
import ipaddress
from typing import Annotated
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.config import settings as core_settings


class CalDavSettings(BaseSettings):
    """`CALDAV_*` settings. Only the first three are required."""

    # `extra="ignore"`: the shared `.env` holds every other setting of the server.
    # A refused value is never echoed back, as for the core's settings.
    model_config = SettingsConfigDict(env_prefix="CALDAV_", env_file=".env", extra="ignore",
                                      hide_input_in_errors=True)

    #: The CalDAV server's address: its root (Nextcloud: `https://<host>/remote.php/dav`), or the
    #: login's principal or calendar home. https, or http to this machine only.
    url: str = ""
    username: str = ""
    #: An app password for that login, not the account's own password.
    password: SecretStr = SecretStr("")
    #: The calendars agents see, by name or id, in this order; the first writable one takes new
    #: events when the agent names none. Empty: every calendar of the login, and a default only
    #: when exactly one of them is writable.
    calendars: Annotated[list[str], NoDecode] = []
    #: The IANA time zone that times without an offset are read in and every time is shown in.
    #: Empty: `TASKS_TIMEZONE`, the zone the task tools use.
    timezone: str = ""
    #: How long one tool call may wait on the CalDAV server, all its requests together.
    timeout_seconds: float = Field(20.0, gt=0, le=120)

    @field_validator("calendars", mode="before")
    @classmethod
    def _split(cls, value):
        """A JSON list or a comma-separated string; each name stripped, empties dropped."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                value = json.loads(text)
            else:
                value = text.split(",")
        return [n for n in (str(v).strip() for v in value) if n]


@functools.cache
def current() -> CalDavSettings:
    """The deployment's settings, read once per process (tests clear the cache).

    Raises:
        pydantic.ValidationError: a value of the wrong type (a timeout that is not a
            number); the message never holds the value.
    """
    return CalDavSettings()


def zone_name(cfg: CalDavSettings) -> str:
    """The time zone's name: `CALDAV_TIMEZONE`, else `TASKS_TIMEZONE`."""
    return cfg.timezone.strip() or core_settings.tasks_timezone


def zone(cfg: CalDavSettings) -> ZoneInfo:
    """The time zone itself.

    Raises:
        ZoneInfoNotFoundError: an unknown zone (`problems` names it first).
    """
    return ZoneInfo(zone_name(cfg))


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def problems(cfg: CalDavSettings) -> list[str]:
    """What keeps the calendar tools from working, one sentence each; empty when nothing does."""
    found = [f"{name} is not set" for name, value in (
        ("CALDAV_URL", cfg.url), ("CALDAV_USERNAME", cfg.username),
        ("CALDAV_PASSWORD", cfg.password.get_secret_value())) if not value.strip()]
    if cfg.url.strip():
        parts = urlsplit(cfg.url.strip())
        if parts.scheme == "http" and not _loopback(parts.hostname or ""):
            found.append("CALDAV_URL must use https (http only to this machine): the password travels with "
                         "every request")
        elif parts.scheme not in ("http", "https") or not parts.hostname:
            found.append("CALDAV_URL is not an http(s) address")
        elif parts.query or parts.fragment or parts.username or parts.password:
            found.append("CALDAV_URL must hold no query, fragment or login: the login goes in CALDAV_USERNAME "
                         "and CALDAV_PASSWORD")
    try:
        zone(cfg)
    except (ZoneInfoNotFoundError, ValueError):
        found.append(f"the time zone {zone_name(cfg)!r} is unknown (CALDAV_TIMEZONE, or TASKS_TIMEZONE when it "
                     "is empty)")
    if core_settings.multi_user_mode:
        found.append("the calendar tools serve a single-user deployment only: with MULTI_USER_MODE on, one "
                     "CalDAV login would show one person's calendars to every user")
    return found
