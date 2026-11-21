"""The structured results of `agent_path` and `agent_path_reached_by`.

A path is a list of pieces in the order they reach the agent, each with a hash, so an agent that already
holds a piece's text (it passes the hashes it holds as `known`) gets back only what is new or changed; then
the structural facts found on the way. Fields come in the order an agent reads them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from src.mcp_server.read_result import _OmitNone
from src.services.tool_outcomes import BodyOutcome


# Who the agent is: a surface, a folder and who started it, or a typical agent by number. (Comments, not
# docstrings, on these models: a docstring is sent with the tool's schema, and the tool texts are kept short.)
class PathStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    typical: int | None = Field(default=None, description=(
        "A typical agent by number, given alone: 1 a claude.ai chat, 2 the voice relay passing on a request, 3 a "
        "Claude Code worker in a repo, 4 a Claude Code session changing the rules, 5 the Coordinator, 6 the "
        "Arborist."))
    surface: Literal["claude-code", "claude-ai"] | None = Field(default=None, description=(
        "claude-ai includes the voice relay."))
    folder: str | None = Field(default=None, description="Claude Code's start folder on the PC; default home.")
    started_by: Literal["owner", "relay", "coordinator"] | None = None
    role: Literal["worker", "coordinator", "arborist", "relay"] | None = Field(default=None, description=(
        "Picks its role note."))
    machine: str | None = Field(default=None, description="The PC whose snapshot to use; default the latest.")


# What the agent was given to do.
class PathWork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area: str | None = Field(default=None, description="Its area: what open_area serves joins the path.")
    kind: str | None = Field(default=None, description="Recorded, not yet used.")


# Where to go on in a text too long for one result.
class ReadFrom(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    offset: int = Field(default=0, ge=0)


# One line pointing to a piece (round 5: `stage`, `always`, `placed`, `after` and `through` gave way to "every
# time" or "when", with every pointing line).
class PieceLine(_OmitNone):
    #: The piece the line sits in.
    piece: str
    #: The line, word for word: whole when its moment doesn't read right without it ("When choosing it"), else the
    #: part holding the moment and the link, about 240 characters, cut with "…". In short form, only for a moment
    #: that doesn't read right without it.
    line: str | None = None
    #: When the line says to read it: its opening (At the start, While, When, Before) or the guide index's "when to
    #: read it"; "unplaced" for a line outside the rule notes that doesn't say to read it. None for a line that says
    #: to read it with no moment of its own (an import, a hook's "read …"): it holds whenever its piece is read.
    when: str | None = None


class PathPiece(_OmitNone):
    #: What it is: `note:<vault path>`, `section:<note path>#<heading>`, `open_area:<main note path>` (the main
    #: note as `open_area` serves it), `file:<PC path>` (outside the vault), `hook:<event>/<script>`, `memory:/<file>`, `tool:obsidian-mcp:<tool>`, `instructions:obsidian-mcp`,
    #: `guide:obsidian-mcp`.
    id: str
    #: On the path whatever the conversation brings: what brings it.
    every_time: str | None = None
    #: Otherwise, the moments that bring it, in the order the agent meets them.
    when: list[str] | None = None
    #: On a backward question: the groups (`reached_by`'s `group`) whose agents have it every time, and those
    #: whose agents have it only when its moment comes.
    every_time_for: list[int] | None = None
    when_for: list[int] | None = None
    #: Where it lives: a path on the PC, a vault path, or this server.
    where: str | None = None
    chars: int | None = None
    #: The first 12 hex digits of the sha256 of its text: the same hash means the same text.
    hash: str
    #: Every line pointing to it, in the order the agent meets them; none for what no line brings (loaded at the
    #: start, a session-start hook, what open_area serves).
    lines: list[PieceLine] | None = None
    #: With `known`: new, changed or unchanged against the hash you passed.
    status: Literal["new", "changed", "unchanged"] | None = None
    #: The piece's text: given when it is new or changed (always, without `known`), within the size budget.
    text: str | None = None
    #: True when the text did not fit in this result: the piece comes in short form; call again, passing as
    #: `known` the hashes of the pieces whose text you received, and it comes next with its details.
    text_withheld: bool | None = None
    #: True when the text is too long for the room in this result: it comes in parts, asked for with
    #: `read_from` (a call carrying this piece alone; often one part is the whole text).
    text_in_parts: bool | None = None
    #: With `read_from`: where this part of the text starts, and where the next one does (none after the last).
    text_offset: int | None = None
    next_offset: int | None = None
    #: True for a note in a private folder: its text is not shown.
    private: bool | None = None


# The faults found on the way, each list only when it holds any (round 5: faults only), and the path's size.
class PathStructure(_OmitNone):
    #: Characters and pieces: on the path every time, arriving only when their moment comes, and tool texts.
    size: dict[str, int] | None = None
    #: Pointers whose line doesn't say when to read the note; those alike but for `from` as one, `from` a list.
    unplaced_pointers: list[dict] | None = None
    #: Rule files reached through a pointer outside every rule file; `from` a list, as above.
    pointers_outside_rule_files: list[dict] | None = None
    #: Links and vault paths to nothing found; `from` a list, as above.
    broken_pointers: list[dict] | None = None
    #: Imports Claude Code doesn't load (`{from, import, target, why}`): one in a project's file resolving outside
    #: the session's folder waits for an approval the desktop app never asks for, so its file is not on the path.
    skipped_imports: list[dict] | None = None
    #: Imports the PC's snapshot warns about (`{from, import, target, warning}`), such as an import line right next
    #: to another one, which failed to load in tests. Their files stay on the path, as the snapshot lists them.
    import_warnings: list[dict] | None = None
    #: What this path leaves out that it should hold (a failed command, a missing snapshot); what the tools leave
    #: out by design is in the tool's description.
    not_followed: list[str] | None = None
    #: The lists above cut short to keep the result within its size budget, if any.
    cut: list[str] | None = None


# One typical agent in the overview: one line per piece, tool texts as one line.
class TypicalPath(_OmitNone):
    number: int | None = None
    name: str
    #: What it reads every time: `<chars> characters in <n> pieces`.
    every_time: str | None = None
    #: `<n>. <id>  <chars> chars  #<hash>`, with `[status]` against `known` and `when: <moments>` for a piece
    #: that arrives only when one of its moments comes (` | ` between them), without their lines: those come with
    #: `agent_path` for that agent.
    pieces: list[str]
    structure: PathStructure


class AgentPathResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    error: str | None = None
    #: The start and work the path was drawn for, as resolved.
    start: dict | None = None
    work: dict | None = None
    #: The PC whose snapshot gave the PC side, and when that side last changed.
    machine: str | None = None
    snapshot_made: str | None = None
    #: Without `known`, every piece on the path; with `known`, only what this call brings (texts, pieces in
    #: parts, new private ones): the pieces you hold and those still waiting are left out.
    pieces: list[PathPiece] | None = None
    #: How many pieces' texts come in later calls: those marked `text_withheld` here on a call without `known`;
    #: with `known`, those listed in an earlier call and left out here.
    waiting: int | None = None
    #: With `known`: the pieces you hold that are no longer on the path.
    left_path: list[str] | None = None
    structure: PathStructure | None = None
    #: Without `start`: the typical agents side by side, then facts over every area.
    typical: list[TypicalPath] | None = None
    #: Rule files no path reaches (the typical agents, a chat on each area, a worker in each related folder).
    rule_files_no_path_reaches: list[str] | None = None
    #: Facts over every run, each pointer once.
    across_every_area: PathStructure | None = None
    notes: list[str] | None = None


class ReachedByResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    error: str | None = None
    place: str | None = None
    #: What was matched: the place itself; its note (a place no path holds: one that doesn't exist yet, or a part of
    #: a note, the note saying whether it holds that section); its area; or nothing, saying whether the place exists.
    matched_by: str | None = None
    #: Each group of agents (`group`, numbered) with the chain that brings it there (`via`) and every piece on
    #: those agents' paths pointing to the place (`pointed_to_by`); "a claude.ai chat working on any area" or "a
    #: Claude Code worker in any related folder" when every one reaches it the same way.
    reached_by: list[dict] | None = None
    machine: str | None = None
    snapshot_made: str | None = None
    #: What those agents have in front of them when the place applies, in arrival order: the pieces of each
    #: chain, the place included, every piece pointing to the place, and the start pieces every agent of a group
    #: meets (loaded at the start, session-start hook, read at the start), each with its moments and text. For a
    #: group reached through several areas, its first agent's chain stands for the others. With `known`, only
    #: what this call brings, as in `agent_path`.
    pieces: list[PathPiece] | None = None
    #: Every other piece on those paths, one line each: `<id>  <chars> chars  #<hash>`, `moment: <moments>` for one
    #: waiting on a moment, `(groups …)` when not every group has it; each server's tool texts as one line; then a
    #: line per piece with other versions of its text. Any of them can be read with `read_from: {id, offset: 0}`.
    #: Left out of a call with `known`, as the structure is.
    other_pieces: list[str] | None = None
    #: How many pieces' texts come in later calls, as in `agent_path`.
    waiting: int | None = None
    #: With `known`: the pieces you hold that are on none of those paths any more.
    left_path: list[str] | None = None
    structure: PathStructure | None = None
    notes: list[str] | None = None
