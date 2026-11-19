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
#: A pointing line (`via`) longer than this is cut, ending with "…": the whole line is in the text of the
#: piece that points (its `arrives`), and on a large path the lines alone ran past the result's budget.
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
                          open_area_instructions="\n".join(OPEN_AREA_INSTRUCTIONS))


def _setup(uid, machine: str | None, served: ap.ServedTexts, read=None, trace: ap.Trace | None = None) -> ap.Setup:
    notes: list[str] = []
    paths = _vault_files(uid)
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
# draws. Every path is drawn in a helper process, after the vault changes (`_Drawer`), into a reach map; a write
# answers from the map with a few file stats, or says it can't tell. See docs/architecture/agent-paths.md,
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


def _stat(uid, path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(os.path.join(_vault_root(uid), path))
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


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
        if x.startswith(("note:", "section:")):
            files.append(x.split(":", 1)[1].split("#")[0])
        elif x.startswith("memory:"):
            files.append(ap.MEMORY_NOTE)
    return tuple(dict.fromkeys(files))


def draw_reach_map(uid, served: ap.ServedTexts) -> ReachMap | str:
    """Every path, drawn on the vault as it is now: the reach map, or why it can't be drawn."""
    began = time.monotonic()
    signed: dict[str, tuple | None] = {}
    read = _signed_reader(uid, signed)
    setup_trace = ap.Trace()
    try:
        setup = _setup(uid, None, served, read, setup_trace)
    except (_SetupError, ap.StartingPointsError) as exc:
        return str(exc)
    if setup.snapshot is None:
        return f"no PC has written a snapshot to {ap.SNAPSHOT_FOLDER}/, so what Claude Code loads is unknown"
    read(_rule_files.RULE_FILES_PATH)  # read by `_setup` outside `read`: its signature joins the setup's
    snapshots = [p for p in signed if _is_snapshot(p) and signed[p] is not None]
    keep_made = frozenset(snapshots) if len(snapshots) > 1 else frozenset()
    for p in keep_made:
        signed[p] = (signed[p][0], _content(p, _reader(uid)(p), True))
    setup_inputs = frozenset(signed)
    v, runs = setup.vault, []
    for _label, s, w in ap.run_starts(setup):
        v.trace = ap.Trace()
        b = ap.run(setup, s, w)
        runs.append(_Run({pid: _chain_files(b, pid) for pid in b.order if pid.startswith(("note:", "section:"))},
                         v.trace))
        v.trace = None
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB on Linux
    except (ImportError, OSError):
        peak = 0
    return ReachMap(dict(v.by_rel), signed, setup_inputs, frozenset(ap.registered_hook_scripts(setup)), tuple(runs),
                    round(time.monotonic() - began, 1), peak, setup_trace, keep_made)


def _draw_in_helper(root: str, served: ap.ServedTexts) -> ReachMap | str:
    """Run in the helper process: the vault at `root` read as a single-user server reads it."""
    from src.config import settings
    from src.services import vault as vault_service

    settings.multi_user_mode = False
    settings.vault_path = root
    vault_service.clear_user_vault_cache()
    return draw_reach_map(None, served)


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

    async def _draw(self, uid) -> None:
        while True:
            while (wait := self._due - time.monotonic()) > 0:
                await asyncio.sleep(wait)
            due = self._due
            try:
                served = await _served(uid)
                if DRAW_IN_HELPER:
                    result = await asyncio.get_running_loop().run_in_executor(
                        _helper(), _draw_in_helper, self.root, served)
                else:
                    result = await asyncio.to_thread(draw_reach_map, uid, served)
            except Exception as exc:  # noqa: BLE001 — a failed draw is said on the next write, never raised
                logger.warning("agent paths: drawing the reach map failed", exc_info=True)
                result = f"drawing the agent paths failed ({type(exc).__name__})"
                if isinstance(exc, BrokenProcessPool):
                    _drop_helper()
            if isinstance(result, str):
                self.failure = result
            else:
                self.map, self.failure = result, ""
                logger.info("agent paths: reach map drawn in %.1f s, %d paths, drawing process peak memory %d MiB",
                            result.seconds, len(result.runs), result.peak_kib // 1024)
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
    touched = (any(written.lower() in r.trace.texts or (new and r.trace.new_file_counts(written)) for r in m.runs)
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
    path = p.id.split(":", 1)[1].split("#")[0] if p.id.startswith(("note:", "section:")) else None
    moment = p.moment if p.moment and p.moment != "when met" else None
    if status == "unchanged":
        # The caller holds this piece from an earlier call: what places it on the path, and its hash, are
        # enough; the rest (where, how, the pointing line) came then. Keeps a call with `known` small, so
        # withheld texts always find room on the next one.
        return PathPiece(id=p.id, stage=ap.STAGE_NAMES[p.stage], always=p.always, moment=moment, hash=p.hash,
                         status=status)
    return PathPiece(
        id=p.id, stage=ap.STAGE_NAMES[p.stage], arrives=p.arrives, always=p.always, where=p.where, chars=p.chars,
        hash=p.hash, via=_short(p.via, _VIA_MAX) if p.via else None, moment=moment,
        also_pointed_by=p.also or None, status=status,
        private=True if path and private_folders.is_private(path) else None)


_DETAILS = ("arrives", "where", "chars", "via", "also_pointed_by")


def _short_form(o: PathPiece) -> dict:
    """Strip a piece to what places it on the path and its hash; the details taken, to give back with its
    text."""
    taken = {k: getattr(o, k) for k in _DETAILS}
    for k in _DETAILS:
        setattr(o, k, None)
    return taken


def _with_texts(result, pieces: list[ap.Piece], out: list[PathPiece], notes: list[str],
                known: dict | None = None) -> None:
    """Fill in the texts of new or changed pieces, in path order, while the result stays within the budget.

    Sets `result.notes`. A first call (no `known`) lists every piece: those whose text waits for a later call
    in short form (id, stage, always, moment, hash), their details (how it arrives, where, its pointing line)
    coming with their text, so the whole path shows at once. A call with `known` lists only what it brings:
    the pieces with their text this time, those now marked in parts, and new private ones; the pieces held
    and those still waiting were in the first call, so they are left out and counted (`waiting`), and a removal
    shows in `left_path` (owner, 2026-11-18). Measured on the owner's vault, repeating them took most of each
    call: a backward question's 150 pieces in short form left room for one text or two. Every call makes
    progress: the first text due either arrives or, longer than the room left, is marked to come in parts
    (`read_from`, one call, which carries the piece alone).
    """
    budget = PAGE_CHARS - _NOTES_RESERVE
    listing = known is not None
    order = {id(o): i for i, o in enumerate(out)}
    due = [(p, o) for p, o in zip(pieces, out) if o.status != "unchanged" and not o.private]
    if listing:
        result.pieces = [o for o in out if o.status != "unchanged" and o.private]
        # The chains and the structure came with the first call too, unchanged by a later one (owner,
        # 2026-11-18: 9,000 characters a call on a backward question); a call without known shows them again.
        result.structure = None
        if hasattr(result, "reached_by"):
            result.reached_by = None
        notes.append("The structure (and for a backward question, who reaches the place) came with your first "
                     "call; call without known to see them again.")
    details = {id(o): _short_form(o) for _, o in due}
    used = len(result.model_dump_json())
    given: list[PathPiece] = []
    count = {"in_parts": 0, "waiting": 0}

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
        if used + cost <= budget:
            restore(o)
            o.text, used = p.text, used + cost
            given.append(o)
            if listing:
                result.pieces.append(o)
        elif listing:
            count["waiting"] += 1
        else:
            o.text_withheld, used = True, used + len(',"text_withheld":true')
    if listing:
        result.pieces.sort(key=lambda o: order[id(o)])

    def withheld_note() -> list[str]:
        lines = []
        if listing and count["waiting"]:
            lines.append(f"{count['waiting']} texts are still to come (waiting): their pieces were in your first "
                         "call. Call again with the same arguments, passing in known the hash of every piece whose "
                         "text you now hold, and they come next with their details.")
        n = sum(1 for o in result.pieces or [] if o.text_withheld)
        if n:
            lines.append(f"{n} texts did not fit in this result (text_withheld): those pieces come in short form. "
                         "Call again with the same arguments, passing in known the hash of every piece whose text "
                         "you now hold, and only what's new comes next, with these texts.")
        if count["in_parts"]:
            lines.append(f"{count['in_parts']} texts are too long for the room in this result (text_in_parts): call "
                         "again with the same arguments and read_from={id, offset: 0} for each; each part gives the "
                         "next offset.")
        return lines

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
            result.waiting = count["waiting"] if listing else None
            result.notes = (notes + withheld_note()) or None

    take_back(budget)
    _fit_metadata(result, notes, budget)
    result.waiting = count["waiting"] if listing else None
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
        for name in ("rule_file_mentions", "unplaced_pointers", "pointers_outside_rule_files", "into_areas",
                     "broken_pointers", "skipped_imports", "import_warnings"):
            items = getattr(st, name)
            while items and size() > cap:
                items.pop()
                st.cut = sorted(set((st.cut or []) + [name]))
    if dropped:
        notes.append(f"The last {dropped} pieces are left out: even in short form, the pieces pass the size "
                     "budget. Call again passing in known the hashes of the pieces you hold, and the rest come "
                     "next.")
    if st is not None and st.cut:
        notes.append(f"Some lists of structure are cut short to fit ({', '.join(st.cut)}).")


def _fit_overview(result) -> None:
    """Keep the overview within the budget: the structure lists are cut short from the last typical agent
    back, then those across every area, each cut said in its `cut` and in the notes."""
    cap = PAGE_CHARS - _NOTES_RESERVE
    structures = [t.structure for t in reversed(result.typical or [])] + [result.across_every_area]
    names = ("rule_file_mentions", "unplaced_pointers", "pointers_outside_rule_files", "into_areas",
             "broken_pointers", "skipped_imports", "import_warnings")
    cut = False
    for st in structures:
        for name in names:
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


def _part_of(result, pieces: list[ap.Piece], out: list[PathPiece], read_from: ReadFrom):
    """The call asked for one part of one text: that piece alone, without the rest of the result."""
    hit = next(((p, o) for p, o in zip(pieces, out) if p.id == read_from.id), None)
    if hit is None:
        raise _SetupError(f"read_from.id {read_from.id!r} is not on this path.")
    result.structure = result.left_path = None
    _one_part(result, hit[0], hit[1], read_from.offset)
    return result


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
    return [{**x, "via": _short(x["via"], _VIA_MAX)} if x.get("via") else x for x in items] if items else None


def _grouped_mentions(ms: list[dict] | None) -> list[dict] | None:
    """The links to rule files that are mentions, one entry per rule file with the notes linking to it: a
    path meets sixty of them, and quoting each line ran past the result's budget (the line is in the text of
    the note it comes from)."""
    out: dict[str, list[str]] = {}
    for m in ms or []:
        froms = out.setdefault(m["target"], [])
        if m["from"] not in froms:
            froms.append(m["from"])
    return [{"target": t, "from": f} for t, f in out.items()] or None


def _structure(st: dict) -> PathStructure:
    return PathStructure(
        size=st.get("size"), unplaced_pointers=_clip_vias(st.get("unplaced_pointers")),
        pointers_outside_rule_files=_clip_vias(st.get("pointers_outside_rule_files")),
        into_areas=_clip_vias(st.get("into_areas")), mentions_not_followed=st.get("mentions_not_followed"),
        rule_file_mentions=_grouped_mentions(st.get("rule_file_mentions")),
        broken_pointers=_clip_vias(st.get("broken_pointers")),
        skipped_imports=st.get("skipped_imports") or None, import_warnings=st.get("import_warnings") or None,
        not_followed=st.get("not_followed") or None)


def _short(s: str | None, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _overview_lines(b: ap.PathBuilder, known: dict | None) -> list[str]:
    """One line per piece, each server's tool texts as one, as the prototype's overview prints them."""
    lines, tools = [], {}
    for pid in b.order:
        p = b.pieces[pid]
        if p.stage == ap.TOOLS:
            tools.setdefault(pid.split(":")[1], []).append(p)  # instructions:<server>, tool:<server>:<name>, …
            continue
        status = "" if known is None else (" [unchanged]" if known.get(p.id) == p.hash
                                           else " [changed]" if p.id in known else " [new]")
        when = "" if p.always else f"  when: {_short(p.moment, 90)}"
        lines.append(f"{len(lines) + 1}. {pid}  {p.chars:,} chars  #{p.hash}{status}{when}")
    for server, texts in tools.items():
        combined = ap.digest("".join(p.hash for p in texts))
        kinds = "instructions, guide, tools" if server == ap.SERVER else "instructions, tools"
        lines.append(f"{len(lines) + 1}. tool:{server}:* ({len(texts)} texts: {kinds})  "
                     f"{sum(p.chars for p in texts):,} chars  #{combined}")
    return lines


def _start_of(start: PathStart, points: ap.StartingPoints) -> ap.Start:
    if start.typical is not None:
        if any(v is not None for k, v in start.model_dump().items() if k not in ("typical", "machine")):
            raise _SetupError("start.typical names a typical agent whole: give it alone (start.machine aside).")
        hit = next((t for t in points.typical if t.handwritten == start.typical), None)
        if hit is None:
            raise _SetupError(f"No typical agent {start.typical}: {ap.STARTING_POINTS_PATH} has "
                              f"{', '.join(str(t.handwritten) for t in points.typical)}.")
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


async def agent_path_impl(start: PathStart | None, work: PathWork | None, known: dict | None,
                          read_from: ReadFrom | None = None) -> AgentPathResult:
    uid = current_user_id.get()
    served = await _served(uid)
    machine = start.machine if start else None

    def draw() -> AgentPathResult:
        setup = _setup(uid, machine, served)
        notes = list(setup.notes)
        snap = setup.snapshot
        common = {"machine": snap.machine if snap else None, "snapshot_made": snap.made if snap else None}
        if start is None:
            if read_from is not None:
                raise _SetupError("read_from needs the start (and work) of the call that gave text_in_parts.")
            runs = ap.all_runs(setup)
            typical = [TypicalPath(number=s.handwritten, name=label, pieces=_overview_lines(b, known), structure=_structure(ap.structure_of(b)))
                       for label, s, _w, b in runs[: len(setup.points.typical)]]
            notes.append("Pass start (a typical agent's number, or a surface and folder) for one path with its "
                         "texts.")
            unreached = ap.unreached_rule_files(setup, runs)
            if unreached:
                notes.append(_NO_PATH_NOTE)
            # What every path leaves out by design is said once, across every area, rather than on each path.
            by_design = ap.by_design(setup.vault)
            for t in typical:
                t.structure.not_followed = [x for x in t.structure.not_followed or [] if x != by_design] or None
            across = _structure(ap.across_runs(runs))
            across.not_followed = [by_design]
            result = AgentPathResult(typical=typical, rule_files_no_path_reaches=unreached,
                                     across_every_area=across, notes=notes, **common)
            _fit_overview(result)
            return result
        s = _start_of(start, setup.points)
        w = ap.Work(work.area, work.kind) if work else ap.Work()
        b = ap.run(setup, s, w)
        if s.role and not b.role_picked and not _ROLE_EXTRAS.get(s.role):
            # Nothing on this path offers a choice of role note (on claude.ai, no session-start hook does):
            # said, so a path drawn for a start that isn't a real case isn't taken for its answer (owner).
            notes.append(f"role: {s.role} changed nothing: no line on this path picks a role note.")
        notes += _changed_hook_scripts(setup, b.order)
        pieces = [b.pieces[i] for i in b.order]
        out = [_piece(p, known) for p in pieces]
        result = AgentPathResult(
            start={k: v for k, v in vars(s).items() if v}, work={k: v for k, v in vars(w).items() if v} or None,
            pieces=out, left_path=_left_path(known, b.order),
            structure=_structure(ap.structure_of(b)), notes=notes or None, **common)
        if read_from is not None:
            return _part_of(result, pieces, out, read_from)
        _with_texts(result, pieces, out, notes, known)
        return result

    try:
        return await asyncio.to_thread(draw)
    except (_SetupError, ap.StartingPointsError) as exc:
        return _failed(AgentPathResult, str(exc))


async def agent_path_reached_by_impl(place: str, known: dict | None, machine: str | None,
                                     read_from: ReadFrom | None = None) -> ReachedByResult:
    uid = current_user_id.get()
    served = await _served(uid)

    def draw() -> ReachedByResult:
        setup = _setup(uid, machine, served)
        notes = list(setup.notes)
        r = ap.reached_by(setup, place.strip())
        notes += _changed_hook_scripts(setup, r["pieces"])
        if r["matched_by"].startswith("nothing"):
            notes.append(_NO_PATH_NOTE)
        pieces = list(r["pieces"].values())
        out = [_piece(p, known) for p in pieces]
        snap = setup.snapshot
        result = ReachedByResult(
            place=r["place"], matched_by=r["matched_by"], reached_by=r["reached_by"],
            machine=snap.machine if snap else None, snapshot_made=snap.made if snap else None, pieces=out,
            left_path=_left_path(known, r["pieces"]), structure=_structure(r["structure"]), notes=notes or None)
        if read_from is not None:
            result.reached_by = None
            return _part_of(result, pieces, out, read_from)
        _with_texts(result, pieces, out, notes, known)
        return result

    try:
        return await asyncio.to_thread(draw)
    except (_SetupError, ap.StartingPointsError) as exc:
        return _failed(ReachedByResult, str(exc))


#: Said in a result that reports a place no path reaches: a cold reader took it for `not_followed` (2026-11-19).
_NO_PATH_NOTE = (
    "No path reaches (`rule_files_no_path_reaches`, or `matched_by` \"nothing\") means the path tools followed every "
    "way they know that rules reach agents, and none leads there: either nothing sends an agent there, or something "
    "does by a way they don't follow, which they must then be taught. `not_followed` is different: what the tools "
    "know of and leave out, because they can't read it (such as claude.ai's own system prompt) or by design (such as "
    "a repo's docs); a rule reached only through one of them also shows as reached by no path.")


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
    """What an agent of this setup meets, in order: every rule text on its path, with a hash.

    Without `start`: the five typical agents side by side. `structure` lists facts to judge, not faults. Field
    reference, for maintaining this tool: https://github.com/kelno/obsidian-mcp/blob/main/docs/architecture/agent-paths.md
    """
    return await agent_path_impl(start, work, known, read_from)


async def agent_path_reached_by(
    place: Annotated[str, _A_PLACE],
    known: Annotated[dict[str, str] | None, _A_KNOWN] = None,
    machine: Annotated[str | None, _A_MACHINE] = None,
    read_from: Annotated[ReadFrom | None, _A_READ_FROM] = None,
) -> ReachedByResult:
    """Which agents of this setup meet a rule's place, by which chain, and every piece on their paths.

    `agent_path` backward: use it before adding or changing a rule. Its pieces, `structure`, `known` and
    `read_from` work as in `agent_path`.
    """
    return await agent_path_reached_by_impl(place, known, machine, read_from)


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
