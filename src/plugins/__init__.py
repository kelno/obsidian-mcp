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
- `tool_text` and `guide_text`: what the plugin adds to tool descriptions and
  to `get_vault_guide`, so a deployment without it never describes what it
  does.

`check` and `rewrite` run for `create_note`, `edit_note` (dry runs included)
and `write_file` on a `.md`, on a write that changes the note; not for the task
writers or `move_note`. docs/architecture/plugins.md says exactly when.

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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from src.config import settings
from src.services import note_lines, refusals

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
#: whether this is a dry run; returns the text to append ("" or None for none).
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
        tool_text: tool name → paragraphs the plugin adds to that tool's
            description, where its docstring has the `TOOL_TEXT_MARKER` line.
            One paragraph per line, unwrapped.
        guide_text: sections appended to `get_vault_guide`, each starting
            with a `## … (this server)` heading.
    """

    name: str
    check: Callable[[NoteWrite], Awaitable[Refusal | None]] | None = None
    rewrite: Callable[[NoteWrite], Awaitable[Rewrite]] | None = None
    tool_text: Mapping[str, str] = field(default_factory=dict)
    guide_text: str = ""


class PluginLoadError(RuntimeError):
    """A name in `PLUGINS` that names no loadable plugin."""


#: The line, in a tool's docstring, where the plugins' `tool_text` for that
#: tool goes; removed when no plugin has any.
TOOL_TEXT_MARKER = "{plugin_text}"


def describe(tool_name: str, doc: str) -> str:
    """`doc` with the loaded plugins' `tool_text` for `tool_name` in place of its marker line.

    Called while `src/mcp_server/server.py` is imported. A `PLUGINS` name that
    does not load gives the tool no plugin text here rather than crashing the
    import: the lifespan's `_load_plugins` then refuses to start with a clear
    CRITICAL line, before anything is served.

    Raises:
        ValueError: `doc` has no marker line, or more than one.
    """
    lines = doc.split("\n")
    at = [i for i, line in enumerate(lines) if line.strip() == TOOL_TEXT_MARKER]
    if len(at) != 1:
        raise ValueError(f"{tool_name}'s docstring must hold the line {TOOL_TEXT_MARKER} exactly once")
    i = at[0]
    indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    try:
        loaded = active()
    except PluginLoadError:
        loaded = ()   # the lifespan refuses to start on it (see above)
    paragraphs = [p.strip() for plugin in loaded if (p := plugin.tool_text.get(tool_name, "")).strip()]
    if not paragraphs:
        # The marker and the blank line after it go, so no double blank remains.
        end = i + 2 if i + 1 < len(lines) and not lines[i + 1].strip() else i + 1
        return "\n".join(lines[:i] + lines[end:])
    text = "\n\n".join("\n".join(indent + line if line.strip() else "" for line in p.split("\n")) for p in paragraphs)
    return "\n".join(lines[:i] + [text] + lines[i + 1:])


def tool_text(tool_name: str) -> Callable[[Callable], Callable]:
    """A decorator, under `@mcp.tool()`, putting the plugins' text into the tool's description."""

    def apply(fn: Callable) -> Callable:
        fn.__doc__ = describe(tool_name, fn.__doc__ or "")
        return fn

    return apply


def guide_text() -> str:
    """The loaded plugins' sections for `get_vault_guide`, in plugin order."""
    return "".join("\n\n" + p.guide_text.strip() for p in active() if p.guide_text.strip())


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
        except Exception as e:  # noqa: BLE001 - any import failure (a SyntaxError too) is the same refusal to start
            raise PluginLoadError(f"PLUGINS names {name!r}, which could not be imported from src/plugins/: "
                                  f"{type(e).__name__}: {e}") from e
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


def _lines(text: str) -> list[str]:
    """`text`'s lines, without their endings: CRLF, LF and lone-CR notes alike."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _out_of_scope(old: str | None, before: str, after: str) -> bool:
    """Whether a rewrite of `before` into `after` removed or altered a kept line, or touched the frontmatter.

    Which lines the caller kept is decided as `note_lines` decides it for the
    core's own rewrites; whether the rewrite kept them is exact (only the line
    ending may differ), so a rewrite that re-indents or trims a kept line is
    caught. The frontmatter span is the larger of the two texts', so adding a
    block counts as touching it.
    """
    before_lines, after_lines = _lines(before), _lines(after)
    head = max(note_lines.frontmatter_lines(before_lines), note_lines.frontmatter_lines(after_lines))
    if after_lines[:head] != before_lines[:head]:
        return True
    old_lf = None if old is None else "\n".join(_lines(old))
    changed = note_lines.changed_lines(old_lf, "\n".join(before_lines))
    kept = Counter(line for i, line in enumerate(before_lines) if i not in changed and line.strip())
    have = Counter(after_lines)
    return any(have[line] < count for line, count in kept.items())


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
            if not (isinstance(refusal, Refusal) and isinstance(refusal.code, str) and isinstance(refusal.message, str)
                    and refusal.code in refusals.BODY_CODES):
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
                block = plugin_report(shift, dry_run)
                if block is None:
                    block = ""   # nothing to say
                elif not isinstance(block, str):
                    logger.error("plugin report on %s (%s) returned %r, not a str", self.path, self.tool, type(block))
                    block = None
            except Exception:  # noqa: BLE001 - a plugin bug must not fail the write; logged and reported
                logger.exception("plugin report failed on %s (%s)", self.path, self.tool)
                block = None
            out.append(block if block is not None else (
                "\n\nplugin_error: a server plugin's report on this write failed; the write itself is "
                "as described above. Nothing for you to fix: it is logged for the server's maintainer."))
        return "".join(out) + "".join(self._errors)
