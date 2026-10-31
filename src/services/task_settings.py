"""The owner's Tasks plugin settings, read from the vault.

The task tools behave the way the owner's own Tasks plugin is configured, so
they read the plugin's settings file (`data.json` under the vault's
`.obsidian/plugins/obsidian-tasks-plugin/`, `TASKS_PLUGIN_SETTINGS_PATH`) on
every call rather than copying any of it into this server's configuration.

The file is read through the vault root's anchored descriptor
(`vault_fs.open_dir_beneath`), with no symlink followed, because it lives in a
dot-directory the ordinary tool path guard refuses on purpose. It is never
written.

- **Absent file**: the plugin's own defaults (`Settings.ts` `defaultSettings`,
  8.6.0), which is exactly what the plugin itself uses when it has never saved
  its settings. Every result says which source it used (`source`), so this is
  never a silent fallback.
- **Unreadable, not JSON, not an object, or a field of the wrong type**:
  `TasksSettingsError`, which every task tool turns into a refusal. Guessing a
  setting would make a tool disagree with the plugin while looking right.
- **`taskFormat` other than `tasksPluginEmoji`** (the Dataview format):
  refused; only the emoji format is implemented.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import PurePosixPath

#: A settings file larger than this is refused unread. The real one is a few KB.
MAX_SETTINGS_BYTES = 1024 * 1024

EMOJI_FORMAT = "tasksPluginEmoji"

# `defaultSettings` / `new StatusSettings()` in the plugin's 8.6.0 source.
_DEFAULT_CORE_STATUSES = [
    {"symbol": " ", "name": "Todo", "nextStatusSymbol": "x", "availableAsCommand": True, "type": "TODO"},
    {"symbol": "x", "name": "Done", "nextStatusSymbol": " ", "availableAsCommand": True, "type": "DONE"},
]
_DEFAULT_CUSTOM_STATUSES = [
    {"symbol": "/", "name": "In Progress", "nextStatusSymbol": "x", "availableAsCommand": True, "type": "IN_PROGRESS"},
    {"symbol": "-", "name": "Cancelled", "nextStatusSymbol": " ", "availableAsCommand": True, "type": "CANCELLED"},
]


class TasksSettingsError(Exception):
    """The settings file exists but cannot be used as the plugin would use it."""


@dataclass(frozen=True)
class StatusConfig:
    symbol: str
    name: str
    next_symbol: str
    available_as_command: bool
    type: str


@dataclass(frozen=True)
class TasksSettings:
    """The subset of the plugin's settings the task tools act on."""

    global_filter: str = ""
    global_query: str = ""
    remove_global_filter: bool = False
    task_format: str = EMOJI_FORMAT
    set_created_date: bool = False
    set_done_date: bool = True
    set_cancelled_date: bool = True
    use_filename_as_scheduled_date: bool = False
    filename_as_scheduled_date_format: str = ""
    filename_as_date_folders: tuple[str, ...] = ()
    recurrence_on_next_line: bool = False
    statuses: tuple[StatusConfig, ...] = ()
    #: "file" when read from the vault, "plugin defaults" when the file is absent.
    source: str = "plugin defaults"
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def plugin_defaults(cls) -> "TasksSettings":
        return cls.from_mapping({}, source="plugin defaults")

    @classmethod
    def from_mapping(cls, data: dict, *, source: str = "file") -> "TasksSettings":
        if not isinstance(data, dict):
            raise TasksSettingsError("the Tasks plugin settings file does not hold a JSON object")

        def get(key, kind, default):
            value = data.get(key, default)
            if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
                raise TasksSettingsError(
                    f"the Tasks plugin setting {key!r} has an unexpected value ({value!r})"
                )
            return value

        status_settings = data.get("statusSettings", {})
        if not isinstance(status_settings, dict):
            raise TasksSettingsError("the Tasks plugin setting 'statusSettings' is not an object")
        core = status_settings.get("coreStatuses", _DEFAULT_CORE_STATUSES)
        custom = status_settings.get("customStatuses", _DEFAULT_CUSTOM_STATUSES)
        statuses = tuple(_status(entry) for entry in [*_list(core, "coreStatuses"), *_list(custom, "customStatuses")])

        folders = get("filenameAsDateFolders", list, [])
        if not all(isinstance(f, str) for f in folders):
            raise TasksSettingsError("the Tasks plugin setting 'filenameAsDateFolders' is not a list of folders")

        return cls(
            global_filter=get("globalFilter", str, ""),
            global_query=get("globalQuery", str, ""),
            remove_global_filter=get("removeGlobalFilter", bool, False),
            task_format=get("taskFormat", str, EMOJI_FORMAT),
            set_created_date=get("setCreatedDate", bool, False),
            set_done_date=get("setDoneDate", bool, True),
            set_cancelled_date=get("setCancelledDate", bool, True),
            use_filename_as_scheduled_date=get("useFilenameAsScheduledDate", bool, False),
            filename_as_scheduled_date_format=get("filenameAsScheduledDateFormat", str, ""),
            filename_as_date_folders=tuple(folders),
            recurrence_on_next_line=get("recurrenceOnNextLine", bool, False),
            statuses=statuses,
            source=source,
            raw=data,
        )

    def describe(self) -> dict:
        """What a tool result says about the settings it applied."""
        return {
            "source": self.source,
            "global_filter": self.global_filter,
            "global_query": self.global_query,
            "set_created_date": self.set_created_date,
            "set_done_date": self.set_done_date,
            "set_cancelled_date": self.set_cancelled_date,
            "use_filename_as_scheduled_date": self.use_filename_as_scheduled_date,
            "statuses": [
                {"symbol": s.symbol, "name": s.name, "type": s.type} for s in self.statuses
            ],
        }


def _list(value, key: str) -> list:
    if not isinstance(value, list):
        raise TasksSettingsError(f"the Tasks plugin setting {key!r} is not a list")
    return value


def _status(entry) -> StatusConfig:
    if not isinstance(entry, dict):
        raise TasksSettingsError("a Tasks plugin status entry is not an object")
    symbol = entry.get("symbol")
    name = entry.get("name")
    next_symbol = entry.get("nextStatusSymbol")
    kind = entry.get("type", "TODO")
    available = entry.get("availableAsCommand", True)
    if not all(isinstance(v, str) for v in (symbol, name, next_symbol, kind)) or not isinstance(available, bool):
        raise TasksSettingsError(f"a Tasks plugin status entry has an unexpected shape ({entry!r})")
    return StatusConfig(symbol, name, next_symbol, available, kind)


def read_settings(root, rel_path: str) -> TasksSettings:
    """Read and validate the settings file at `rel_path` beneath vault `root`.

    Raises `TasksSettingsError` for anything but a usable file or an absent
    one. The emoji-format check is here so that no tool can act under a
    format it does not implement.
    """
    from src.services import vault_fs

    pure = PurePosixPath(rel_path)
    if pure.is_absolute() or ".." in pure.parts or not pure.name:
        raise TasksSettingsError(f"TASKS_PLUGIN_SETTINGS_PATH is not a vault-relative file path: {rel_path!r}")
    root_fd = vault_fs.open_root(root)
    try:
        parent = str(pure.parent) if str(pure.parent) != "." else "."
        try:
            dir_fd = vault_fs.open_dir_beneath(root_fd, parent)
        except FileNotFoundError:
            return TasksSettings.plugin_defaults()
        try:
            try:
                fd = os.open(pure.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
            except FileNotFoundError:
                return TasksSettings.plugin_defaults()
            try:
                size = os.fstat(fd).st_size
                if size > MAX_SETTINGS_BYTES:
                    raise TasksSettingsError(
                        f"the Tasks plugin settings file is {size:,} bytes, over the "
                        f"{MAX_SETTINGS_BYTES:,}-byte limit"
                    )
                data = os.read(fd, MAX_SETTINGS_BYTES + 1)
            finally:
                os.close(fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise TasksSettingsError(f"the Tasks plugin settings file could not be read ({exc.strerror or exc})") from None
    except vault_fs.VaultFSError as exc:
        raise TasksSettingsError(f"the Tasks plugin settings file could not be read ({exc})") from None
    finally:
        os.close(root_fd)
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TasksSettingsError(f"the Tasks plugin settings file is not valid JSON ({exc})") from None
    settings = TasksSettings.from_mapping(parsed, source="file")
    if settings.task_format != EMOJI_FORMAT:
        raise TasksSettingsError(
            f"the Tasks plugin is set to the {settings.task_format!r} task format; the task "
            f"tools implement only the emoji format ({EMOJI_FORMAT!r})"
        )
    return settings
