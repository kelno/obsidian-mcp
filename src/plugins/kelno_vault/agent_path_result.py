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

    typical: int | None = Field(default=None, description="A typical agent by number (1-5); give it alone.")
    surface: Literal["claude-code", "claude-ai"] | None = Field(default=None, description=(
        "claude-ai includes the voice relay."))
    folder: str | None = Field(default=None, description="Claude Code's start folder on the PC; default home.")
    started_by: Literal["owner", "relay", "coordinator"] | None = None
    role: Literal["worker", "coordinator", "relay"] | None = Field(default=None, description=(
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


class PathPiece(_OmitNone):
    #: What it is: `note:<vault path>`, `section:<note path>#<heading>`, `file:<PC path>` (outside the vault),
    #: `hook:<event>/<script>`, `memory:/<file>`, `tool:obsidian-mcp:<tool>`, `instructions:obsidian-mcp`,
    #: `guide:obsidian-mcp`.
    id: str
    #: When it arrives: loaded at the start, session-start hook, read at the start, at the first bridge
    #: result, when the work's area is opened, when its moment comes, when the connector's tools load.
    stage: str
    #: How it arrives, in words: what loads it or points to it. Left out, with `where`, `chars`, `via` and
    #: `also_pointed_by`, for a piece you hold (`unchanged`) and one whose text comes later (`text_withheld`):
    #: the short form, its details coming with its text.
    arrives: str | None = None
    #: False when it arrives only if its moment comes (`moment`).
    always: bool
    #: Where it lives: a path on the PC, a vault path, or this server.
    where: str | None = None
    chars: int | None = None
    #: The first 12 hex digits of the sha256 of its text: the same hash means the same text.
    hash: str
    #: The line that points to it, word for word up to 240 characters, then cut with "…".
    via: str | None = None
    #: When that line says to read it: its opening (At the start, While, When, Before) or the guide index's
    #: "when to read it"; "unplaced" when a line outside the rule notes doesn't say.
    moment: str | None = None
    #: Other pieces on the path pointing to it.
    also_pointed_by: list[str] | None = None
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


# Facts about the structure itself, found on the way; judging them is left to the caller.
class PathStructure(_OmitNone):
    #: Characters and pieces: always on the path, arriving only when their moment comes, and tool texts.
    size: dict[str, int] | None = None
    #: Pointers whose line doesn't say when to read the note.
    unplaced_pointers: list[dict] | None = None
    #: Rule files reached through a pointer outside every rule file.
    pointers_outside_rule_files: list[dict] | None = None
    #: Pointers into an area's local pieces from a shared one, or from one area's local pieces into another's.
    into_areas: list[dict] | None = None
    #: Links that are mentions, not pointers: counted, not followed.
    mentions_not_followed: int | None = None
    #: Of those, the links to rule files: between rule notes, a link in none of the pointer shapes is a
    #: mention, not a pointer, so the rule file it names is not put on the path. One entry per rule file
    #: (`{target, from: [...]}`), the notes linking to it.
    rule_file_mentions: list[dict] | None = None
    #: Links and vault paths to nothing found.
    broken_pointers: list[dict] | None = None
    #: Imports Claude Code doesn't load (`{from, import, target, why}`): one in a project's file resolving outside
    #: the session's folder waits for an approval the desktop app never asks for, so its file is not on the path.
    skipped_imports: list[dict] | None = None
    #: Imports the PC's snapshot warns about (`{from, import, target, warning}`), such as an import line right next
    #: to another one, which failed to load in tests. Their files stay on the path, as the snapshot lists them.
    import_warnings: list[dict] | None = None
    #: What the path leaves out: what cannot be followed yet, and what isn't followed by design.
    not_followed: list[str] | None = None
    #: The lists above cut short to keep the result within its size budget, if any.
    cut: list[str] | None = None


# One typical agent in the overview: one line per piece, tool texts as one line.
class TypicalPath(_OmitNone):
    number: int | None = None
    name: str
    #: `<n>. <id>  <chars> chars  #<hash>`, with `[status]` against `known` and `when: <moment>` for a piece
    #: that arrives only when its moment comes.
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
    #: With `known`: how many pieces' texts come in later calls (listed in your first call, not here).
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
    #: What was matched: the place itself, its note (a place that doesn't exist yet), or its area.
    matched_by: str | None = None
    #: Each kind of agent with the chain that brings it there; "any area" or "any related folder" when every
    #: one reaches it the same way.
    reached_by: list[dict] | None = None
    machine: str | None = None
    snapshot_made: str | None = None
    #: Every piece on those paths; with `known`, only what this call brings, as in `agent_path`.
    pieces: list[PathPiece] | None = None
    #: With `known`: how many pieces' texts come in later calls.
    waiting: int | None = None
    #: With `known`: the pieces you hold that are on none of those paths any more.
    left_path: list[str] | None = None
    structure: PathStructure | None = None
    notes: list[str] | None = None
