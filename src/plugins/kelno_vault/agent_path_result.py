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


class PathStart(BaseModel):
    """Who the agent is: a surface, a folder and who started it, or one of the typical agents by number."""

    model_config = ConfigDict(extra="forbid")

    typical: int | None = Field(default=None, description=(
        "One of the typical agents, by its number in the vault's starting points (1 a claude.ai chat, 2 the "
        "voice relay, 3 a Claude Code worker in a repo, 4 a session changing the rules, 5 the Coordinator). "
        "Give it without the other start fields (machine aside)."))
    surface: Literal["claude-code", "claude-ai"] | None = Field(default=None, description=(
        "Where the agent runs: claude-code (a Claude Code session on a PC) or claude-ai (a claude.ai chat, "
        "the voice relay included)."))
    folder: str | None = Field(default=None, description=(
        "Claude Code: the folder the session starts in, as an absolute path on the PC (D:\\work\\repo); "
        "without it, the home folder."))
    started_by: Literal["owner", "relay", "coordinator"] | None = Field(default=None, description=(
        "Who started the session."))
    role: Literal["worker", "coordinator", "relay"] | None = Field(default=None, description=(
        "The role it plays in the owner's Coordinator setup; picks its role note, and relay adds the relay's "
        "notes."))
    machine: str | None = Field(default=None, description=(
        "The PC whose snapshot gives what only a PC sees; without it, the only one, or the most recent."))


class PathWork(BaseModel):
    """What the agent was given to do."""

    model_config = ConfigDict(extra="forbid")

    area: str | None = Field(default=None, description=(
        "The area the work is in (its name, slug or main note's name): what open_area serves on it joins the "
        "path. A Claude Code session in a folder an area lists gets that area without it."))
    kind: str | None = Field(default=None, description="The kind of task (coding, filing, rules, …); recorded, "
                                                        "not yet matched against the pointers' moments.")


class PathPiece(_OmitNone):
    #: What it is: `note:<vault path>`, `section:<note path>#<heading>`, `file:<PC path>` (outside the vault),
    #: `hook:<event>/<script>`, `memory:/<file>`, `tool:obsidian-mcp:<tool>`, `instructions:obsidian-mcp`,
    #: `guide:obsidian-mcp`.
    id: str
    #: When it arrives: loaded at the start, session-start hook, read at the start, at the first bridge
    #: result, when the work's area is opened, when its moment comes, when the connector's tools load.
    stage: str
    #: How it arrives, in words: what loads it or points to it.
    arrives: str
    #: False when it arrives only if its moment comes (`moment`).
    always: bool
    #: Where it lives: a path on the PC, a vault path, or this server.
    where: str
    chars: int
    #: The first 12 hex digits of the sha256 of its text: the same hash means the same text.
    hash: str
    #: The line that points to it, word for word.
    via: str | None = None
    #: When that line says to read it; "unplaced" when it doesn't say.
    moment: str | None = None
    #: Other pieces on the path pointing to it.
    also_pointed_by: list[str] | None = None
    #: With `known`: new, changed or unchanged against the hash you passed.
    status: Literal["new", "changed", "unchanged"] | None = None
    #: The piece's text: given when it is new or changed (always, without `known`), within the size budget.
    text: str | None = None
    #: True when the text did not fit in this result: call again, passing as `known` the hashes of the
    #: pieces whose text you received, and the rest comes next.
    text_withheld: bool | None = None
    #: True when the text is larger than any one result can carry: read it where it lives (`where`).
    text_too_large: bool | None = None
    #: True for a note in a private folder: its text is not shown.
    private: bool | None = None


class PathStructure(_OmitNone):
    """Facts about the structure itself, found on the way; judging them is left to the caller."""

    #: Characters and pieces: always on the path, arriving only when their moment comes, and tool texts.
    size: dict[str, int] | None = None
    #: Pointers whose line doesn't say when to read the note.
    unplaced_pointers: list[dict] | None = None
    #: Rule files reached through a pointer outside every rule file.
    pointers_outside_rule_files: list[dict] | None = None
    #: Pointers into an area's notes from the trunk, or from one area into another.
    into_areas: list[dict] | None = None
    #: Links to notes that aren't rule files, with no reading verb: counted, not followed.
    mentions_not_followed: int | None = None
    #: Pointers to nothing found.
    broken_pointers: list[dict] | None = None
    #: What the path leaves out because it cannot be followed yet.
    not_followed: list[str] | None = None


class TypicalPath(_OmitNone):
    """One typical agent in the overview: one line per piece, tool texts as one line."""

    number: int | None = None
    name: str
    #: That agent's path as written by hand in the vault, to compare with the drawn one.
    handwritten: str | None = None
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
    #: A typical agent's path as written by hand in the vault, to compare with the drawn one.
    handwritten: str | None = None
    pieces: list[PathPiece] | None = None
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
    #: Every piece on those paths.
    pieces: list[PathPiece] | None = None
    #: With `known`: the pieces you hold that are on none of those paths any more.
    left_path: list[str] | None = None
    structure: PathStructure | None = None
    notes: list[str] | None = None
