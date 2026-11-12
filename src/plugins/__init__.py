"""Plugins: behaviour that follows one vault's own conventions, kept out of the core.

The core serves any Obsidian vault as it is (CLAUDE.md, "The core serves any
vault"). What only one vault wants (its folder layout, its naming or linking
rules) lives in a plugin: a module under `src/plugins/<name>/` exposing one
`plugin` object, a `Plugin`. A deployment loads plugins by naming them in the
`PLUGINS` setting, in the order they run; with none named, the server is the
plain core.

A plugin fills only the hooks it needs:

- `check(write)`: may refuse a note write, returning a `Refusal`; nothing is
  then written. Every plugin's check sees the caller's own text, before any
  rewrite, so a refusal always names what the agent sent.
- `rewrite(write)`: returns a `Rewrite`, the text to write and an optional
  block for the tool's result. Rewrites run in plugin order, each on the
  previous one's output. A rewrite that only reports returns the text as given.

Both run for `create_note`, `edit_note` (dry runs included) and `write_file` on
a `.md`, on a write that changes the note and whose old and new text are
readable UTF-8; not for the task writers or `move_note`.

A plugin bug never fails a write: a hook that raises, or a rewrite that
removes or alters a non-blank line the caller kept from the old note, or
changes the frontmatter block, is set aside; the write goes ahead without it,
the result says so (`plugin_error:`) and the server logs it. A name in
`PLUGINS` that does not load stops the server at startup instead: running
without a vault's guards by accident is worse than not starting.

See docs/architecture/plugins.md.
"""

from __future__ import annotations

import functools
import importlib
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from src.config import settings
from src.services import link_paths, refusals

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NoteWrite:
    """One note write, as a plugin sees it. Read-only.

    Attributes:
        tool: the tool writing (`create_note`, `edit_note`, `write_file`).
        path: the note's vault-relative path, as it will be written.
        old: the note's text before the write; None for a new note.
        new: the text about to be written (for a rewrite, after the earlier
            plugins' rewrites).
        dry_run: True when nothing will be written (`edit_note(dry_run=True)`).
        user_id: the caller's user id; None in single-user mode.
    """

    tool: str
    path: str
    old: str | None
    new: str
    dry_run: bool
    user_id: int | None


@dataclass(frozen=True)
class Refusal:
    """A plugin's refusal of a write: a refusal code (`MCP-REFUSAL`) and the message for the agent.

    The code must be listed in `src/services/refusals.py`'s `BODY_CODES`, the
    catalogue every refusal is checked against; a refusal with an unlisted
    code is treated as a plugin error and the write goes ahead.
    """

    code: str
    message: str


#: A rewrite's block for the tool's result: called with the number of lines
#: the core's later steps (the agent-edit stamp) added above the body, and
#: whether this is a dry run; returns the text to append ("" for none).
ReportRender = Callable[[int, bool], str]


@dataclass(frozen=True)
class Rewrite:
    """A plugin's rewrite: the text to write, and its block for the result."""

    text: str
    report: ReportRender | None = None


@dataclass(frozen=True)
class Plugin:
    """What a plugin module exposes as `plugin`. Every hook is optional.

    Attributes:
        name: the plugin's name, which is its module's name under `src/plugins/`.
        check: refuses a write (`Refusal`) or lets it through (None).
        rewrite: returns the text to write and its result block.
    """

    name: str
    check: Callable[[NoteWrite], Awaitable[Refusal | None]] | None = None
    rewrite: Callable[[NoteWrite], Awaitable[Rewrite]] | None = None


class PluginLoadError(RuntimeError):
    """A name in `PLUGINS` that names no loadable plugin."""


def load(names: tuple[str, ...]) -> tuple[Plugin, ...]:
    """The plugins `names` names, in that order.

    Raises:
        PluginLoadError: a name that is repeated, has no module under
            `src/plugins/`, fails to import, or whose module exposes no
            `plugin` of type `Plugin` named like the module.
    """
    loaded: list[Plugin] = []
    for name in names:
        if any(p.name == name for p in loaded):
            raise PluginLoadError(f"PLUGINS names {name!r} twice")
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except ImportError as e:
            raise PluginLoadError(f"PLUGINS names {name!r}, which could not be imported from src/plugins/: {e}") from e
        plugin = getattr(module, "plugin", None)
        if not isinstance(plugin, Plugin):
            raise PluginLoadError(f"src/plugins/{name} exposes no `plugin` of type Plugin")
        if plugin.name != name:
            raise PluginLoadError(f"src/plugins/{name}'s plugin is named {plugin.name!r}; it must be named like its module")
        loaded.append(plugin)
    return tuple(loaded)


@functools.cache
def active() -> tuple[Plugin, ...]:
    """The plugins this deployment loads (`PLUGINS`), loaded once per process.

    Raises:
        PluginLoadError: see `load`. The lifespan calls this first, so a bad
            name stops the server before it serves.
    """
    return load(tuple(settings.plugins))


def _error_line(plugin: Plugin, step: str, why: str) -> str:
    return (f"\n\nplugin_error: the server's `{plugin.name}` plugin {why} in its {step} step, so this write went "
            "ahead without it. Nothing for you to fix: it is logged for the server's maintainer.")


def _kept_lines(old: str | None, before: str) -> Counter[str]:
    """The non-blank lines of `before` that the caller kept from `old`, as `link_paths` compares lines."""
    changed = link_paths.changed_lines(old, before)
    return Counter(link_paths.compare_key(line) for i, line in enumerate(before.split("\n"))
                   if i not in changed and line.strip())


def _out_of_scope(old: str | None, before: str, after: str) -> bool:
    """Whether a rewrite of `before` into `after` removed or altered a kept line, or touched the frontmatter."""
    before_lines = before.split("\n")
    after_lines = after.split("\n")
    head = link_paths.frontmatter_lines(before_lines)
    if after_lines[:head] != before_lines[:head]:
        return True
    have = Counter(link_paths.compare_key(line) for line in after_lines)
    return any(have[key] < count for key, count in _kept_lines(old, before).items())


@dataclass
class NoteWriteRun:
    """The plugins' part in one note write: `check`, then `rewrite`, then `render` into the result.

    Built by the writer once it knows the old and new text; carries the
    plugins' result blocks and errors from one step to the next.
    """

    tool: str
    path: str
    old: str | None
    dry_run: bool
    user_id: int | None
    _reports: list[ReportRender] = field(default_factory=list)
    _errors: list[str] = field(default_factory=list)

    def _write(self, new: str) -> NoteWrite:
        return NoteWrite(self.tool, self.path, self.old, new, self.dry_run, self.user_id)

    async def check(self, new: str) -> Refusal | None:
        """The first plugin refusal of `new`, in plugin order, or None."""
        for plugin in active():
            if plugin.check is None:
                continue
            try:
                refusal = await plugin.check(self._write(new))
            except Exception:  # noqa: BLE001 - a plugin bug must not fail the write; logged and reported
                logger.exception("plugin %s: check failed on %s (%s)", plugin.name, self.path, self.tool)
                self._errors.append(_error_line(plugin, "check", "failed"))
                continue
            if refusal is None:
                continue
            if not isinstance(refusal, Refusal) or refusal.code not in refusals.BODY_CODES:
                logger.error("plugin %s: check returned %r on %s, not a Refusal with a code listed in "
                             "refusals.BODY_CODES", plugin.name, refusal, self.path)
                self._errors.append(_error_line(plugin, "check", "returned something unusable"))
                continue
            return refusal
        return None

    async def rewrite(self, new: str) -> str:
        """`new` after every plugin's rewrite, in plugin order."""
        for plugin in active():
            if plugin.rewrite is None:
                continue
            try:
                result = await plugin.rewrite(self._write(new))
            except Exception:  # noqa: BLE001 - a plugin bug must not fail the write; logged and reported
                logger.exception("plugin %s: rewrite failed on %s (%s)", plugin.name, self.path, self.tool)
                self._errors.append(_error_line(plugin, "rewrite", "failed"))
                continue
            if not isinstance(result, Rewrite) or not isinstance(result.text, str):
                logger.error("plugin %s: rewrite returned %r on %s, not a Rewrite", plugin.name, type(result), self.path)
                self._errors.append(_error_line(plugin, "rewrite", "returned something unusable"))
                continue
            if result.text != new and _out_of_scope(self.old, new, result.text):
                logger.error("plugin %s: rewrite on %s changed lines the caller kept, or the frontmatter; "
                             "set aside", plugin.name, self.path)
                self._errors.append(_error_line(plugin, "rewrite", "tried to change text the write did not touch"))
                continue
            new = result.text
            if result.report is not None:
                self._reports.append(result.report)
        return new

    def render(self, *, shift: int = 0, dry_run: bool = False) -> str:
        """The plugins' blocks for the result, then their errors."""
        out = []
        for plugin_report in self._reports:
            try:
                out.append(plugin_report(shift, dry_run))
            except Exception:  # noqa: BLE001 - a plugin bug must not fail the write; logged and reported
                logger.exception("plugin report failed on %s (%s)", self.path, self.tool)
                out.append("\n\nplugin_error: a server plugin's report on this write failed; the write itself is "
                           "as described above. Nothing for you to fix: it is logged for the server's maintainer.")
        return "".join(out) + "".join(self._errors)
