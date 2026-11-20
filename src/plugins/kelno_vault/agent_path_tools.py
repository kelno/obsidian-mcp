"""`agent_path` and `agent_path_reached_by`: the agent path tool, as two tools of the `kelno_vault` plugin.

They draw, from this vault's setup as it is, what each kind of agent meets and in what order (`agent_paths`,
the engine, holds the reading rules and why). This module gathers what the engine reads: the vault through
the caller's own root, the PC side from the snapshots the PCs write into the vault, and this server's own
texts as it serves them (its tool descriptions with every plugin's lines, its instructions, the server's part
of `get_vault_guide`, `open_area`'s instruction lines), which no script on the PC could see.

A first call returns every piece's text, which runs past a hundred thousand characters on most paths: the
result is held to `PAGE_CHARS`, and the texts that don't fit are marked, to come with the next
call once the caller passes the hashes it now holds; a text too long for any one result comes in parts, asked
for with `read_from`. See docs/architecture/agent-paths.md.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import multiprocessing
import os
import posixpath
import re
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from src import plugins
from src.auth.session import current_user_id
from src.config import MAX_NOTE_BYTES
from src.plugins.kelno_vault import agent_paths as ap
from src.plugins.kelno_vault import areas_index as _areas_index
from src.plugins.kelno_vault import rule_files as _rule_files
from src.plugins.kelno_vault.agent_path_result import (
    AgentPathResult,
    PathPiece,
    PieceLine,
    PathStart,
    PathStructure,
    PathWork,
    ReachedByResult,
    ReadFrom,
    TypicalPath,
)
from src.services import private_folders
from src.services.tool_outcomes import body_refusal as _body_refusal
from src.services.vault import _vault_root, read_bytes

#: One result's size, past the server's read cap (`MAX_READ_RESPONSE_CHARS`, 40,000) for these two tools
#: alone: a full read of a path took 12 to 20 calls at 40,000 (owner, 2026-11-18, on trial). Claude Code
#: shows a model at most `MAX_MCP_OUTPUT_TOKENS` (25,000 tokens by default, about 50,000 characters of this
#: JSON) and refuses a longer one, which the owner's PCs raise; and it saves to a file any result past 50,000
#: characters unless the tool declares a larger size in its listing (`_RESULT_META`; live check 3, 2026-11-18).
PAGE_CHARS = 80_000
#: Claude Code's `anthropic/maxResultSizeChars`: above a page, for the JSON's own spacing.
_RESULT_META = {"anthropic/maxResultSizeChars": PAGE_CHARS + 20_000}
#: Characters kept free of texts for the notes added after them (the withheld, in-parts and cut lines).
_NOTES_RESERVE = 1_000
#: What a `read_from` call carries besides the piece itself (start, work, machine, notes): a text that
#: wouldn't fit beside that in any result comes in parts.
_PART_SHELL = 1_000
#: A pointing line (a moment's `line`, a structure entry's `via`) longer than this is cut, ending with "…": the
#: whole line is in the text of the piece that points, and on a large path the lines alone ran past the result's
#: budget. A moment that doesn't stand alone keeps its whole line (`_moments`).
_VIA_MAX = 240
#: The PC hook that writes the snapshots; `run` refreshes one at once (named in the stale-snapshot notice).
_SNAPSHOT_SCRIPT = "_/claude-code-hooks/agent-paths-snapshot.py"
#: Roles whose start adds more than a role note: the relay is handed its notes by the bridge.
_ROLE_EXTRAS = {"relay": ["relay_notes"]}

logger = logging.getLogger(__name__)


# ── Reading the setup ────────────────────────────────────────────────


class _SetupError(Exception):
    """What a path needs could not be read; the message says what, for the caller."""


def _vault_files(uid) -> list[str]:
    """Every visible file of the caller's vault, vault-relative; hidden folders and files left out, as the
    file tools leave them out."""
    root = _vault_root(uid)
    out = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        base = os.path.relpath(folder, root).replace(os.sep, "/")
        for name in files:
            if not name.startswith("."):
                out.append(name if base == "." else f"{base}/{name}")
    return out


def _reader(uid):
    def read(path: str) -> str | None:
        try:
            return ap.as_text(read_bytes(path, uid, MAX_NOTE_BYTES))
        except (FileNotFoundError, ValueError, OSError):
            return None
    return read


def _snapshot(paths: list[str], read, machine: str | None, notes: list[str]) -> ap.Snapshot | None:
    """The snapshot of `machine`, or the only one, or the most recent; None when no PC has written one."""
    found: list[ap.Snapshot] = []
    for path in sorted(p for p in paths if posixpath.dirname(p) == ap.SNAPSHOT_FOLDER and p.endswith(".json")):
        try:
            found.append(ap.Snapshot.parse(read(path) or ""))
        except (ValueError, KeyError) as exc:
            notes.append(f"The snapshot {path} could not be read ({exc}); it is left out.")
    if machine:
        hit = [s for s in found if s.machine.casefold() == machine.casefold()]
        if not hit:
            raise _SetupError(f"No snapshot from the machine {machine!r} in {ap.SNAPSHOT_FOLDER}/: there are "
                              f"{', '.join(s.machine for s in found) or 'none'}.")
        return hit[0]
    if len(found) > 1:
        found.sort(key=lambda s: s.made or "", reverse=True)
        notes.append(f"Several PCs have written a snapshot ({', '.join(s.machine for s in found)}): "
                     f"{found[0].machine}'s, the most recent, is used; pass start.machine for another.")
    return found[0] if found else None


async def _served(uid) -> ap.ServedTexts:
    """This server's own texts as it serves them now, every loaded plugin's lines included."""
    from mcp.server.fastmcp import FastMCP

    from src.config import settings
    from src.mcp_server import server as _server
    from src.mcp_server.tools import vault_guide_server_part
    from src.plugins.kelno_vault.open_area import OPEN_AREA_INSTRUCTIONS

    # The full list, not the one scoped to the caller's credential: a path is about a kind of agent.
    listed = await FastMCP.list_tools(_server.mcp)
    tools = []
    for tool in listed:
        params = (tool.inputSchema or {}).get("properties", {})
        tools.append((tool.name, (tool.description or "") + "".join(
            f"\n{k}: {v.get('description', '')}" for k, v in params.items() if v.get("description"))))
    return ap.ServedTexts(tools=tools, instructions=_server.mcp.instructions or "",
                          guide=await vault_guide_server_part(uid),
                          open_area_instructions="\n".join(OPEN_AREA_INSTRUCTIONS),
                          guide_note=settings.vault_guide_note)


def _setup(uid, machine: str | None, served: ap.ServedTexts, read=None, trace: ap.Trace | None = None,
           paths: list[str] | None = None) -> ap.Setup:
    notes: list[str] = []
    paths = _vault_files(uid) if paths is None else paths
    read = read or _reader(uid)
    try:
        rule_files = _rule_files.read(uid)
    except (FileNotFoundError, ValueError, OSError, _rule_files.RuleFilesError) as exc:
        raise _SetupError(f"The vault's list of rule files ({_rule_files.RULE_FILES_PATH}) could not be read "
                          f"({exc or type(exc).__name__}): without it, no pointer can be told from a mention.")
    points_text = read(ap.STARTING_POINTS_PATH)
    if points_text is None:
        raise _SetupError(f"The starting points ({ap.STARTING_POINTS_PATH}) could not be read: they say what "
                          "each surface loads first.")
    try:
        points = ap.StartingPoints.parse(points_text)
    except ap.StartingPointsError as exc:
        raise _SetupError(str(exc))
    try:
        areas = _areas_index.parse(read(ap.AREAS_NOTE) or "")
    except _areas_index.AreasIndexError as exc:
        notes.append(f"The list of areas ({ap.AREAS_NOTE}) could not be read ({exc}): no area joins a path.")
        areas = []
    snapshot = _snapshot(paths, read, machine, notes)
    vault = ap.Vault(paths, read, rule_files, areas, snapshot.vault if snapshot else None, trace)
    return ap.Setup(vault, points, snapshot, served, notes)


# ── Whether a rule's place is reached, after a write ─────────────────
#
# Drawing every path took about a minute on the owner's server (an Atom C2750, 2026-11-19), so a write never
# draws. The kept drawing below is brought up to date in a helper process after the vault changes (`_Drawer`), and
# gives a reach map; a write answers from the map with a few file stats, or says it can't tell. See docs/architecture/agent-paths.md,
# "Reached, after a rule write".


class ReachUnknown(Exception):
    """Whether a place is reached could not be told; the message says why, in a few words."""


#: Seconds of quiet after the last request before the map is drawn again, so a burst of writes is one draw.
REDRAW_AFTER_SECONDS = 20.0
#: The helper process's niceness: the server's own requests come first.
_HELPER_NICENESS = 10
#: False draws in a thread of this process instead of the helper (tests).
DRAW_IN_HELPER = True


@dataclass(frozen=True)
class _Run:
    #: Each note or section on the path, with the vault files its chain from the start passes through.
    reached: dict[str, tuple[str, ...]]
    #: What the path read and looked up.
    trace: ap.Trace


@dataclass(frozen=True)
class ReachMap:
    """Every path, drawn once: what each reached, and what each read and looked up."""

    #: The vault's files, by their lower-case path.
    by_lower: dict[str, str]
    #: Every file the drawing read, vault path to `(its stat, its text's digest)`; None for a missing file.
    inputs: dict[str, tuple | None]
    #: Of those, what the setup reads (the starting points, the list of areas, the rule files, the snapshots):
    #: a change to one may change every path.
    setup_inputs: frozenset[str]
    #: The hook scripts the PC registers: reached as hooks.
    registered: frozenset[str]
    runs: tuple[_Run, ...]
    #: How long the drawing took, and the drawing process's peak memory (KiB; 0 where it can't be read).
    seconds: float = 0.0
    peak_kib: int = 0
    #: What the setup looked up (the areas' main notes).
    setup_trace: ap.Trace = field(default_factory=ap.Trace)
    #: The snapshots whose `made` counts: with several, it chooses the one the paths are drawn from.
    keep_made: frozenset[str] = frozenset()
    #: How many paths this update drew again (all of them on a whole drawing).
    redrawn: int = 0
    #: Why every path was drawn again, on a whole drawing (`_Kept.cause`); empty on an update.
    cause: str = ""


def _signature(full: str) -> tuple[int, int, int, int] | None:
    """What changes when a file or folder is written: its mtime and size, and its ctime and inode, since Syncthing
    writes a temp file, renames it into place and sets the source device's mtime, so a file can come back with an
    older mtime and the same size."""
    try:
        st = os.stat(full)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ctime_ns, st.st_ino


def _stat(uid, path: str) -> tuple[int, int, int, int] | None:
    return _signature(os.path.join(_vault_root(uid), path))


#: Threads taking a batch of signatures: each stat through gocryptfs is a FUSE round trip, which several threads
#: overlap. Below `_PARALLEL_FROM` paths, one thread.
_STAT_THREADS = 8
_PARALLEL_FROM = 64


def _signatures(fulls: list[str]) -> list[tuple | None]:
    """`_signature` of each path, in order, taken by several threads for a large batch."""
    global _STAT_POOL
    if len(fulls) < _PARALLEL_FROM:
        return [_signature(f) for f in fulls]
    if _STAT_POOL is None:
        _STAT_POOL = concurrent.futures.ThreadPoolExecutor(_STAT_THREADS, thread_name_prefix="agent-paths-stat")
    return list(_STAT_POOL.map(_signature, fulls, chunksize=64))


_STAT_POOL: concurrent.futures.ThreadPoolExecutor | None = None


def _content(path: str, text: str | None, keep_made: bool = False) -> str | None:
    """A file's digest for the map: a snapshot's leaves out `made`, rewritten at every session's start on the PC,
    unless `keep_made` (with several snapshots, `made` chooses one)."""
    if text is None:
        return None
    if not keep_made and _is_snapshot(path):
        try:
            data = json.loads(text)
            data.pop("made", None)
            text = json.dumps(data, sort_keys=True)
        except ValueError:
            pass
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_snapshot(path: str) -> bool:
    return posixpath.dirname(path) == ap.SNAPSHOT_FOLDER and path.endswith(".json")


def _signed_reader(uid, signed: dict):
    """`_reader`, each file's stat (taken before the read) and digest kept in `signed` at its first read."""
    base = _reader(uid)

    def read(path: str) -> str | None:
        st = _stat(uid, path)
        text = base(path)
        signed.setdefault(path, None if text is None else (st, _content(path, text)))
        return text
    return read


def _chain_files(b: ap.PathBuilder, pid: str) -> tuple[str, ...]:
    files = []
    for x in ap.chain(b, pid):
        if x.startswith(ap.VAULT_IDS):
            files.append(x.split(":", 1)[1].split("#")[0])
        elif x.startswith("memory:"):
            files.append(ap.MEMORY_NOTE)
    return tuple(dict.fromkeys(files))


def _peak_kib() -> int:
    """The drawing process's peak memory, KiB (0 where it can't be read)."""
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB on Linux
    except (ImportError, OSError):
        return 0


# ── The kept drawing ─────────────────────────────────────────────────
#
# Every path is drawn once and kept (`_Kept`), in the helper process: a call to the path tools, and the reach map a
# write answers from, both come from it. Before each use it is brought up to date from file stats: only the paths
# whose reads a change touches are drawn again, all of them when what the setup reads changed. See
# docs/architecture/agent-paths.md, "The kept drawing".


class _Files:
    """The vault's visible files (as `_vault_files` finds them), kept with each folder's signature: a file added,
    removed or renamed changes its folder's mtime, so only those folders are listed again."""

    def __init__(self, root: str):
        self.root = root
        #: Each visible folder, vault-relative ("" for the root): its signature, files and subfolders.
        self.folders: dict[str, tuple[tuple | None, list[str], list[str]]] = {}
        self._list("")

    def _list(self, rel: str) -> None:
        full = os.path.join(self.root, rel) if rel else self.root
        sig = _signature(full)  # before listing: a change while listing shows at the next look
        files, dirs = [], []
        try:
            with os.scandir(full) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        is_dir = e.is_dir()
                    except OSError:
                        is_dir = False
                    if not is_dir:
                        files.append(e.name)
                    elif not e.is_symlink():  # os.walk lists a linked folder but doesn't go in
                        dirs.append(e.name)
        except OSError:
            pass  # os.walk skips a folder it can't list
        self.folders[rel] = (sig, files, dirs)
        for d in dirs:
            self._list(f"{rel}/{d}" if rel else d)

    def _drop(self, rel: str) -> None:
        _sig, _files, dirs = self.folders.pop(rel, (None, [], []))
        for d in dirs:
            self._drop(f"{rel}/{d}" if rel else d)

    def paths(self) -> list[str]:
        return [f"{rel}/{n}" if rel else n for rel, (_s, files, _d) in self.folders.items() for n in files]

    def refresh(self) -> set[str]:
        """List again the folders whose signature changed; the vault paths added or removed since."""
        before = set(self.paths())
        rels = list(self.folders)
        now = _signatures([os.path.join(self.root, rel) if rel else self.root for rel in rels])
        changed = [rel for rel, sig in zip(rels, now) if sig != self.folders[rel][0]]
        if not changed:
            return set()
        for rel in changed:
            if rel in self.folders:
                self._drop(rel)
                self._list(rel)
        return before ^ set(self.paths())


#: Rule sections held by each note, kept by its signature for the overview's list of every rule file: per vault
#: root and rule headings, `{path: (signature, headings held)}`.
_SECTIONS: dict[tuple[str, tuple[str, ...]], dict[str, tuple]] = {}


def _run_key(s: ap.Start, w: ap.Work) -> tuple:
    return (s.surface, s.folder, s.started_by, s.role, tuple(s.extras), s.name, s.number, w.area, w.kind)


@dataclass
class _KeptRun:
    label: str
    start: ap.Start
    work: ap.Work
    b: ap.PathBuilder
    trace: ap.Trace


#: Paths drawn for calls with a start of their own (a work, a custom start), kept the same way, at most this many.
_EXTRA_RUNS = 32


class _Kept:
    """Every path of one vault root and snapshot choice, drawn once, then kept up to date (`refresh`)."""

    def __init__(self, uid, machine: str | None, served: ap.ServedTexts, files: _Files | None = None):
        began = time.monotonic()
        self.uid, self.machine, self.served = uid, machine, served
        self.root = str(_vault_root(uid))
        if files is not None:
            files.refresh()
        self.files = files or _Files(self.root)
        #: Every file read, vault path to `(its signature, its text's digest)`, None for a missing file.
        self.signed: dict[str, tuple | None] = {}
        read = _signed_reader(uid, self.signed)
        self.setup_trace = ap.Trace()
        self.setup = _setup(uid, machine, served, read, self.setup_trace, self.files.paths())
        read(_rule_files.RULE_FILES_PATH)  # read by `_setup` outside `read`: its signature joins the setup's
        snapshots = [p for p in self.signed if _is_snapshot(p) and self.signed[p] is not None]
        self.keep_made = frozenset(snapshots) if len(snapshots) > 1 else frozenset()
        for p in self.keep_made:
            self.signed[p] = (self.signed[p][0], _content(p, _reader(uid)(p), True))
        self.setup_inputs = frozenset(self.signed)
        self.setup.vault.trace = None
        self.runs = [self._draw(label, s, w) for label, s, w in ap.run_starts(self.setup)]
        self.extra: dict[tuple, _KeptRun] = {}
        self.redrawn, self.seconds = len(self.runs), round(time.monotonic() - began, 1)
        #: Why this drawing is a whole one (set by `_kept`), then why the next must be (`refresh`); empty on an
        #: update. Logged with the reach map, so the journal shows what forces whole drawings.
        self.cause = ""
        self.why = ""
        #: Whether the reach map sent out was drawn before the last change (`reach_map`).
        self.map_due = True

    def _draw(self, label: str, s: ap.Start, w: ap.Work) -> _KeptRun:
        v = self.setup.vault
        v.trace = ap.Trace()
        try:
            return _KeptRun(label, s, w, ap.run(self.setup, s, w), v.trace)
        finally:
            v.trace = None

    def refresh(self, served: ap.ServedTexts) -> bool:
        """Bring the drawing up to date with the vault; False when it must be drawn whole again (what the setup reads
        changed, or the served texts)."""
        began = time.monotonic()
        if served != self.served:
            self.why = _served_change(self.served, served)
            return False
        moved = self.files.refresh()
        read = _reader(self.uid)
        #: Each changed file's new text (None: gone).
        changed: dict[str, str | None] = {}
        signed = list(self.signed.items())
        now = _signatures([os.path.join(self.root, p) for p, _b in signed])
        for (p, before), st in zip(signed, now):
            if before is not None and st == before[0]:
                continue
            # Read again (a file that couldn't be read before, missing or too large, is tried each time): its new
            # signature, taken before the read, is kept whatever comes of it, so the file stays watched, a note read
            # only for a section that didn't change included (adversarial review, 2026-11-19).
            text = None if st is None else read(p)
            now_signed = None if text is None else (st, _content(p, text, p in self.keep_made))
            if (before is None) != (now_signed is None) or (before and now_signed and before[1] != now_signed[1]):
                changed[p] = text
            elif text is not None and _is_snapshot(p) and self.setup.snapshot is not None:
                # Only `made` changed (it is left out of the digest): the paths stand, the date follows.
                try:
                    data = json.loads(text)
                except ValueError:
                    data = {}
                if isinstance(data, dict) and data.get("machine") == self.setup.snapshot.machine:
                    self.setup.snapshot.made = data.get("made")
            self.signed[p] = now_signed
        touched = {p.lower() for p in set(changed) | moved}
        setup = sorted(p for p in set(changed) | moved if p.lower() in {s.lower() for s in self.setup_inputs}
                       or (p in changed and self.setup_trace.changed_by(p, changed[p]))
                       or (p in moved and (self.setup_trace.new_file_counts(p) or _is_snapshot(p))))
        if setup:
            self.why = "changed: " + ", ".join(
                (f"{p} (snapshot content)" if _is_snapshot(p) and p in changed and changed[p] is not None
                 else f"{p} (added or removed)" if p in moved else p) for p in setup[:5]) + (
                f" and {len(setup) - 5} more" if len(setup) > 5 else "")
            return False
        self.cause = ""
        if not touched:
            self.redrawn = 0
            return True
        v = self.setup.vault
        for p in changed:
            v.forget(p)
        if moved:
            v.set_paths(self.files.paths())

        def stale(r: _KeptRun) -> bool:
            return (any(r.trace.changed_by(p, t) for p, t in changed.items())
                    or any(r.trace.new_file_counts(p) for p in moved))

        self.redrawn = 0
        for i, r in enumerate(self.runs):
            if stale(r):
                self.runs[i] = self._draw(r.label, r.start, r.work)
                self.redrawn += 1
        self.extra = {k: r for k, r in self.extra.items() if not stale(r)}
        self.seconds = round(time.monotonic() - began, 1)
        self.map_due = True
        return True

    def run_for(self, s: ap.Start, w: ap.Work) -> ap.PathBuilder:
        """The path of `s` doing `w`: one of the kept runs, or drawn now and kept with them."""
        key = _run_key(s, w)
        for r in self.runs:
            if _run_key(r.start, r.work) == key:
                return r.b
        if key not in self.extra:
            if len(self.extra) >= _EXTRA_RUNS:
                del self.extra[next(iter(self.extra))]
            self.extra[key] = self._draw("", s, w)
        return self.extra[key].b

    def all_runs(self) -> list[tuple[str, ap.Start, ap.Work, ap.PathBuilder]]:
        return [(r.label, r.start, r.work, r.b) for r in self.runs]

    def every_rule_file(self) -> list[str]:
        """`agent_paths.every_rule_file`, each note's rule sections kept by its signature (`_SECTIONS`): only a note
        that changed since is read again."""
        v = self.setup.vault
        headings = tuple(v.rule_headings)
        kept = _SECTIONS.setdefault((self.root, headings), {})
        notes = [r for r in v.by_rel.values() if r.endswith(".md") and not v.is_rule_file(r)]
        now = dict(zip(notes, _signatures([os.path.join(self.root, r) for r in notes])))
        for gone in set(kept) - set(notes):
            del kept[gone]
        read = _reader(self.uid)

        def sections_of(r: str) -> tuple[str, ...]:
            st, hit = now.get(r), kept.get(r)
            if hit is None or st is None or hit[0] != st:
                hit = kept[r] = (st, ap.rule_sections_in(read(r) or "", list(headings)))
            return hit[1]
        return ap.every_rule_file(v, sections_of)

    def reach_map(self) -> ReachMap | str:
        """The reach map a write answers from: this drawing's paths, what they reached by which files, and every
        file they and the setup read."""
        self.map_due = False
        if self.setup.snapshot is None:
            return f"no PC has written a snapshot to {ap.SNAPSHOT_FOLDER}/, so what Claude Code loads is unknown"
        inputs = {p: s for p, s in self.signed.items()
                  if p in self.setup_inputs or any(r.trace.reads(p) for r in self.runs)}
        runs = tuple(_Run({pid: _chain_files(r.b, pid) for pid in r.b.order if pid.startswith(("note:", "section:"))},
                          r.trace) for r in self.runs)
        return ReachMap(dict(self.setup.vault.by_rel), inputs, self.setup_inputs,
                        frozenset(ap.registered_hook_scripts(self.setup)), runs, self.seconds, _peak_kib(),
                        self.setup_trace, self.keep_made, self.redrawn, cause=self.cause)


#: The kept drawings of this process, by vault root and snapshot choice (`machine`, None for the default).
_KEPT: dict[tuple[str, str | None], _Kept] = {}
_KEPT_LOCK = threading.Lock()


def _kept(uid, machine: str | None, served: ap.ServedTexts) -> _Kept:
    """The kept drawing for this root and machine, brought up to date, or drawn whole when it must be. Call with
    `_KEPT_LOCK` held. Raises _SetupError (or StartingPointsError) when a path can't be drawn."""
    key = (str(_vault_root(uid)), machine.casefold() if machine else None)
    k = _KEPT.get(key)
    if k is not None:
        try:
            if k.refresh(served):
                return k
        except Exception:
            _KEPT.pop(key, None)
            raise
        files, why = k.files, k.why
    else:
        files, why = None, "the first drawing since the server started"
    _KEPT.pop(key, None)
    k = _Kept(uid, machine, served, files)
    k.cause = why
    _KEPT[key] = k
    return k


def _served_change(before: ap.ServedTexts, now: ap.ServedTexts) -> str:
    """Which of the served texts changed, in words, for the journal: the tools by name."""
    parts = [name for name in ("instructions", "guide", "open_area_instructions", "guide_note")
             if getattr(before, name) != getattr(now, name)]
    old, new = dict(before.tools), dict(now.tools)
    tools = sorted(n for n in set(old) | set(new) if old.get(n) != new.get(n))
    if tools:
        parts.append("tools " + ", ".join(tools[:5]) + (f" and {len(tools) - 5} more" if len(tools) > 5 else ""))
    return "served texts changed: " + ("; ".join(parts) or "their order")


def draw_reach_map(uid, served: ap.ServedTexts) -> ReachMap | str:
    """Every path, drawn now on the vault as it is, not kept: the reach map, or why it can't be drawn."""
    try:
        return _Kept(uid, None, served).reach_map()
    except (_SetupError, ap.StartingPointsError) as exc:
        return str(exc)


def update_reach_map(uid, served: ap.ServedTexts) -> ReachMap | str:
    """The kept drawing brought up to date: the reach map, or why it can't be drawn."""
    with _KEPT_LOCK:
        try:
            return _kept(uid, None, served).reach_map()
        except (_SetupError, ap.StartingPointsError) as exc:
            return str(exc)


def _as_single_user(root: str) -> None:
    """In the helper process: the vault at `root` read as a single-user server reads it."""
    from src.config import settings
    from src.services import vault as vault_service

    if settings.multi_user_mode or settings.vault_path != root:
        settings.multi_user_mode = False
        settings.vault_path = root
        vault_service.clear_user_vault_cache()


def _update_in_helper(root: str, served: ap.ServedTexts) -> ReachMap | str:
    _as_single_user(root)
    return update_reach_map(None, served)


def _call_in_helper(root: str, kind: str, args: tuple, served: ap.ServedTexts):
    _as_single_user(root)
    return _answer_call(None, kind, args, served)


def _lower_priority() -> None:
    try:
        os.nice(_HELPER_NICENESS)
    except (AttributeError, OSError):
        pass


_HELPER: concurrent.futures.ProcessPoolExecutor | None = None


def _helper() -> concurrent.futures.ProcessPoolExecutor:
    """One helper process, started at its first use and kept: it imports the server's code once."""
    global _HELPER
    if _HELPER is None:
        _HELPER = concurrent.futures.ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn"), initializer=_lower_priority)
    return _HELPER


def _drop_helper() -> None:
    """Forget a helper process that died, so the next draw starts a new one."""
    global _HELPER
    if _HELPER is not None:
        _HELPER.shutdown(wait=False, cancel_futures=True)
        _HELPER = None


_HELPER_STOP_SECONDS = 3.0


def stop_helper() -> None:
    """End the helper process, a drawing under way included: asked to stop, then terminated if it is still
    running after `_HELPER_STOP_SECONDS` (a whole drawing takes about a minute on the server). The executor's own
    shutdown waits for the work under way; the server's shutdown can't."""
    global _HELPER
    helper, _HELPER = _HELPER, None
    if helper is None:
        return
    # Its worker processes, read before the shutdown forgets them: the executor has no public list (Python 3.14's
    # `terminate_workers` would do this).
    workers = list((getattr(helper, "_processes", None) or {}).values())
    helper.shutdown(wait=False, cancel_futures=True)
    for p in workers:
        p.join(_HELPER_STOP_SECONDS)
        if p.is_alive():
            p.terminate()
            p.join(_HELPER_STOP_SECONDS)


async def helper_lifespan() -> None:
    """The plugin's `startup` task: it waits while the server serves, and at shutdown, when the lifespan cancels
    it, ends the helper process, so it never outlives the server's worker."""
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.to_thread(stop_helper)


class _Drawer:
    """The reach map of one vault root, drawn again after changes: one draw at a time, a burst of requests one
    draw, `REDRAW_AFTER_SECONDS` after the last of them (at once for the first)."""

    def __init__(self, root: str):
        self.root = root
        self.map: ReachMap | None = None
        #: Why the last draw failed; "" when it didn't.
        self.failure = ""
        self._due = 0.0
        self._task: asyncio.Task | None = None

    def request(self, uid) -> None:
        self._due = time.monotonic() + (REDRAW_AFTER_SECONDS if self.map is not None or self.failure else 0.0)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._draw(uid))

    def took(self, result: "ReachMap | str") -> None:
        """A drawing's reach map, or why it failed: kept for the writes' answers, and logged."""
        if isinstance(result, str):
            self.failure = result
            return
        self.map, self.failure = result, ""
        logger.info("agent paths: reach map drawn in %.1f s (redrew %d of %d paths%s), drawing process peak memory "
                    "%d MiB", result.seconds, result.redrawn, len(result.runs),
                    f": {result.cause}" if result.cause else "", result.peak_kib // 1024)

    async def _draw(self, uid) -> None:
        while True:
            while (wait := self._due - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            due = self._due
            try:
                served = await _served(uid)
                if DRAW_IN_HELPER:
                    result = await asyncio.get_running_loop().run_in_executor(
                        _helper(), _update_in_helper, self.root, served)
                else:
                    result = await asyncio.to_thread(update_reach_map, uid, served)
            except Exception as exc:  # noqa: BLE001 — a failed draw is said on the next write, never raised
                logger.warning("agent paths: drawing the reach map failed", exc_info=True)
                result = f"drawing the agent paths failed ({type(exc).__name__})"
                if isinstance(exc, BrokenProcessPool):
                    _drop_helper()
            self.took(result)
            if self._due == due:  # no request came while drawing
                return


_DRAWERS: dict[str, _Drawer] = {}


def _drawer(uid) -> _Drawer:
    root = str(_vault_root(uid))
    return _DRAWERS.setdefault(root, _Drawer(root))


def warm_reach_map(uid, path: str) -> None:
    """On a note write with no rule place: start the first drawing of the vault's paths, if none is under way, so
    a rule write finds it ready; after a write to a note the map read, draw it again."""
    d = _drawer(uid)
    if d.map is None:
        if (d._task is None or d._task.done()) and not d.failure:
            d.request(uid)
    elif path.replace("\\", "/").lstrip("/") in d.map.inputs:
        d.request(uid)


_REDRAWING = "the agent paths are being drawn again after changes to notes they read"


def _answer(uid, m: ReachMap, path: str, created: bool, places: list[str]) -> tuple[set[str], bool]:
    """Which of `places` a path reaches once `path` is written, from the map: `(reached, whether the map is now
    stale)`. Raises ReachUnknown when the map can't tell."""
    written = path.replace("\\", "/").lstrip("/")
    read = _reader(uid)

    def changed(p: str) -> bool:
        before = m.inputs[p]
        st = _stat(uid, p)
        if before is None or st is None:
            return (before is None) != (st is None)
        return st != before[0] and _content(p, read(p), p in m.keep_made) != before[1]

    known = written.lower() in m.by_lower
    moved = {p.lower() for p in m.inputs if p.lower() != written.lower() and changed(p)}
    if moved & {p.lower() for p in m.setup_inputs}:
        raise ReachUnknown(_REDRAWING)
    # A note the map didn't have counts as new, whether or not this write creates it.
    new = created or not known
    touched = (any(r.trace.reads(written) or (new and r.trace.new_file_counts(written)) for r in m.runs)
               or (new and m.setup_trace.new_file_counts(written)))
    stale = bool(moved) or touched
    found = set()
    for place in places:
        kind, rest = place.split(":", 1)
        note, sep, heading = rest.partition("#")
        pid = f"{kind}:{m.by_lower.get(note.lower(), note)}{sep}{heading}"
        # Reached when a path reached it by a chain none of whose notes changed since; the written note is the
        # place's own, and a note's own text never cuts the way to it.
        if pid in m.registered or any(pid in r.reached and not {f.lower() for f in r.reached[pid]} & moved
                                      for r in m.runs):
            found.add(place)
            continue
        # Not reached then. Still not, unless this write is to a note some path reads or looked for (a new rule
        # section in a note opened for its sections, say), or a note some path read changed since: it may now
        # point here, or to a note that does.
        if touched or moved:
            raise ReachUnknown(_REDRAWING)
    return found, stale


async def reached_after_write(uid, path: str, places: list[str], created: bool = False) -> set[str]:
    """Which of `places` (`note:<path>`, `section:<path>#<heading>`) some agent path reaches once the write to `path`
    lands (`created`: it creates the note): what the path tools count as reached (`agent_paths.reached_places`), answered from the reach map.

    Raises ReachUnknown, saying why, when it can't tell: no map yet, the last drawing failed, or changes since it
    was drawn that may reach these places. Each time, and after a write to a note some path reads, the map is
    drawn again.
    """
    d = _drawer(uid)
    m = d.map
    if m is None:
        d.request(uid)
        raise ReachUnknown(d.failure or "the agent paths are being drawn for the first time since the server "
                                        "started")
    try:
        found, stale = await asyncio.to_thread(_answer, uid, m, path, created, places)
    except ReachUnknown:
        d.request(uid)
        raise
    if stale or d.failure:
        d.request(uid)
    return found


# ── Shaping the result ───────────────────────────────────────────────


def _piece(p: ap.Piece, known: dict | None) -> PathPiece:
    status = None if known is None else ("unchanged" if known.get(p.id) == p.hash
                                         else "changed" if p.id in known else "new")
    path = p.id.split(":", 1)[1].split("#")[0] if p.id.startswith(ap.VAULT_IDS) else None
    lines = _lines(p)
    every_time, when = _every_time_or_when(p)
    if status == "unchanged":
        # The caller holds this piece from an earlier call: its moments, if it waits on one, and its hash are
        # enough; the rest (what brings it, where, the pointing lines) came then. Keeps a call with `known` small, so
        # withheld texts always find room on the next one.
        return PathPiece(id=p.id, when=when, lines=_no_lines(lines), hash=p.hash, status=status)
    return PathPiece(
        id=p.id, every_time=every_time, when=when, where=p.where, chars=p.chars, hash=p.hash, lines=lines,
        status=status, private=True if path and private_folders.is_private(path) else None)


def _every_time_or_when(p: ap.Piece) -> tuple[str | None, list[str] | None]:
    """A piece on the path whatever the conversation brings: what brings it; otherwise the moments that bring it, each
    once, in the order met (round 5: `stage`, `always` and `arrives` gave way to these)."""
    if p.always:
        return p.arrives, None
    when: list[str] = []
    for e in p.moments:
        if e["moment"] not in when:
            when.append(e["moment"])
    return None, when or ["unplaced"]


def _lines(p: ap.Piece) -> list[PieceLine] | None:
    """Every line pointing to the piece, with the piece it sits in and its moment; the line whole when its moment
    doesn't stand alone (`ap.stands_alone`), else the part holding the moment and the link (`_window`). A line read
    as soon as it is met, in a piece that waits on a moment, gives that piece's moment (round 5: the line is the one
    pointing here, where `through` named the pieces between)."""
    direct = {src: line for _, src, line in p.arrivals if src}
    out: list[PieceLine] = []
    for e in p.moments:
        if not e["piece"]:
            continue
        src = e["through"][-1] if e["through"] else e["piece"]
        line = direct.get(src, e["line"]) if e["through"] else e["line"]
        if line:
            line = " ".join(line.split()) if not ap.stands_alone(e["moment"]) else _window(line, e["moment"], p.id)
        # A line with no moment of its own holds whenever its piece is read: no `when`, as in the short form (Agent
        # paths, 2026-11-21: "when met" read as a condition beside an every_time piece).
        x = PieceLine(piece=src, line=line, when=None if e["moment"] == "when met" else e["moment"])
        if x not in out:
            out.append(x)
    return out or None


def _window(line: str, moment: str | None, pid: str) -> str:
    """A long line cut around its pointer: from the start of the sentence holding the moment, past the link to the
    piece, about `_VIA_MAX` characters; a link far beyond comes after " … " with a little around it (test bench,
    2026-11-20: cut from the line's start, 7 of 45 lines lost the moment or the link)."""
    flat = " ".join(line.split())
    if len(flat) <= _VIA_MAX:
        return flat
    low = flat.lower()
    at = low.find((moment or "")[:40].lower()) if moment else -1
    start = 0
    if at > 0:
        start = max(low.rfind(". ", 0, at), low.rfind("? ", 0, at), low.rfind("! ", 0, at)) + 2
        start = 0 if start < 2 else start
    end = min(len(flat), start + _VIA_MAX)
    name = posixpath.basename(pid.split(":", 1)[1].split("#")[0]).removesuffix(".md").lower() if pid.startswith(
        ap.VAULT_IDS) else ""
    link = low.find(name, start) if name else -1
    head = ("…" if start else "") + flat[start:end]
    if link < 0 or link + len(name) <= end:
        return head + ("…" if end < len(flat) else "")
    if link + len(name) + 2 - start <= _VIA_MAX + 120:  # a little longer, to hold the link
        end = min(len(flat), link + len(name) + 2)
        return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")
    a, b = max(end, link - 60), min(len(flat), link + len(name) + 20)
    return head + " … " + flat[a:b] + ("…" if b < len(flat) else "")


def _no_lines(lines: list[PieceLine] | None) -> list[PieceLine] | None:
    """The short form's lines: without their text, except one whose moment doesn't read right without it ("When
    choosing it"); the lines come with the piece's details (owner: the short form stays short)."""
    if not lines:
        return lines
    return [x if x.when and not ap.stands_alone(x.when) else x.model_copy(update={"line": None}) for x in lines]


_DETAILS = ("every_time", "where", "chars")


def _short_form(o: PathPiece) -> dict:
    """Strip a piece to its moments, if it waits on one, and its hash (no `when`: it is there every time); the details
    taken, to give back with its text (what brings it, `every_time`, longer than the old `stage`, filled a page)."""
    taken = {k: getattr(o, k) for k in (*_DETAILS, "lines")}
    for k in _DETAILS:
        setattr(o, k, None)
    o.lines = _no_lines(o.lines)
    return taken


def _with_texts(result, pieces: list[ap.Piece], out: list[PathPiece], notes: list[str],
                known: dict | None = None, fill_order: list[str] | None = None) -> None:
    """Fill in the texts of new or changed pieces, in path order, while the result stays within the budget.

    Sets `result.notes`. A first call (no `known`) lists every piece: those whose text waits for a later call
    in short form (id, `when` for one waiting on a moment, lines without their text, hash), their details (what
    brings it, where,
    the pointing lines) coming with their text, so the whole path shows at once. A call with `known` lists only what it brings:
    the pieces with their text this time, those now marked in parts, and new private ones; the pieces held
    and those still waiting were in the first call, so they are left out and counted (`waiting`), and a removal
    shows in `left_path` (owner, 2026-11-18). Measured on the owner's vault, repeating them took most of each
    call: a backward question's 150 pieces in short form left room for one text or two. Every call makes
    progress: the first text due either arrives or, longer than the room left, is marked to come in parts
    (`read_from`, one call, which carries the piece alone).

    `fill_order` (a backward question's): the order the texts fill a page, the place first, whatever their order
    on the path; the pieces stay listed in path order.
    """
    budget = PAGE_CHARS - _NOTES_RESERVE
    listing = known is not None
    order = {id(o): i for i, o in enumerate(out)}
    due = [(p, o) for p, o in zip(pieces, out) if o.status != "unchanged" and not o.private]
    if fill_order is not None:
        at = {pid: n for n, pid in enumerate(fill_order)}
        due.sort(key=lambda po: at.get(po[0].id, len(at)))
    else:
        # The rule pieces' texts first, the tool texts after them, wherever the tools load on the path: listed
        # before the moments since round 3, the tool texts (over 100,000 characters) would otherwise fill the first
        # pages.
        due.sort(key=lambda po: ap.tool_text(po[0]))
    if listing:
        result.pieces = [o for o in out if o.status != "unchanged" and o.private]
        # The chains and the structure came with the first call too, unchanged by a later one (owner,
        # 2026-11-18: 9,000 characters a call on a backward question); a call without known shows them again.
        result.structure = None
        if hasattr(result, "reached_by"):
            result.reached_by = result.other_pieces = None
        notes.append("The structure (and for a backward question, who reaches the place and the other pieces' lines) "
                     "comes only with a call without known: call once without it if you haven't had them, or to "
                     "see them again.")
    details = {id(o): _short_form(o) for _, o in due}
    used = len(result.model_dump_json())
    given: list[PathPiece] = []
    count = {"in_parts": 0, "waiting": 0}
    #: Once a rule piece on the path whatever the conversation brings waits for a later call, no piece waiting on a
    #: moment comes before it (test bench, 2026-11-20: typical 5's page 1 sent moment pieces and withheld an always
    #: one, so a reader paging through met them out of order). The tool texts come after every rule piece (`due`),
    #: so one waiting holds nothing back.
    always_held = False
    #: Once a rule piece waits, no tool text comes before it (test bench, 2026-11-20: typical 3's page 2 carried all
    #: 34 of the bridge's tool texts while Vault conventions waited for page 3).
    rule_held = False

    def restore(o: PathPiece) -> None:
        for k, v in details[id(o)].items():
            setattr(o, k, v)

    for p, o in due:
        restore(o)
        full = len(o.model_dump_json())
        _short_form(o)
        meta = full + 1 if listing else full - len(o.model_dump_json())  # what listing it whole adds
        cost = len(json.dumps(p.text, ensure_ascii=False)) + len(',"text":') + meta
        whole = budget - full - _PART_SHELL  # a read_from call, this piece alone
        if cost > whole or (not given and used + cost > budget):
            # Too long for any one result, or for the room this one leaves when it is the first text due:
            # either way it comes in parts (`read_from`), so every call makes progress.
            restore(o)
            o.text_in_parts = True
            count["in_parts"] += 1
            if listing:
                result.pieces.append(o)
            used = len(result.model_dump_json())
            continue
        in_order = fill_order is not None or (
            (p.always or not always_held) and not (rule_held and ap.tool_text(p)))
        if used + cost <= budget and in_order:
            restore(o)
            o.text, used = p.text, used + cost
            given.append(o)
            if listing:
                result.pieces.append(o)
            continue
        always_held = always_held or p.always
        rule_held = rule_held or not ap.tool_text(p)
        if listing:
            count["waiting"] += 1
        else:
            o.text_withheld, used = True, used + len(',"text_withheld":true')
    if listing:
        result.pieces.sort(key=lambda o: order[id(o)])

    def texts(n: int) -> str:
        return f"{n} text is" if n == 1 else f"{n} texts are"

    def withheld_note() -> list[str]:
        lines = []
        if listing and count["waiting"]:
            lines.append(f"{texts(count['waiting'])} still to come (waiting): their pieces were in an earlier call. "
                         "Call again with the same arguments, passing in known the hash of every piece whose text you "
                         "now hold, and they come next with their details.")
        n = withheld()
        if n:
            lines.append(f"{texts(n)} not in this result (text_withheld, counted in waiting): those pieces come in "
                         "short form: `when` for one waiting on a moment (none: it is there every time), the lines "
                         "pointing to it without their text (a moment that doesn't read right alone keeps its line). "
                         "Call again with the same arguments, passing in known the hash of every piece "
                         "whose text you now hold, and only what's new comes next, with these texts (read_from is "
                         + ("for a text marked text_in_parts, or a piece listed in other_pieces)."
                            if hasattr(result, "other_pieces") else "only for a text marked text_in_parts)."))
        if count["in_parts"]:
            lines.append(f"{texts(count['in_parts'])} too long for the room in this result (text_in_parts): call "
                         "again with the same arguments and read_from={id, offset: 0} for each; each part gives the "
                         "next offset.")
        return lines

    def withheld() -> int:
        return sum(1 for o in result.pieces or [] if o.text_withheld)

    def take_back(cap: int) -> None:
        while len(given) > 1 and len(result.model_dump_json()) > cap:
            last = given.pop()  # the estimate fell short: the last texts given go back (never the first)
            last.text = None
            _short_form(last)
            if listing:
                result.pieces.remove(last)
                count["waiting"] += 1
            else:
                last.text_withheld = True
            result.waiting = count["waiting"] if listing else (withheld() or None)
            result.notes = (notes + withheld_note()) or None

    take_back(budget)
    _fit_metadata(result, notes, budget)
    result.waiting = count["waiting"] if listing else (withheld() or None)
    result.notes = (notes + withheld_note()) or None
    take_back(PAGE_CHARS)


def _fit_metadata(result, notes: list[str], cap: int) -> None:
    """Past the budget even in short form (some four hundred pieces), the last pieces waiting for their text
    are left out, then the structure's lists cut short; said in the notes."""

    def size():
        return len(result.model_dump_json())

    pieces = result.pieces or []
    dropped = 0
    while size() > cap and pieces and pieces[-1].text is None:
        pieces.pop()
        dropped += 1
    st = result.structure
    if st is not None:
        for name in _FAULT_LISTS:
            items = getattr(st, name)
            while items and size() > cap:
                items.pop()
                st.cut = sorted(set((st.cut or []) + [name]))
    if dropped:
        notes.append(f"The last {dropped} pieces are left out: even in short form, the pieces pass the size "
                     "budget. Call again passing in known the hashes of the pieces you hold, and the rest come "
                     "next.")
    if st is not None and st.cut:
        _note_cut(notes, st)


def _fit_overview(result) -> None:
    """Keep the overview within the budget: the structure lists are cut short from the last typical agent
    back, then those across every area, each cut said in its `cut` and in the notes."""
    cap = PAGE_CHARS - _NOTES_RESERVE
    structures = [t.structure for t in reversed(result.typical or [])] + [result.across_every_area]
    cut = False
    for st in structures:
        for name in _FAULT_LISTS:
            items = getattr(st, name) if st else None
            while items and len(result.model_dump_json()) > cap:
                items.pop()
                st.cut, cut = sorted(set((st.cut or []) + [name])), True
    # Then the piece lines, from the last typical agent back, each ending on how many are left out.
    for t in reversed(result.typical or []):
        left = 0
        while len(t.pieces) > 1 and len(result.model_dump_json()) > cap:
            if left:
                t.pieces.pop()  # the "… more" line
            t.pieces.pop()
            left += 1
            t.pieces.append(f"… {left} more: pass start={{typical: {t.number}}} for the whole path")
        cut = cut or bool(left)
    if cut:
        result.notes = (result.notes or []) + [
            "This overview is cut short to fit the size budget (each structure's cut says which of its lists, "
            "and a cut path ends on how many pieces it leaves out): pass start for one agent's path in full."]


def _one_part(result, p: ap.Piece, o: PathPiece, offset: int) -> None:
    """Make `result` carry one part of `p`'s text, from `offset`, as much as fits; `o` alone in its pieces."""
    if o.private:
        raise _SetupError(f"{p.id} is in a private folder: its text is not shown.")
    if offset > len(p.text):
        raise _SetupError(f"read_from.offset {offset} is past the end of {p.id} ({len(p.text)} characters).")
    result.pieces = [o]
    o.text_offset = offset
    room = PAGE_CHARS - _NOTES_RESERVE - len(result.model_dump_json()) - len(
        ',"text":,"next_offset":0000000')
    n = max(1, min(len(p.text) - offset, room))
    while n > 1 and len(json.dumps(p.text[offset:offset + n], ensure_ascii=False)) > room:
        n = max(1, n * 9 // 10)
    o.text = p.text[offset:offset + n]
    end = offset + len(o.text)
    o.next_offset = end if end < len(p.text) else None
    result.notes = (result.notes or []) + [
        f"Part of {p.id}: characters {offset} to {end} of {len(p.text)}. "
        + (f"Next: read_from={{id: {json.dumps(p.id, ensure_ascii=False)}, offset: {end}}}." if o.next_offset
           else "That was the last part; its hash is the whole text's.")]


def _part_of(result, pieces: list[ap.Piece], out: list[PathPiece], read_from: ReadFrom,
             lined: set[str] | None = None):
    """The call asked for one part of one text: that piece alone, without the rest of the result. A
    `tool:<server>:*` line's id gives its texts whole instead, `offset` counting texts (Agent paths, 2026-11-20:
    read_from refused the id the line showed, and the line named none of its members); `lined`, when given, is
    the ids the lines were made from, so the texts are those the line counts."""
    group = _TOOL_GROUP.fullmatch(read_from.id)
    if group:
        members = [(p, o) for p, o in zip(pieces, out) if ap.tool_text(p) and p.id.split(":")[1] == group[1]
                   and (lined is None or p.id in lined)]
        if not members:
            raise _SetupError(f"read_from.id {read_from.id!r}: no tool texts of {group[1]} on this path.")
        result.structure = result.left_path = None
        _group_texts(result, members, read_from.id, read_from.offset)
        return result
    hit = next(((p, o) for p, o in zip(pieces, out) if p.id == read_from.id), None)
    if hit is None:
        raise _SetupError(f"read_from.id {read_from.id!r} is not on this path.")
    result.structure = result.left_path = None
    _one_part(result, hit[0], hit[1], read_from.offset)
    return result


#: The id of one server's tool texts as one line (`_piece_lines`).
_TOOL_GROUP = re.compile(r"tool:([^:*]+):\*")


def _group_texts(result, members: list[tuple[ap.Piece, PathPiece]], gid: str, offset: int) -> None:
    """Make `result` carry the texts of `members` whole, from the `offset`-th, as many as fit (at least one)."""
    if offset >= len(members):
        raise _SetupError(f"read_from.offset {offset} is past the last text of {gid} ({len(members)} texts).")
    result.pieces = []
    for p, o in members[offset:]:
        o.text = p.text
        result.pieces.append(o)
        if len(result.pieces) > 1 and len(result.model_dump_json()) > PAGE_CHARS - _NOTES_RESERVE:
            result.pieces.pop()
            o.text = None
            break
    end = offset + len(result.pieces)
    result.notes = (result.notes or []) + [
        f"{gid}: texts {offset + 1} to {end} of {len(members)}, each whole. "
        + (f"Next: read_from={{id: {json.dumps(gid, ensure_ascii=False)}, offset: {end}}}." if end < len(members)
           else "That was the last of them.")]


def _changed_hook_scripts(setup: ap.Setup, piece_ids) -> list[str]:
    """Notes for each hook on the path whose script, a vault file read live here, differs from the one the
    snapshot ran (format 2's `hook_scripts`): its message on the path may be out of date."""
    snap = setup.snapshot
    if not snap or not snap.hook_scripts:
        return []
    out, seen = [], set()
    for pid in piece_ids:
        if not pid.startswith("hook:"):
            continue
        pid = pid.split("#")[0]  # a hook's messages at other events are `hook:<event>/<script>#<n>`
        script = pid.split("/", 1)[-1]
        if pid in seen:
            continue
        seen.add(pid)
        for sid, then in snap.hook_scripts.items():
            if not (sid.startswith("note:") and posixpath.basename(sid) == script):
                continue
            now = setup.vault.text(sid[len("note:"):])
            if then and now is not None and ap.digest(now) != then:
                # The remedy comes with the notice, so no rule has to teach it (owner, 2026-11-18).
                out.append(f"{sid[len('note:'):]} changed since the snapshot ({snap.made}): the message of {pid} "
                           f"here may be out of date. Refresh it from a Claude Code session on {snap.machine}: "
                           f"`python {_SNAPSHOT_SCRIPT} run` in the vault (any new session there refreshes it "
                           f"too); from claude.ai, ask the owner or a desk session to.")
    return out


def _left_path(known: dict | None, on_path) -> list[str] | None:
    """The pieces the caller holds that are no longer on the path: a removal, which no status could show."""
    gone = [pid for pid in (known or {}) if pid not in on_path]
    return gone or None


def _clip_vias(items: list[dict] | None) -> list[dict] | None:
    """The list with each line cut at `_VIA_MAX`."""
    return None if items is None else [{**x, "via": _short(x["via"], _VIA_MAX)} if x.get("via") else x
                                       for x in items]


def _grouped_from(items: list[dict] | None) -> list[dict] | None:
    """A list of pointers with the entries alike but for `from` as one, `from` a list (round 4, cold read
    2026-11-20: one line copied into 26 area main notes gave 26 entries in `pointers_outside_rule_files`)."""
    if items is None:
        return None
    out: dict[str, dict] = {}
    for x in items:
        rest = {k: v for k, v in x.items() if k != "from"}
        e = out.setdefault(json.dumps(rest, sort_keys=True), {"from": [], **rest})
        if x.get("from") not in e["from"]:
            e["from"].append(x.get("from"))
    return list(out.values())


#: The structure's lists of faults.
_FAULT_LISTS = ("unplaced_pointers", "pointers_outside_rule_files", "broken_pointers", "skipped_imports",
                "import_warnings")


def _structure(st: dict) -> PathStructure:
    """The structure as the result gives it: the path's size, and each list of faults only when it holds any (round
    5: faults only; `into_areas` and the mention lists went, and empty lists with them)."""
    out = PathStructure(
        size=st.get("size"), unplaced_pointers=_grouped_from(_clip_vias(st.get("unplaced_pointers"))),
        pointers_outside_rule_files=_grouped_from(_clip_vias(st.get("pointers_outside_rule_files"))),
        broken_pointers=_grouped_from(_clip_vias(st.get("broken_pointers"))),
        skipped_imports=st.get("skipped_imports"), import_warnings=st.get("import_warnings"),
        not_followed=st.get("not_followed"))
    for name, value in list(vars(out).items()):
        if value == []:
            setattr(out, name, None)
    return out


def _short(s: str | None, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _overview_when(p: ap.Piece) -> str:
    """A waiting piece's moments on one line, without their lines (room: five paths side by side, owner: the short
    form stays short): each once, ` | ` between them."""
    out = []
    for e in p.moments:
        m = _short(e["moment"], 60)
        if m not in out:
            out.append(m)
    return _short(" | ".join(out), 150) or "unplaced"


def _overview_lines(b: ap.PathBuilder, known: dict | None) -> list[str]:
    """One line per piece, each server's tool texts as one, as the prototype's overview prints them."""
    return _piece_lines([b.pieces[pid] for pid in b.order], known, numbered=True)


def _piece_lines(pieces: list[ap.Piece], known: dict | None, numbered: bool, tag=None) -> list[str]:
    """One line per piece (`<id>  <chars> chars  #<hash>`, its status against `known`, `when:` its moments when it waits
    on one), each server's tool texts as one line, where its first one comes; `tag(pieces)`, when given, ends each
    line."""
    tag = tag or (lambda ps: "")
    lines, tools, at = [], {}, {}
    for p in pieces:
        if ap.tool_text(p):
            server = p.id.split(":")[1]  # instructions:<server>, tool:<server>:<name>, …
            if server not in tools:
                at[server] = len(lines)
                lines.append(None)
            tools.setdefault(server, []).append(p)
            continue
        status = "" if known is None else (" [unchanged]" if known.get(p.id) == p.hash
                                           else " [changed]" if p.id in known else " [new]")
        when = "" if p.always else f"  when: {_overview_when(p)}"
        lines.append(f"{p.id}  {p.chars:,} chars  #{p.hash}{status}{when}{tag([p])}")
    for server, texts in tools.items():
        combined = ap.digest("".join(p.hash for p in texts))
        kinds = "instructions, guide, tools" if server == ap.SERVER else "instructions, tools"
        lines[at[server]] = (f"tool:{server}:* ({len(texts)} texts: {kinds})  {sum(p.chars for p in texts):,} "
                             f"chars  #{combined}{tag(texts)}")
    return [f"{n}. {line}" for n, line in enumerate(lines, 1)] if numbered else lines


def _start_of(start: PathStart, points: ap.StartingPoints) -> ap.Start:
    if start.typical is not None:
        if any(v is not None for k, v in start.model_dump().items() if k not in ("typical", "machine")):
            raise _SetupError("start.typical names a typical agent whole: give it alone (start.machine aside).")
        hit = next((t for t in points.typical if t.number == start.typical), None)
        if hit is None:
            raise _SetupError(f"No typical agent {start.typical}: {ap.STARTING_POINTS_PATH} has "
                              f"{', '.join(str(t.number) for t in points.typical)}.")
        return hit
    if start.surface is None:
        raise _SetupError("start needs a surface (claude-code or claude-ai), or a typical agent's number.")
    return ap.Start(start.surface, start.folder, start.started_by, start.role, list(_ROLE_EXTRAS.get(start.role, [])))


def _failed(result_type, prose: str, code: str = "invalid_argument"):
    outcome = _body_refusal(prose, code)
    result = result_type(error=str(outcome))
    result._body_outcome = outcome
    return result


# ── The tools ────────────────────────────────────────────────────────


def _answer_call(uid, kind: str, args: tuple, served: ap.ServedTexts):
    """A path tool's call, answered from the kept drawing (brought up to date first): `("ok", result, reach map or
    None)` or `("error", why, None)`. The reach map comes back when the drawing changed since the last one sent, so
    the writes answer from the same drawing."""
    machine = (args[0].machine if args[0] else None) if kind == "path" else args[2]
    with _KEPT_LOCK:
        try:
            k = _kept(uid, machine, served)
            result = _path_result(k, *args) if kind == "path" else _reached_by_result(k, *args)
        except (_SetupError, ap.StartingPointsError) as exc:
            return "error", str(exc), None
        return "ok", result, (k.reach_map() if machine is None and k.map_due else None)


async def _in_drawing(uid, kind: str, args: tuple):
    """Run a path tool's call where the kept drawing lives: the helper process, or a thread of this one (tests)."""
    served = await _served(uid)
    if DRAW_IN_HELPER:
        try:
            status, out, reach = await asyncio.get_running_loop().run_in_executor(
                _helper(), _call_in_helper, str(_vault_root(uid)), kind, args, served)
        except BrokenProcessPool:
            _drop_helper()
            raise
    else:
        status, out, reach = await asyncio.to_thread(_answer_call, uid, kind, args, served)
    if reach is not None:
        _drawer(uid).took(reach)
    return status, out


def _path_result(kept: _Kept, start: PathStart | None, work: PathWork | None, known: dict | None,
                 read_from: ReadFrom | None) -> AgentPathResult:
    setup = kept.setup
    notes = list(setup.notes)
    snap = setup.snapshot
    common = {"machine": snap.machine if snap else None, "snapshot_made": snap.made if snap else None}
    if start is None:
        if read_from is not None:
            raise _SetupError("read_from needs the start (and work) of the call that gave text_in_parts.")
        runs = kept.all_runs()
        typical = [_typical_path(label, s, b, known) for label, s, _w, b in runs[: len(setup.points.typical)]]
        notes.append("Pass start (a typical agent's number, or a surface and folder) for one path with its "
                     "texts.")
        unreached = ap.unreached_rule_files(setup, runs, kept.every_rule_file())
        if unreached:
            notes.append(_NO_PATH_NOTE)
        across = _structure(ap.across_runs(runs))
        result = AgentPathResult(typical=typical, rule_files_no_path_reaches=unreached,
                                 across_every_area=across, notes=notes, **common)
        _fit_overview(result)
        return result
    s = _start_of(start, setup.points)
    w = ap.Work(work.area, work.kind) if work else ap.Work()
    b = kept.run_for(s, w)
    if s.role and not b.role_picked and not _ROLE_EXTRAS.get(s.role):
        # Nothing on this path offers a choice of role note (on claude.ai, no session-start hook does):
        # said, so a path drawn for a start that isn't a real case isn't taken for its answer (owner).
        notes.append(f"role: {s.role} changed nothing: no line on this path picks a role note.")
    if b.role_unpicked:
        notes.append("No role given: a line on this path names several role notes, so each waits on its role (its "
                     "moments say which); pass start.role for that role's path.")
    if w.area and any(x.get("from") == "work.area" for x in b.broken):
        notes.append(f"work.area {w.area!r} names no area of {ap.AREAS_NOTE} (by its name, its main note's name, "
                     "a related folder, its slug or its tag): the path goes on without one.")
    notes += _changed_hook_scripts(setup, b.order)
    pieces = [b.pieces[i] for i in b.order]
    out = [_piece(p, known) for p in pieces]
    result = AgentPathResult(
        start={("typical" if k == "number" else k): v for k, v in vars(s).items() if v}, work={k: v for k, v in vars(w).items() if v} or None,
        pieces=out, left_path=_left_path(known, b.order),
        structure=_structure(ap.structure_of(b)), notes=notes or None, **common)
    if known is None and read_from is None:  # the structure comes only then, and the note on the fields with it
        notes.append(_FIELD_NOTE)
    result.notes = notes or None
    if read_from is not None:
        return _part_of(result, pieces, out, read_from)
    _with_texts(result, pieces, out, notes, known)
    return result


def _reached_by_result(kept: _Kept, place: str, known: dict | None, machine: str | None,
                       read_from: ReadFrom | None) -> ReachedByResult:
    setup = kept.setup
    notes = list(setup.notes)
    r = ap.reached_by(setup, place.strip(), kept.all_runs())
    notes += _changed_hook_scripts(setup, r["pieces"])
    if r["matched_by"].startswith("nothing"):
        notes.append(_NO_PATH_NOTE)
    every = list(r["pieces"].values())
    # The path up to the place, with texts; every other piece on those paths as a line (owner, 2026-11-19, round
    # 3: six places gave the same 215 pieces, about 470,000 characters).
    pieces = [r["pieces"][i] for i in r["texts"]]
    out = [_grouped(_piece(p, known), p, r["met"]) for p in pieces]
    held = set(r["texts"])
    snap = setup.snapshot
    result = ReachedByResult(
        place=r["place"], matched_by=r["matched_by"], reached_by=r["reached_by"],
        machine=snap.machine if snap else None, snapshot_made=snap.made if snap else None, pieces=out,
        other_pieces=_other_pieces(r, [p for p in every if p.id not in held]) or None,
        left_path=_left_path(known, r["pieces"]), structure=_structure(r["structure"]), notes=notes or None)
    if known is None and read_from is None:  # the structure comes only then, and its note with it
        if result.other_pieces:
            notes.append(_OTHER_PIECES_NOTE)
        if r["other_versions"]:
            notes.append(_VERSIONS_NOTE)
        if out:  # a place no path reaches has no pieces to explain
            notes += [_FIELD_NOTE, _GROUPS_NOTE]
    result.notes = notes or None
    if read_from is not None:  # any piece on those paths, not only those listed with their text
        result.reached_by = result.other_pieces = None
        return _part_of(result, every, [_grouped(_piece(p, known), p, r["met"]) for p in every], read_from,
                        lined={p.id for p in every if p.id not in held})
    if known is None:
        _trim_structure(result, pieces, notes)
    _with_texts(result, pieces, out, notes, known, fill_order=r["fill_order"])
    return result


def _grouped(o: PathPiece, p: ap.Piece, met: dict) -> PathPiece:
    """A backward question's piece with the groups whose agents have it every time, and those whose agents have it
    only when its moment comes (test bench, 2026-11-20: Personal agent showed "read at the start" beside a Claude
    Code group whose agents meet it only when its moment comes; round 5: `groups` and `other_stages` gave way to
    these)."""
    mine = sorted(met.get(p.id, {}).items())
    o.every_time_for = [n for n, (_stage, always) in mine if always] or None
    o.when_for = [n for n, (_stage, always) in mine if not always] or None
    return o


def _typical_path(label: str, s: ap.Start, b: ap.PathBuilder, known: dict | None) -> TypicalPath:
    """One typical agent in the overview, headed by what it reads every time (round 5: the overview's headline)."""
    st = ap.structure_of(b)
    size = st["size"]
    return TypicalPath(
        number=s.number, name=label,
        # The tool texts aren't all in front of the agent from the start: Claude Code defers them, and claude.ai
        # shows a description's first line until the chat searches its tools (Agent paths, 2026-11-21).
        every_time=(f"{size['always_chars']:,} characters in {size['always_pieces']} pieces every time, plus up to "
                    f"{size['tool_text_chars']:,} in {size['tool_texts']} tool texts as tools are loaded"),
        pieces=_overview_lines(b, known), structure=_structure(st))


def _other_pieces(r: dict, pieces: list[ap.Piece]) -> list[str]:
    """A backward question's `other_pieces`: one line per piece (`_piece_lines`), ending on the groups that have
    it when not every group does; the place's note, when the place is a section of it, says so; then one line per
    piece with other versions of its text, counting them and naming who meets them (round 4, cold read 2026-11-20:
    24 lines for the area-start hook, one per folder)."""
    every = set(range(1, len(r["reached_by"]) + 1))

    def tag(ps: list[ap.Piece]) -> str:
        has = sorted({n for p in ps for n in r["met"].get(p.id, {})})
        out = "" if set(has) == every else f"  (groups {', '.join(map(str, has))})"
        if r["section_of"] in {p.id for p in ps}:
            out += "  (the place's note: the place is its section, given in pieces)"
        return out

    lines = _piece_lines(pieces, None, numbered=False, tag=tag)
    for e in r["other_versions"]:
        n = len(e["hashes"])
        lines.append(f"{e['id']}  {n} other version{'s' if n > 1 else ''} of its text, on the paths of "
                     f"{_who(e['agents'])}")
    return lines


def _who(labels: list[str]) -> str:
    """Agents' labels, short: the per-folder workers and per-area chats each as one phrase listing the folders or
    areas."""
    kinds = (("a Claude Code worker in ", "Claude Code workers in "), ("a claude.ai chat working on ",
                                                                       "claude.ai chats working on "))
    out, lists = [], {}
    for label in labels:
        kind = next((k for k in kinds if label.startswith(k[0])), None)
        if kind is None:
            out.append(label)
            continue
        if kind not in lists:
            lists[kind] = []
            out.append(kind)
        lists[kind].append(label[len(kind[0]):])
    return "; ".join(x if isinstance(x, str) else (x[0] + lists[x][0] if len(lists[x]) == 1
                                                  else x[1] + ", ".join(lists[x])) for x in out)


def _trim_structure(result, pieces: list[ap.Piece], notes: list[str]) -> None:
    """When the texts would not all fit in one page beside the rest, but would without some of the structure's
    lists, those lists are cut first, longest first, before any text waits for a later call (owner, round 3: a
    pointer from 26 area main notes filled it). When the texts can't all fit anyway, nothing is cut: they come
    over several calls, and the structure whole with the first."""
    st = result.structure
    if st is None:
        return
    texts = sum(len(json.dumps(p.text, ensure_ascii=False)) + len(',"text":') for p in pieces)
    room = PAGE_CHARS - _NOTES_RESERVE - texts
    names = _FAULT_LISTS
    lists = sum(len(json.dumps(getattr(st, n) or [], ensure_ascii=False)) for n in names)
    if len(result.model_dump_json()) - lists > room:
        return
    while len(result.model_dump_json()) > room:
        longest = max(names, key=lambda n: len(getattr(st, n) or []))
        items = getattr(st, longest) or []
        if len(items) <= 1:
            break
        del items[(len(items) + 1) // 2:]
        st.cut = sorted(set((st.cut or []) + [longest]))
    if st.cut:
        _note_cut(notes, st)


_CUT_NOTE = "Some lists of structure are cut short to keep the result within its size budget"


def _note_cut(notes: list[str], st: PathStructure) -> None:
    """The one note on the structure's cut lists, replacing an earlier one (test bench, 2026-11-20: two notes, one
    for the room left for the texts and one for the budget, said the same)."""
    notes[:] = [n for n in notes if not n.startswith(_CUT_NOTE)]
    notes.append(f"{_CUT_NOTE} ({', '.join(st.cut)}).")


async def agent_path_impl(start: PathStart | None, work: PathWork | None, known: dict | None,
                          read_from: ReadFrom | None = None) -> AgentPathResult:
    status, out = await _in_drawing(current_user_id.get(), "path", (start, work, known, read_from))
    return out if status == "ok" else _failed(AgentPathResult, out)


async def agent_path_reached_by_impl(place: str, known: dict | None, machine: str | None,
                                     read_from: ReadFrom | None = None) -> ReachedByResult:
    status, out = await _in_drawing(current_user_id.get(), "reached_by", (place, known, machine, read_from))
    return out if status == "ok" else _failed(ReachedByResult, out)


#: Said with `other_pieces`: the result models' comments aren't served, and a writer asked how to read a line's text
#: (Agent paths, 2026-11-20).
_OTHER_PIECES_NOTE = ("other_pieces: every other piece on those paths, one line each; read any of them with the same "
                      "arguments and read_from={id, offset: 0}; a tool:<server>:* line gives its texts whole, as "
                      "many as fit, offset counting texts.")


#: Said when a piece has another text on another path (test bench, 2026-11-20: an area-start hook's messages, one
#: per area, stood under one id).
_VERSIONS_NOTE = ("Some pieces have another text on other paths (an area-start hook's message names the area): "
                  "`pieces` and `read_from` give the one the chain's agents meet; other_pieces ends on one line "
                  "per such piece, counting its other versions and naming who meets them; agent_path for one of "
                  "them gives that agent's text.")


#: What the fields mean, on a call without `known` (the result models' comments aren't served; round 4, cold read
#: 2026-11-20: `always`, `placed` and `after` were guessed; round 5 replaced them).
_FIELD_NOTE = ("every_time: the piece is on the path whatever the conversation brings, and this is what brings it; "
               "when: it arrives only when one of these moments comes; lines: each line pointing to it, the piece it "
               "sits in and the moment it gives (none: it holds whenever that piece is read).")


#: Said with a backward question's groups (round 4, cold read 2026-11-20: which agent has which piece was guessed).
_GROUPS_NOTE = ("group: each reached_by entry is one group, agents meeting the place by the same chain; a piece's "
                "every_time_for and when_for name the groups whose agents have it every time, or only when its "
                "moment comes (its every_time or when are as the first of them meets it); a line in other_pieces "
                "ends on its groups when not every group has it. The pieces fill pages in this order: the place, "
                "what arrives with it, what points to it and the chain, then the start pieces.")


#: Said in a result that reports a place no path reaches: a cold reader took it for `not_followed` (2026-11-19).
_NO_PATH_NOTE = (
    "No path reaches (`rule_files_no_path_reaches`, or `matched_by` \"nothing\") means the path tools followed every "
    "way they know that rules reach agents, and none leads there: either nothing sends an agent there, or something "
    "does by a way they don't follow, which they must then be taught. `not_followed` is different: what the tools "
    "know of and leave out, because they can't read it (such as claude.ai's own system prompt) or by design (the "
    "tool's description lists those); a rule reached only through one of them also shows as reached by no path.")


#: What the tools leave out by design, said once in both descriptions (round 5: it was a line of every path's
#: `not_followed`, read from the vault's Where agent instructions go; the wording is the Agent paths session's,
#: 2026-11-21) [P277, P287].
BY_DESIGN = (
    "What the path tools don't follow, by design: in a repo, anything beyond what Claude Code loads there (its "
    "`CLAUDE.md` files and their imports), whose docs you read by hand with the repo's files in front of you; a "
    "reference with no link or path; a wildcard path, or a placeholder path other than the vault's note patterns "
    "(`<folder>/<folder>.md`, `Reference/Servers/<name>/<name>.md`); what isn't a rule file, a hook's message or a "
    "tool's text, such as a code comment, a URL or another repo's file; an import Claude Code itself skips. When one "
    "of these matters, point to it from a rule file the tools follow, in one of the pointer shapes of Where agent "
    "instructions go.")


_A_START = Field(description="Who the agent is; omit it for the five typical agents side by side.")
_A_WORK = Field(description="What it was given to do.")
_A_KNOWN = Field(description="{piece id: hash} of the texts you hold: only new or changed ones come back.")
_A_READ_FROM = Field(description="{id, offset}: the next part of a text in parts, with the same other arguments.")
_A_PLACE = Field(description="A vault path, `note.md#Heading`, `tool:obsidian-mcp:<tool>` or "
                             "`hook:<event>/<script>`; it may not exist yet.")
_A_MACHINE = Field(description="The PC whose snapshot to use; default the latest.")


async def agent_path(
    start: Annotated[PathStart | None, _A_START] = None,
    work: Annotated[PathWork | None, _A_WORK] = None,
    known: Annotated[dict[str, str] | None, _A_KNOWN] = None,
    read_from: Annotated[ReadFrom | None, _A_READ_FROM] = None,
) -> AgentPathResult:
    return await agent_path_impl(start, work, known, read_from)


async def agent_path_reached_by(
    place: Annotated[str, _A_PLACE],
    known: Annotated[dict[str, str] | None, _A_KNOWN] = None,
    machine: Annotated[str | None, _A_MACHINE] = None,
    read_from: Annotated[ReadFrom | None, _A_READ_FROM] = None,
) -> ReachedByResult:
    return await agent_path_reached_by_impl(place, known, machine, read_from)


#: The descriptions, set here so both can carry `BY_DESIGN` (a docstring can't be built from a constant).
agent_path.__doc__ = f"""What an agent of this setup meets, in order: every rule text on its path, with a hash.

    Without `start`: the five typical agents side by side. `structure` lists the faults found, only when there are
    any. Who meets one rule, and the path up to it: `agent_path_reached_by`.

    {BY_DESIGN}

    Field reference, for maintaining this tool: https://github.com/kelno/obsidian-mcp/blob/main/docs/architecture/agent-paths.md
    """
agent_path_reached_by.__doc__ = f"""Which agents of this setup meet a rule's place, by which chain, and what they have in front of them then.

    `agent_path` backward: use it before adding or changing a rule. Texts come for the place first, what points
    to it and arrives with it, each chain, and the start pieces its agents share; the other pieces on their paths
    as lines, readable with `read_from`. Its pieces, `structure`, `known` and `read_from` work as in `agent_path`.

    {BY_DESIGN}
    """


def _path_refusal(message: str) -> AgentPathResult:
    return AgentPathResult(error=message)


def _reached_by_refusal(message: str) -> ReachedByResult:
    return ReachedByResult(error=message)


TOOLS = (
    plugins.PluginTool(
        name="agent_path", fn=agent_path, logged_args=("start", "work"), title="Agent path",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        refusal_result=_path_refusal, result_type=AgentPathResult, meta=_RESULT_META),
    plugins.PluginTool(
        name="agent_path_reached_by", fn=agent_path_reached_by, logged_args=("place", "machine"),
        title="Agents reaching a place",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        refusal_result=_reached_by_refusal, result_type=ReachedByResult, meta=_RESULT_META),
)
