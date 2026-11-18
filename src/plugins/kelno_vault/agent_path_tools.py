"""`agent_path` and `agent_path_reached_by`: the agent path tool, as two tools of the `kelno_vault` plugin.

They draw, from this vault's setup as it is, what each kind of agent meets and in what order (`agent_paths`,
the engine, holds the reading rules and why). This module gathers what the engine reads: the vault through
the caller's own root, the PC side from the snapshots the PCs write into the vault, and this server's own
texts as it serves them (its tool descriptions with every plugin's lines, its instructions, the server's part
of `get_vault_guide`, `open_area`'s instruction lines), which no script on the PC could see.

A first call returns every piece's text, which runs past a hundred thousand characters on most paths: the
result is held to `MAX_READ_RESPONSE_CHARS`, and the texts that don't fit are marked, to come with the next
call once the caller passes the hashes it now holds. See docs/architecture/agent-paths.md.
"""

import asyncio
import json
import os
import posixpath
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from src import plugins
from src.auth.session import current_user_id
from src.config import MAX_NOTE_BYTES, settings
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
    TypicalPath,
)
from src.services import private_folders
from src.services.tool_outcomes import body_refusal as _body_refusal
from src.services.vault import _vault_root, read_bytes

#: Characters kept free of texts for the notes added after them (the withheld line among them).
_NOTES_RESERVE = 500
#: Roles whose start adds more than a role note: the relay is handed its notes by the bridge.
_ROLE_EXTRAS = {"relay": ["relay_notes"]}


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


def _setup(uid, machine: str | None, served: ap.ServedTexts) -> ap.Setup:
    notes: list[str] = []
    paths = _vault_files(uid)
    read = _reader(uid)
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
    vault = ap.Vault(paths, read, rule_files, areas, snapshot.vault if snapshot else None)
    return ap.Setup(vault, points, snapshot, served, notes)


# ── Shaping the result ───────────────────────────────────────────────


def _piece(p: ap.Piece, known: dict | None) -> PathPiece:
    status = None if known is None else ("unchanged" if known.get(p.id) == p.hash
                                         else "changed" if p.id in known else "new")
    path = p.id.split(":", 1)[1].split("#")[0] if p.id.startswith(("note:", "section:")) else None
    return PathPiece(
        id=p.id, stage=ap.STAGE_NAMES[p.stage], arrives=p.arrives, always=p.always, where=p.where, chars=p.chars,
        hash=p.hash, via=p.via, moment=p.moment if p.moment and p.moment != "when met" else None,
        also_pointed_by=p.also or None, status=status,
        private=True if path and private_folders.is_private(path) else None)


def _with_texts(result, pieces: list[ap.Piece], out: list[PathPiece], notes: list[str]) -> None:
    """Fill in the texts of new or changed pieces, in path order, while the result stays within the budget.

    Sets `result.notes`. The texts are added by an estimate of their serialized length, then the whole result
    is measured, and the last texts given are taken back until it fits.
    """
    budget = settings.max_read_response_chars - _NOTES_RESERVE
    used = len(result.model_dump_json())
    room = budget - used  # what a call holding every other text would have for this one
    given: list[PathPiece] = []
    wanted = too_large = 0
    for p, o in zip(pieces, out):
        if o.status == "unchanged" or o.private:
            continue
        cost = len(json.dumps(p.text, ensure_ascii=False)) + len(',"text":')
        if cost > room:
            # No later call could carry it either: saying it comes next would send the caller round forever.
            o.text_too_large, used, too_large = True, used + len(',"text_too_large":true'), too_large + 1
            continue
        wanted += 1
        if used + cost <= budget:
            o.text, used = p.text, used + cost
            given.append(o)
        else:
            o.text_withheld, used = True, used + len(',"text_withheld":true')

    def withheld_note() -> list[str]:
        n = wanted - len(given)
        lines = [f"{n} texts did not fit in this result (text_withheld): call again with the same arguments, "
                 "passing in known the hash of every piece whose text you now hold, and they come next."] if n else []
        if too_large:
            lines.append(f"{too_large} texts are too large for any one result (text_too_large): read them where "
                         "they live (`where`), a vault note with read_note, a file on the PC with your own tools.")
        return lines

    result.notes = (notes + withheld_note()) or None
    while given and len(result.model_dump_json()) > settings.max_read_response_chars:
        last = given.pop()
        last.text, last.text_withheld = None, True
        result.notes = (notes + withheld_note()) or None


def _left_path(known: dict | None, on_path) -> list[str] | None:
    """The pieces the caller holds that are no longer on the path: a removal, which no status could show."""
    gone = [pid for pid in (known or {}) if pid not in on_path]
    return gone or None


def _structure(st: dict) -> PathStructure:
    return PathStructure(
        size=st.get("size"), unplaced_pointers=st.get("unplaced_pointers") or None,
        pointers_outside_rule_files=st.get("pointers_outside_rule_files") or None,
        into_areas=st.get("into_areas") or None, mentions_not_followed=st.get("mentions_not_followed"),
        broken_pointers=st.get("broken_pointers") or None, not_followed=st.get("not_followed") or None)


def _short(s: str | None, n: int) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _overview_lines(b: ap.PathBuilder, known: dict | None) -> list[str]:
    """One line per piece, the connector's tool texts as one, as the prototype's overview prints them."""
    lines, tools = [], []
    for pid in b.order:
        p = b.pieces[pid]
        if p.stage == ap.TOOLS:
            tools.append(p)
            continue
        status = "" if known is None else (" [unchanged]" if known.get(p.id) == p.hash
                                           else " [changed]" if p.id in known else " [new]")
        when = "" if p.always else f"  when: {_short(p.moment, 90)}"
        lines.append(f"{len(lines) + 1}. {pid}  {p.chars:,} chars  #{p.hash}{status}{when}")
    if tools:
        combined = ap.digest("".join(p.hash for p in tools))
        lines.append(f"{len(lines) + 1}. tool:{ap.SERVER}:* ({len(tools)} texts: instructions, guide, tools)  "
                     f"{sum(p.chars for p in tools):,} chars  #{combined}")
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


async def agent_path_impl(start: PathStart | None, work: PathWork | None, known: dict | None) -> AgentPathResult:
    uid = current_user_id.get()
    served = await _served(uid)
    machine = start.machine if start else None

    def draw() -> AgentPathResult:
        setup = _setup(uid, machine, served)
        notes = list(setup.notes)
        hand = ap.handwritten_paths(setup.vault)
        snap = setup.snapshot
        common = {"machine": snap.machine if snap else None, "snapshot_made": snap.made if snap else None}
        if start is None:
            runs = ap.all_runs(setup)
            typical = [TypicalPath(number=s.handwritten, name=label, handwritten=hand.get(s.handwritten),
                                   pieces=_overview_lines(b, known), structure=_structure(ap.structure_of(b)))
                       for label, s, _w, b in runs[: len(setup.points.typical)]]
            notes.append("Pass start (a typical agent's number, or a surface and folder) for one path with its "
                         "texts.")
            return AgentPathResult(typical=typical, rule_files_no_path_reaches=ap.unreached_rule_files(setup, runs),
                                   across_every_area=_structure(ap.across_runs(runs)), notes=notes, **common)
        s = _start_of(start, setup.points)
        w = ap.Work(work.area, work.kind) if work else ap.Work()
        b = ap.run(setup, s, w)
        pieces = [b.pieces[i] for i in b.order]
        out = [_piece(p, known) for p in pieces]
        result = AgentPathResult(
            start={k: v for k, v in vars(s).items() if v}, work={k: v for k, v in vars(w).items() if v} or None,
            handwritten=hand.get(s.handwritten), pieces=out, left_path=_left_path(known, b.order),
            structure=_structure(ap.structure_of(b)), notes=notes or None, **common)
        _with_texts(result, pieces, out, notes)
        return result

    try:
        return await asyncio.to_thread(draw)
    except (_SetupError, ap.StartingPointsError) as exc:
        return _failed(AgentPathResult, str(exc))


async def agent_path_reached_by_impl(place: str, known: dict | None, machine: str | None) -> ReachedByResult:
    uid = current_user_id.get()
    served = await _served(uid)

    def draw() -> ReachedByResult:
        setup = _setup(uid, machine, served)
        notes = list(setup.notes)
        r = ap.reached_by(setup, place.strip())
        pieces = list(r["pieces"].values())
        out = [_piece(p, known) for p in pieces]
        snap = setup.snapshot
        result = ReachedByResult(
            place=r["place"], matched_by=r["matched_by"], reached_by=r["reached_by"],
            machine=snap.machine if snap else None, snapshot_made=snap.made if snap else None, pieces=out,
            left_path=_left_path(known, r["pieces"]), structure=_structure(r["structure"]), notes=notes or None)
        _with_texts(result, pieces, out, notes)
        return result

    try:
        return await asyncio.to_thread(draw)
    except (_SetupError, ap.StartingPointsError) as exc:
        return _failed(ReachedByResult, str(exc))


_A_START = Field(description="Who the agent is (see its fields). Omit it for the overview: the five typical "
                             "agents side by side, one line per piece, then facts over every area.")
_A_WORK = Field(description="What the agent was given to do; optional.")
_A_KNOWN = Field(description="The pieces whose text you already hold, as {piece id: hash}: only new or changed "
                             "pieces come back with their text. Omit it on a first call.")
_A_PLACE = Field(description="Where the rule lives, or would: a vault path (`_/agent/guides/coding-practices.md`), "
                             "a section (`Areas/Voice bridge/Voice bridge.md#For agents`), "
                             "`tool:obsidian-mcp:<tool>` or `hook:SessionStart/<script>`. A place that doesn't "
                             "exist yet is matched by its note, then by its area.")
_A_MACHINE = Field(description="The PC whose snapshot gives what only a PC sees; without it, the only one, or "
                               "the most recent.")


async def agent_path(
    start: Annotated[PathStart | None, _A_START] = None,
    work: Annotated[PathWork | None, _A_WORK] = None,
    known: Annotated[dict[str, str] | None, _A_KNOWN] = None,
) -> AgentPathResult:
    """What an agent of this setup meets, in the order it arrives: every rule text on its path, with a hash.

    "I'm this agent, doing this work: what do I meet?" `start` says who (a typical agent's number, or a
    surface, folder and who started it), `work` the area it works in. Each piece is a note, a note's
    section, a file Claude Code loads on the PC, a hook's message or one of this server's texts as served,
    with how and when it arrives (`stage`, `arrives`, the pointing line `via` and its `moment`; `always:
    false` when it arrives only if that moment comes), its length and a 12-digit `hash`. Pass the hashes
    you hold as `known`: only new or changed pieces come back with their text, and `left_path` lists those
    you hold that are gone. Texts that don't fit in one result are marked `text_withheld` and come with the
    next call; one larger than any result is `text_too_large`, to read where it lives. Then `structure`:
    pointers that don't say when, links into areas, pointers to nothing, what the tool can't follow yet.
    What only a PC sees comes from the snapshot each PC writes at a session's start (`machine`,
    `snapshot_made`). Without `start`: the five typical agents side by side, one line per piece, and the
    rule files no path reaches.
    """
    return await agent_path_impl(start, work, known)


async def agent_path_reached_by(
    place: Annotated[str, _A_PLACE],
    known: Annotated[dict[str, str] | None, _A_KNOWN] = None,
    machine: Annotated[str | None, _A_MACHINE] = None,
) -> ReachedByResult:
    """Which agents of this setup meet a rule's place, by which chain, and every piece on their paths.

    `agent_path` backward: "this rule lives here, or would: who meets it, and what's around it?" Use it
    before adding or changing a rule, to check it against what those agents already read. Returns
    `reached_by` (each kind of agent with the chain of pieces that brings it there; "any area" or "any
    related folder" where every one reaches it the same way), `pieces` (every piece on those paths, as
    `agent_path` gives them, with the same `known`, `left_path` and size limits) and `structure`. The
    paths drawn are the typical agents', a claude.ai chat on each area and a Claude Code worker in each
    folder an area lists. A place that doesn't exist yet is matched by its note, then by its area
    (`matched_by`).
    """
    return await agent_path_reached_by_impl(place, known, machine)


def _path_refusal(message: str) -> AgentPathResult:
    return AgentPathResult(error=message)


def _reached_by_refusal(message: str) -> ReachedByResult:
    return ReachedByResult(error=message)


TOOLS = (
    plugins.PluginTool(
        name="agent_path", fn=agent_path, logged_args=("start", "work"),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        refusal_result=_path_refusal, result_type=AgentPathResult),
    plugins.PluginTool(
        name="agent_path_reached_by", fn=agent_path_reached_by, logged_args=("place", "machine"),
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        refusal_result=_reached_by_refusal, result_type=ReachedByResult),
)
