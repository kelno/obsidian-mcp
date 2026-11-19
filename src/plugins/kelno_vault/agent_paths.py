"""Agent paths: everything that tells one kind of agent how to act in this vault's setup, in the order it arrives.

The engine behind `agent_path` and `agent_path_reached_by` (`agent_path_tools`). Two questions over the same
pieces:

- forward (`agent_path`): "I'm this agent, doing this work: what do I meet?"
- backward (`agent_path_reached_by`): "this rule lives here, or would: who meets it, and what's around it?"

The tool exists so agents can keep the vault's rules maintainable: it draws, from the setup as it is, which
paths an agent actually meets, rather than a hand-kept list that drifts (the vault note `Areas/Agent
personnel/Agent paths/Agent paths - path tool.md` holds the design and the owner's decisions). It was first
built as a script on the owner's PC; that prototype is the reference for the reading rules below, and this
module keeps its pieces, ids and hashes so its output can be compared with the script's.

Where each part of a path comes from:

- the vault itself: the notes, the rule-file definition (`rule_files`), the list of areas (`areas_index`),
  Claude's memory through its mirror note, the hand-written paths in `WHERE_NOTE`;
- `SNAPSHOT_FOLDER/<machine>.json`, written by a session-start hook on each PC (the vault's
  `_/claude-code-hooks/agent-paths-snapshot.py`), for what only the PC sees: the files Claude Code loads
  at the start in each folder, with the text of those outside the vault, the session-start hooks' messages,
  the hooks registered at every event and what they say at events other than a session's start, the voice
  relay's notes;
- `STARTING_POINTS_PATH`, the only hand-written input: what each surface loads first (a fact of the product
  no file states) and the five typical agents;
- the server's own texts as served (tool descriptions, the server instructions, `get_vault_guide`'s own
  part, `open_area`'s instruction lines), handed in by the tool wrapper.

What cannot be followed is reported under `not_followed` on each path, never guessed, with what isn't followed
by design, as `WHERE_NOTE` lists it (`BY_DESIGN_OPENING`): in a repo, nothing beyond what Claude Code loads there.

Standard library only, and no import from the server: the tool wrapper does the reading, so this module can
also be loaded on its own (the comparison with the prototype does).
"""

from __future__ import annotations

import functools
import hashlib
import heapq
import json
import posixpath
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field

#: The folder the PC-side snapshots are written to, one `<machine>.json` per PC.
SNAPSHOT_FOLDER = "_/agent-paths"
#: The snapshot shape this module reads (the hook's `FORMAT`).
#: Format 2 adds `hook_scripts`; nothing else changed shape.
SNAPSHOT_FORMATS = (1, 2)
#: The hand-written starting points: what each surface loads first, and the typical agents.
STARTING_POINTS_PATH = "_/agent-paths/starting-points.toml"
MEMORY_NOTE = "_/agent/Memory reference.md"
#: The sections of `MEMORY_NOTE` that mirror Claude's memory: its lines, then its profile (`load_memory`).
MEMORY_SECTIONS = ["Lines", "Profile"]
AREAS_NOTE = "Areas/Areas.md"
CONVENTIONS_NOTE = "_/agent/Vault conventions.md"
WHERE_NOTE = "_/agent/Where agent instructions go.md"
CODING_GUIDE = "_/agent/guides/coding-practices.md"
ROLES_DIR = "_/agent/roles/"
#: A main note as `open_area` serves it, without its For agents and Tasks sections: not the note's whole text, so not
#: its id (a caller holding the whole note's hash would take one for the other). This, `note:` and `section:` name a
#: vault file.
OPEN_AREA_ID = "open_area:"
VAULT_IDS = ("note:", "section:", OPEN_AREA_ID)
#: The section of `WHERE_NOTE` holding the hand-written paths of the typical agents, numbered.
HANDWRITTEN_HEADING = "Checking a rule against agent paths"
#: The paragraph of that section listing what the tools leave out by design opens with these words [P277].
BY_DESIGN_OPENING = "The tools don't follow, by design:"
#: The server these tools run in, in tool piece ids (`tool:<server>:<tool>`).
SERVER = "obsidian-mcp"
#: The voice bridge, in its pieces' ids, its texts read from the PC's snapshot (`bridge`).
BRIDGE_SERVER = "claude-voice"

# Arrival stages, in the order things reach an agent.
LOADED, HOOK, START_READ, BRIDGE, WORK, LATER, TOOLS = range(7)
STAGE_NAMES = {
    LOADED: "loaded at the start",
    HOOK: "session-start hook",
    START_READ: "read at the start",
    BRIDGE: "at the first bridge result",
    WORK: "when the work's area is opened",
    LATER: "when its moment comes",
    TOOLS: "when the connector's tools load",
}

# A pointer to a rule note says when to read it in one of a closed list of shapes, set out in "How to write an
# instruction" in WHERE_NOTE [P259]: its bullet or sentence opens with one of these words (anything before the first
# word that isn't a letter is ignored, so a bold label in front doesn't pass), or it takes the guide index's shape,
# `[[note]]: when to read it`. The moment is that opening, up to the first : ; , full stop or table cell.
# The same shapes hold for a repo's `CLAUDE.md` or `AGENTS.md` pointing into the vault, where the index's link is
# a Markdown link or a backticked path (`[x.md](D:/vault/x.md): when to read it`).
POINTER_LEAD = re.compile(r"^((?:at the start|while|when|before)\b(?:[^:;,.|]|\|(?!\s))*)", re.I)
NOT_LETTERS = re.compile(r"^[\W\d_]+")
INDEX_SHAPE = re.compile(r"^(\[\[[^\]]+\]\]|\[[^\]\n]*\]\([^)\s]+\)|`[^`\n]+`):\s*(.+)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
WIKILINK = re.compile(r"\[\[([^\]|#]+?)\\?(#[^\]|]*?)?\\?(?:\|[^\]]*)?\]\]")
BACKTICK_MD = re.compile(r"`([^`\n]+?\.md)`")
ABS_MD = re.compile(r"[A-Za-z]:[\\/][^\n\"'`]*?\.md")
#: A Markdown link to a `.md` file (`[text](docs/x.md)`, an anchor allowed), as a repo's files may point into the vault.
MD_LINK = re.compile(r"\[[^\]\n]*\]\(<?([^)\s<>]+?\.md)>?(?:#[^)\s]*)?\)")
#: A placeholder segment in a backticked vault path (`Reference/Servers/<name>/<name>.md`): the path names a kind of
#: note, every note matching it.
PLACEHOLDER = re.compile(r"<[^<>/`]+>")
#: A rule section named in backticks in the pointing sentence ("read the `## For agents` section of …").
NAMED_SECTION = re.compile(r"`##\s+([^`]+?)\s*`")
#: A hook's message saying the lines that follow are a note's rule section ("from the `## For agents` section of …").
COPIED_SECTION = re.compile(r"\bfrom the `##\s+([^`]+?)\s*` section of\b", re.I)
IMPORT = re.compile(r"(?:^|\s)@((?:[A-Za-z]:)?[~./\\\w][^\s]*\.md)")


class StartingPointsError(Exception):
    """The starting points file is missing or does not say what a path needs."""


def digest(text: str) -> str:
    """A piece's hash: the first 12 hex digits of its text's sha256, as the PC's snapshot hook computes it."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def as_text(data: bytes) -> str:
    """A file's text as Python's universal newlines read it on the PC, so hashes match the snapshot's."""
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


@functools.lru_cache(maxsize=8192)
def section(text: str, heading: str) -> str | None:
    """A level-2 section's text, heading included, up to the next heading of level 1 or 2 (Rule files' definition).

    Kept per (text, heading): every path reads the same notes' sections, the folder notes' above all, and this
    took most of a path's time."""
    lines, inside, fence = [], False, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and re.match(r"^#{1,2} ", line):
            if inside:
                break
            inside = line.strip() == f"## {heading}"
        if inside:
            lines.append(line)
    return "\n".join(lines).strip() if lines else None


def without_sections(text: str, headings: list[str]) -> str:
    out, skip = [], False
    for line in text.splitlines():
        if re.match(r"^#{1,2} ", line):
            skip = line.strip() in {f"## {h}" for h in headings}
        if not skip:
            out.append(line)
    return "\n".join(out).strip()


def frontmatter_flag(text: str, key: str) -> bool:
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    return bool(m and re.search(rf"^{key}:\s*true\s*$", m.group(1), re.M | re.I))


#: An area's tag lives in its main note's `area_tag` frontmatter, `area/<slug>` (the Tasks plugin's areas).
AREA_PREFIX = "area/"
#: What names an area by its tag, as `open_area` takes it: the slug, `area/<slug>` or `#area/<slug>` (lower case).
AREA_TAG_KEY = re.compile(r"#?(?:area/)?([a-z0-9]+(?:-[a-z0-9]+)*)")


def area_tag(text: str) -> str | None:
    """A main note's `area_tag` frontmatter value, quotes stripped; None without one."""
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    hit = m and re.search(r"^area_tag:\s*['\"]?([^'\"\s#]+)['\"]?\s*$", m.group(1), re.M)
    return hit.group(1) if hit else None


# --- PC paths ------------------------------------------------------------------------------------------------
#
# The snapshot names files by the PC's paths (`D:/work/repo/CLAUDE.md`), which this server cannot open: they
# are handled as strings, compared without case as Windows does.

def pc_norm(path: str) -> str:
    p = path.strip().replace("\\", "/")
    drive = re.match(r"^[A-Za-z]:", p)
    head, rest = (p[:2], p[2:]) if drive else ("", p)
    rest = posixpath.normpath(rest) if rest else rest
    return (head + rest).rstrip("/") if len(head + rest) > 3 else head + rest


def pc_absolute(path: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:)?/", path.replace("\\", "/")))


def pc_parents(path: str) -> list[str]:
    """`path`, then each folder above it, up to the drive's root."""
    out, cur = [], pc_norm(path)
    while True:
        out.append(cur)
        parent = pc_norm(posixpath.dirname(cur)) if "/" in cur else cur
        if re.fullmatch(r"[A-Za-z]:", parent):
            parent += "/"
        if parent == cur or not parent:
            return out
        cur = parent


# --- the PC side ---------------------------------------------------------------------------------------------

@dataclass
class Snapshot:
    """One PC's `SNAPSHOT_FOLDER/<machine>.json`, as its session-start hook wrote it."""

    machine: str
    made: str | None
    vault: str
    home: str
    folders: dict[str, dict]
    files: dict[str, dict]
    hooks: list[dict]
    relay_notes: list[str]
    relay_config: str
    #: Format 2: each registered hook script's id (`note:<vault path>` or `file:<path>`) and the digest of its
    #: file as the snapshot read it (None when it couldn't), so a script changed since can be told.
    hook_scripts: dict[str, str | None] = field(default_factory=dict)
    #: Optional (format 2): what each hook says at events other than a session's start, by `hook:<event>/<script>`:
    #: `{event, matcher, messages: [{when, text, hash}]}`, `messages` null and `why` when the script can't say them.
    #: None when the snapshot predates the key.
    hook_messages: dict[str, dict] | None = None
    #: Optional: the voice bridge's own texts, from its `claude-voice texts` command: `{command, instructions,
    #: guide, tools, notes}`, or `{command, error}` when the command failed; None when the bridge isn't on that PC.
    bridge: dict | None = None
    #: Whether the snapshot has the `bridge` key at all (a snapshot from before it says nothing either way).
    bridge_known: bool = False
    #: Each folder's name as the snapshot spells it, by its key in `folders` (lower case).
    folder_names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "Snapshot":
        data = json.loads(text)
        if data.get("format") not in SNAPSHOT_FORMATS:
            raise ValueError(f"format {data.get('format')!r}, where this server reads formats "
                             f"{', '.join(map(str, SNAPSHOT_FORMATS))}")
        hook_messages = data.get("hook_messages")
        return cls(data["machine"], data.get("made"), pc_norm(data["vault"]), pc_norm(data["home"]),
                   {pc_norm(k).lower(): v for k, v in data.get("folders", {}).items()},
                   {k.lower(): v for k, v in data.get("files", {}).items()}, data.get("hooks", []),
                   data.get("relay_notes", []), data.get("relay_config", ""), data.get("hook_scripts") or {},
                   hook_messages if isinstance(hook_messages, dict) else None,
                   data["bridge"] if isinstance(data.get("bridge"), dict) else None, "bridge" in data,
                   {pc_norm(k).lower(): pc_norm(k) for k in data.get("folders", {})})

    def folder(self, folder: str) -> tuple[str, dict] | None:
        """The snapshot's entry for `folder`, or for the deepest folder above it that it holds."""
        for cand in pc_parents(folder):
            if cand.lower() in self.folders:
                return cand, self.folders[cand.lower()]
        return None

    def file_text(self, path: str) -> str | None:
        entry = self.files.get(f"file:{pc_norm(path)}".lower())
        return entry.get("text") if entry else None

    def holds(self, folder: str) -> bool:
        return pc_norm(folder).lower() in self.folders


#: How a CLAUDE.md file below the start folder arrives (`load_folder_claude_md`), followed by its folder.
NESTED_LOAD = "loaded by Claude Code when it reads a file in"


BACKSLASH = chr(92)


def worktree_of(folder: str) -> str | None:
    """The repo a folder is a worktree of (`<repo>/.claude/worktrees/<name>`, or a folder in one), else None."""
    m = re.match(r"^(.+?)/\.claude/worktrees/[^/]+(?:/.*)?$", pc_norm(folder))
    return m.group(1) if m else None


def folder_load(entry: dict) -> bool:
    """Whether a snapshot entry is one of the folder's own CLAUDE.md files (or rules), not the user's."""
    how = entry.get("arrives", "")
    return how.startswith("loaded at the start by Claude Code (") and not how.startswith(
        ("loaded at the start by Claude Code (user CLAUDE.md)", "loaded at the start by Claude Code (user rules)"))


# --- the vault -----------------------------------------------------------------------------------------------

@dataclass
class Trace:
    """What one path read and looked up in the vault, lower case: a changed file it read, or a new file it looked
    for, may change that path; any other change to the vault cannot."""

    #: Notes whose text it read, existing or not.
    texts: set[str] = field(default_factory=set)
    #: Vault paths it looked for (`resolve`, a loaded file's vault path).
    paths: set[str] = field(default_factory=set)
    #: Note names it looked for (a bare-name link).
    stems: set[str] = field(default_factory=set)
    #: Path patterns it matched against every note (`Vault.matching`), as their regex source.
    patterns: set[str] = field(default_factory=set)
    #: Notes it read only for one section (`Vault.section_of`): `(path, heading)` to that section's digest, None
    #: when the note had none. A change elsewhere in such a note cannot change this path.
    sections: dict[tuple[str, str], str | None] = field(default_factory=dict)

    def reads(self, path: str) -> bool:
        """Whether this path read the note at `path`, whole or for a section."""
        low = path.lower()
        return low in self.texts or any(p == low for p, _h in self.sections)

    def new_file_counts(self, path: str) -> bool:
        """Whether a note created at `path` could change this path: it looked for that path or name, or matched it."""
        low = path.lower()
        stem = posixpath.basename(low).removesuffix(".md")
        return (self.reads(path) or low in self.paths or stem in self.stems
                or any(re.fullmatch(p, path, re.I) for p in self.patterns))

    def changed_by(self, path: str, text: str | None) -> bool:
        """Whether `path`'s new text (None: gone) can change this path: it read the note whole, or a section it read
        is not what it was."""
        low = path.lower()
        if low in self.texts:
            return True
        return any(p == low and (digest(body) if (body := section(text or "", h)) else None) != was
                   for (p, h), was in self.sections.items())


class Vault:
    """The vault's files, its rule-file definition and its area table, read once per call.

    `paths` is every visible file, vault-relative; `read` returns a file's text or None. `rule_files` is the
    plugin's definition (`rule_files.RuleFiles`), `areas` the rows of the list of areas (`areas_index`).
    `pc_vault` is where the vault lives on the PC the snapshot comes from, to read the PC's absolute paths.
    """

    def __init__(self, paths: list[str], read: Callable[[str], str | None], rule_files, areas: list,
                 pc_vault: str | None, trace: "Trace | None" = None):
        self._read, self._texts = read, {}
        #: Set by a caller drawing one path at a time (`Trace`), from the start for what the setup looks up.
        self.trace: Trace | None = trace
        self.set_paths(paths)
        self.rule_files = rule_files
        self.rule_headings = list(rule_files.sections)
        self.pc_vault = pc_vault
        self.areas = [{"name": a.name, "folders": [f.replace("\\", "/") for f in a.folders],
                       "main": self.resolve(a.main_note) if a.main_note else None} for a in areas]

    def set_paths(self, paths: list[str]) -> None:
        """The vault's files, as they are now: the lookups by path and by name follow, and the pattern matches are
        found again (a kept drawing calls it when files came or went)."""
        self._matches: dict[re.Pattern, list[str]] = {}
        self.by_rel: dict[str, str] = {}
        self.by_stem: dict[str, list[str]] = {}
        for r in sorted(paths):
            self.by_rel[r.lower()] = r
            name = posixpath.basename(r)
            if name.endswith(".md"):
                self.by_stem.setdefault(name[:-3].lower(), []).append(r)

    def forget(self, r: str) -> None:
        """Drop the text kept for `r`: it is read again when next asked for (a kept drawing, after it changed)."""
        low = r.lower()
        for key in [k for k in self._texts if k.lower() == low]:
            del self._texts[key]

    def rel(self, pc_path: str) -> str | None:
        """The vault path of a PC path, or None outside the vault (or with no snapshot to say where it is)."""
        if not self.pc_vault:
            return None
        p, prefix = pc_norm(pc_path), self.pc_vault.rstrip("/")
        if p.lower() == prefix.lower():
            return ""
        return p[len(prefix) + 1:] if p.lower().startswith(prefix.lower() + "/") else None

    def where(self, r: str) -> str:
        return f"{self.pc_vault}/{r}" if self.pc_vault else r

    def is_rule_file(self, r: str) -> bool:
        return self.rule_files.is_rule_file(r)

    def resolve(self, target: str, by_name: bool = True) -> str | None:
        """A link target (vault path with or without .md, absolute PC path, or bare note name) to its vault path."""
        t = target.strip().replace("\\", "/")
        if re.match(r"^[A-Za-z]:/", t):
            r = self.rel(t)
            return self.lookup(r) if r else None
        for cand in (t, t + ".md"):
            if self.lookup(cand):
                return self.by_rel[cand.lower()]
        if not by_name:  # a path in backticks is a path, not a note name
            return None
        stem = posixpath.basename(t).removesuffix(".md").lower()
        if self.trace is not None:
            self.trace.stems.add(stem)
        hits = self.by_stem.get(stem, [])
        return hits[0] if len(hits) == 1 else None

    def lookup(self, path: str) -> str | None:
        """The vault path spelled as the vault holds it, or None when there is no such file."""
        if self.trace is not None:
            self.trace.paths.add(path.lower())
        return self.by_rel.get(path.lower())

    def matching(self, pattern: re.Pattern) -> list[str]:
        """The `.md` notes whose vault path matches `pattern` whole, sorted; kept for the call, as each path follows
        the same pattern pointers (a scan of every path took most of a path's time)."""
        if self.trace is not None:
            self.trace.patterns.add(pattern.pattern)
        if pattern not in self._matches:
            self._matches[pattern] = sorted(r for r in self.by_rel.values() if r.endswith(".md") and pattern.fullmatch(r))
        return self._matches[pattern]

    def text(self, r: str) -> str | None:
        if self.trace is not None:
            self.trace.texts.add(r.lower())
        if r not in self._texts:
            self._texts[r] = self._read(r)
        return self._texts[r]

    def section_of(self, r: str, heading: str) -> str | None:
        """One rule section of note `r` (`section`), the note read for that section only: the trace keeps the
        section, not the note's whole text."""
        if r not in self._texts:
            self._texts[r] = self._read(r)
        body = section(self._texts[r] or "", heading)
        if self.trace is not None:
            self.trace.sections[(r.lower(), heading)] = digest(body) if body else None
        return body

    def area(self, name_or_folder: str) -> dict | None:
        """The area a name, folder, slug or tag names: its name in the list of areas, its main note's name, a related
        folder (or one below it), or what `open_area` takes besides, its `area_tag` (`chronic-symptoms`,
        `#area/chronic-symptoms`, `area/chronic-symptoms`)."""
        key = name_or_folder.replace("\\", "/").rstrip("/").lower()
        for a in self.areas:
            main_stem = posixpath.basename(a["main"])[:-3].lower() if a["main"] else ""
            if key in (a["name"].lower(), main_stem) or a["name"].lower().startswith(key + " ("):
                return a
        for a in self.areas:
            for f in a["folders"]:
                f = f.rstrip("/").lower()
                if key == f or key.startswith(f + "/"):
                    return a
        tag = AREA_TAG_KEY.fullmatch(key)
        if tag:  # only a slug or a tag reads the main notes, so a folder's path doesn't tie it to every one
            wanted = AREA_PREFIX + tag.group(1)
            for a in self.areas:
                if a["main"] and area_tag(self.text(a["main"]) or "") == wanted:
                    return a
        return None

    def area_of_note(self, r: str) -> dict | None:
        """The area whose folder holds this note (Areas/<Name>/...)."""
        for a in self.areas:
            if a["main"] and r.lower().startswith(posixpath.dirname(a["main"]).lower() + "/"):
                return a
        return None


# --- the starting points -------------------------------------------------------------------------------------

@dataclass
class Start:
    surface: str
    folder: str | None = None
    started_by: str | None = None
    role: str | None = None
    extras: list[str] = field(default_factory=list)
    name: str | None = None
    handwritten: int | None = None


@dataclass
class Work:
    area: str | None = None
    kind: str | None = None


@dataclass
class StartingPoints:
    surfaces: dict
    typical: list[Start]

    @classmethod
    def parse(cls, text: str) -> "StartingPoints":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise StartingPointsError(f"{STARTING_POINTS_PATH} is not valid TOML: {e}") from e
        typical = [Start(t["surface"], t.get("folder"), t.get("started_by"), t.get("role"), t.get("extras", []),
                         t.get("name"), t.get("handwritten")) for t in data.get("typical", [])]
        if not data.get("surface"):
            raise StartingPointsError(f"{STARTING_POINTS_PATH} names no surface")
        return cls(data["surface"], typical)


@dataclass
class ServedTexts:
    """The server's own texts, as it serves them now (the tool wrapper reads them)."""

    #: (tool name, description followed by each parameter's description), in the order tools/list gives them.
    tools: list[tuple[str, str]]
    #: The instructions the server hands a client at connection.
    instructions: str
    #: `get_vault_guide`'s own part: everything but the vault's conventions note, which is a piece of its own.
    guide: str
    #: `open_area`'s instruction lines (`OPEN_AREA_INSTRUCTIONS`).
    open_area_instructions: str
    #: The vault note `get_vault_guide` returns after its own part (the server's `VAULT_GUIDE_NOTE`).
    guide_note: str = CONVENTIONS_NOTE


# --- pieces --------------------------------------------------------------------------------------------------

@dataclass
class Piece:
    id: str
    stage: int
    arrives: str
    where: str
    text: str
    # The arrival that places it (its earliest certain one): the line pointing to it, word for word, when that
    # line says to read it ("unplaced" when it doesn't say), and the piece holding the line.
    via: str | None = None
    moment: str | None = None
    parent: str | None = None
    always: bool = True  # on the path whatever the conversation brings, once the start and work are given
    rule: bool = True  # whether its own pointers are followed
    rules_only: bool = False  # follow only its pointers to rule files (text the agent reads that isn't a rule file)
    #: Every way it arrives, in the order the agent meets them: (moment, pointing piece, line), the piece and
    #: line None for what no line brings (what loads at the start, a hook's message).
    arrivals: list[tuple[str | None, str | None, str | None]] = field(default_factory=list)
    #: The moments that lead to it (`PathBuilder.moments_of`), set once the path is drawn.
    moments: list[dict] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return digest(self.text)

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class Setup:
    """Everything a path is drawn from, read once per call."""

    vault: Vault
    points: StartingPoints
    snapshot: Snapshot | None
    served: ServedTexts
    #: Notes about the inputs themselves (several snapshots, one unreadable), for the result.
    notes: list[str] = field(default_factory=list)


class PathBuilder:
    """Follows one agent's path: start, work, then every pointer in order of arrival."""

    def __init__(self, setup: Setup, start: Start, work: Work):
        self.setup, self.v, self.snap = setup, setup.vault, setup.snapshot
        self.surfaces, self.start, self.work = setup.points.surfaces, start, work
        self.pieces: dict[str, Piece] = {}
        self.order: list[str] = []
        self.queue: list = []
        self.seq = 0
        self.mentions: list[dict] = []  # links to non-rule notes with no reading verb: not followed
        self.broken: list[dict] = []
        #: Imports Claude Code doesn't load, as the snapshot lists them (`skipped_imports`).
        self.skipped: list[dict] = []
        #: Imports the snapshot warns about (an import line next to another one, Q134), followed all the same.
        self.import_warnings: list[dict] = []
        self.not_followed: list[str] = []
        #: The files Claude Code loads when it reads a file in a folder below the start folder, and their imports.
        self.nested: set[str] = set()
        self.role_picked = False  # whether a line naming several role notes was narrowed to the start's role
        #: Whether a line named several role notes and the start gave no role to pick one by.
        self.role_unpicked = False
        #: The role notes of the line being followed, when the start gives no role: vault path (lower) to its role.
        self.role_choice: dict[str, str] = {}
        #: Whether the piece being followed was placed again (`drain`): what it found the first time isn't counted twice.
        self.again = False

    # --- queueing -----------------------------------------------------------------------------------------
    def push(self, piece: Piece) -> None:
        """Earliest arrival wins; at the same stage, a pointer that says when beats one that doesn't."""
        self.seq += 1
        placed = 1 if piece.moment == "unplaced" else 0
        heapq.heappush(self.queue, (piece.stage, 0 if piece.always else 1, placed, self.seq, piece))

    def drain(self) -> None:
        """Take in what was pushed. Every arrival of a piece is kept (`arrivals`), none picked as its route; the
        earliest certain one places it: one on the path whatever the conversation brings beats one waiting on a
        moment, then the earlier stage. A piece placed again that way is followed again, so what it points to is
        placed the same way (an area's `## For agents`, met first behind "Before filing a note into a
        folder", then at the area's opening)."""
        while self.queue:
            *_, p = heapq.heappop(self.queue)
            arrival = (p.moment, p.parent, p.via)
            kept = self.pieces.get(p.id)
            self.again = kept is not None
            if kept is None:
                p.arrivals = [arrival]
                self.pieces[p.id] = p
                self.order.append(p.id)
            else:
                if arrival not in kept.arrivals:
                    kept.arrivals.append(arrival)
                if not ((p.always and not kept.always) or (p.always == kept.always and p.stage < kept.stage)):
                    continue
                kept.stage, kept.always, kept.arrives = p.stage, p.always, p.arrives
                kept.via, kept.moment, kept.parent = p.via, p.moment, p.parent
                self.order.remove(p.id)  # shown where it is now placed
                self.order.append(p.id)
                p = kept
            if p.rule:
                self.follow(p)
        self.again = False

    # --- following pointers ------------------------------------------------------------------------------
    def follow(self, p: Piece) -> None:
        if (p.stage == LOADED or p.id in self.nested) and p.where.lower().endswith(".md"):
            self.follow_imports(p)  # Claude Code expands imports only in what it loads itself
        text = p.text
        if p.id == f"note:{MEMORY_NOTE}":
            # Its Lines and Profile mirror Claude's memory, which only claude.ai loads (`load_memory`): instructions to
            # a claude.ai chat, not to whoever reads the mirror.
            text = without_sections(text, MEMORY_SECTIONS)
        for line in unfenced(text):
            self.role_choice = {}
            if p.id.startswith("file:"):  # outside the vault (a repo's CLAUDE.md): its links into the vault
                for target, pos in file_targets(line):
                    self.file_pointer(p, line, *sentence_at(line, pos), target)
                continue
            code = [(m.start(), m.end()) for m in INLINE_CODE.finditer(line)]
            targets = []
            for m in WIKILINK.finditer(line):
                if any(a <= m.start() < b for a, b in code):
                    continue  # a link written as code (an example) isn't one
                targets.append((m.group(1), (m.group(2) or "").lstrip("#") or None, m.start(), True))
            for m in BACKTICK_MD.finditer(line):
                targets.append((m.group(1), None, m.start(), False))
            for m in ABS_MD.finditer(line):
                if not any(m.group(0) in t[0] for t in targets):
                    targets.append((m.group(0), None, m.start(), False))
            targets = self.pick_role(line, targets)
            for target, heading, pos, by_name in targets:
                self.pointer(p, line, *sentence_at(line, pos), target, heading, by_name)

    def file_pointer(self, p: Piece, line: str, sentence: str, at: int, target: str) -> None:
        """A link in a file outside the vault (a repo's `CLAUDE.md`), by a backticked path or a Markdown link,
        resolved against the pointing file's folder, then the session folder's root: a path into the vault is a
        vault pointer. Any other is left, by design: in a repo, the tools stop at what Claude Code loads [P277]."""
        t = target.strip().replace("\\", "/")
        if t.startswith(("~", "@")) or PLACEHOLDER.search(t) or "*" in t or (":" in t and not re.fullmatch(r"[A-Za-z]:/[^:]*", t)):
            return  # a home-folder path, an import, a kind of file or a command (`git show main:CLAUDE.md`)
        if pc_absolute(t):
            candidates = [pc_norm(t)]
        else:
            found = self.snap.folder(self.start.folder or self.snap.home) if self.snap else None
            bases = [posixpath.dirname(pc_norm(p.where))] + ([found[0]] if found else [])
            candidates = list(dict.fromkeys(pc_norm(posixpath.join(b, t)) for b in bases))
        for c in candidates:
            if self.v.rel(c):
                self.pointer(p, line, sentence, at, c, None, False)
                return

    def pick_role(self, line: str, targets: list) -> list:
        """A line naming several role notes lets the session pick by its role (the session-start hook's line does):
        keep only this agent's role note when the start says which role it plays."""
        roles = [t for t in targets if (self.v.resolve(t[0]) or "").lower().startswith(ROLES_DIR.lower())]
        self.role_choice = {}
        if len(roles) >= 2 and not self.start.role:
            # The start doesn't say which role it plays: each role note waits on its role, none is read whatever
            # the conversation brings (`pointer`).
            self.role_choice = {self.v.resolve(t[0]).lower(): posixpath.basename(self.v.resolve(t[0]))[:-3]
                                for t in roles}
            self.role_unpicked = True
        if len(roles) < 2 or not self.start.role:
            return targets
        mine = [t for t in roles
                if posixpath.basename(self.v.resolve(t[0]))[:-3].lower() == self.start.role.lower()]
        if mine:
            self.role_picked = True  # connector only: lets the tool say when a role changed nothing
        return [t for t in targets if t not in roles or t in mine] if mine else targets

    def follow_imports(self, p: Piece) -> None:
        base = posixpath.dirname(pc_norm(p.where))
        skipped, warned = self.skipped_imports(), self.warned_imports()
        for m in IMPORT.finditer(p.text):
            raw = m.group(1)
            entry = skipped.get((p.id.lower(), f"@{raw}"))
            if entry is not None:  # Claude Code holds it back: a structural fact, not a piece
                if entry not in self.skipped:
                    self.skipped.append(entry)
                continue
            if raw.startswith("~"):
                path = (self.snap.home if self.snap else "~") + raw[1:]
            elif pc_absolute(raw):
                path = raw
            else:
                path = posixpath.join(base, raw.replace("\\", "/"))
            piece = self.file_piece(pc_norm(path), LOADED if p.stage == LOADED else p.stage, f"imported by {p.id}",
                                    parent=p.id, via=f"@{raw}", always=p.always)
            if p.id in self.nested:
                self.nested.add(piece.id)
            warning = warned.get((p.id.lower(), piece.id.lower()))
            if warning:  # on the path, as the snapshot lists it, but the piece itself says it may not load
                piece.arrives += f" (may not load: {warning})"
                found = {"from": p.id, "import": f"@{raw}", "target": piece.id, "warning": warning}
                if found not in self.import_warnings:
                    self.import_warnings.append(found)
            self.push(piece)

    def skipped_imports(self) -> dict[tuple[str, str], dict]:
        """The imports the snapshot says Claude Code skips in this start's folder, by (importing piece, `@` text):
        in a project's file, one resolving outside the session's folder waits for an approval the desktop app never
        asks for (the user's own `~/.claude/CLAUDE.md` is exempt)."""
        found = self.user_folder()
        out = {}
        for e in (found[1].get("skipped_imports") or [] if found else []):
            if e.get("in") and e.get("import"):
                out[(e["in"].lower(), e["import"])] = {"from": e["in"], "import": e["import"], "target": e.get("id"),
                                                      "why": e.get("why")}
        return out

    def warned_imports(self) -> dict[tuple[str, str], str]:
        """The snapshot's `warning` on an imported file, by (importing piece, imported piece): set when the import
        line sits next to another import, which made both fail in some tests (Q134). The file stays on the path, as
        the snapshot lists it loaded; its `arrives` and the structure carry the warning."""
        found = self.user_folder()
        out = {}
        for e in (found[1].get("loads", []) if found else []):
            how = e.get("arrives", "")
            if e.get("warning") and how.startswith("imported by "):
                out[(how.removeprefix("imported by ").lower(), e["id"].lower())] = e["warning"]
        return out

    def pointer(self, p: Piece, line: str, sentence: str, at: int, target: str, heading: str | None,
                by_name: bool) -> None:
        if target.startswith("~") or (not by_name and "/" not in target.replace("\\", "/")):
            return  # a home-folder path (~/.claude/CLAUDE.md) or a bare file name in backticks (a repo's `CLAUDE.md`)
        r = self.v.resolve(target, by_name)
        if p.rules_only and (r is None or not self.v.is_rule_file(r)):
            return
        via = line.strip()
        shaped = pointer_moment(via, sentence, target)
        if r is None and not by_name and (pattern := note_pattern(target)) is not None:
            self.pattern_pointer(p, via, sentence, at, target, pattern, shaped)
            return
        if r is None:
            # Placeholders (`Areas/<Name>/<Name>.md`, `YYYY-MM-DD.md`), memory's own files (`/preferences.md`), bare
            # repo file names (`AGENTS.md`) and Syncthing's versions of a note (`<name>~YYYYMMDD-HHMMSS.md`, outside
            # the vault) aren't vault notes; anything else that resolves to nothing is reported.
            looks_like_note = by_name or ("/" in target and not re.search(
                r"[<>@]|YYYY|^/|^[A-Za-z]+/[A-Za-z]+\.md$|~\d{8}-\d{6}\.md$", target))
            if looks_like_note:
                self.mention({"from": p.id, "target": target, "via": via}, self.broken)
            return
        copied = COPIED_SECTION.search(sentence) if p.id.startswith("hook:") and heading is None else None
        if copied and copied.group(1) in self.v.rule_headings:
            # A hook's message holding a rule section's lines ("from the `## For agents` section of <note>:"): the
            # agent has them when the message comes, whatever points to that section later.
            body = self.v.section_of(r, copied.group(1))
            if body:
                kept = all(x.strip() in p.text for x in body.splitlines()[1:] if x.strip())
                self.push(Piece(f"section:{r}#{copied.group(1)}", max(p.stage, START_READ),
                                f"copied into {p.id}'s message" + ("" if kept else " (the copy differs from the "
                                                                               "section now)"),
                                self.v.where(r), body, via, "when met", p.id, p.always))
                return
        if r.lower() == (self.piece_rel(p) or "").lower() and heading is None:
            return
        moment = shaped or ("when met" if reads_it(sentence, at) else "unplaced")
        reads = reads_it(sentence, at) or bool(re.match(r"^\s*[-*]\s+\[\[", line))  # a verb, or the guide index
        stage, always = self.placed(p, moment)
        role = self.role_choice.get(r.lower())
        if role and moment != "unplaced":  # one of several role notes, and the start gives no role (`pick_role`)
            moment, stage, always = f"if its role is {role}", LATER, False
        arrives = f"pointed to by {p.id}"
        if self.v.is_rule_file(r):
            if moment == "unplaced" or (shaped is None and self.bound(p)):
                # Between rule notes, a link in none of the pointer shapes is a mention [P250, P259]; elsewhere (a
                # hook's message, a main note's text) one that says neither when nor "read". Listed, not followed.
                self.mention({"from": p.id, "target": r, "via": via, "rule_file": True})
                return
            self.push(self.note_piece(r, stage, arrives, p.id, via, moment, always))
            return
        if heading and heading in self.v.rule_headings and (moment == "unplaced" or (shaped is None and self.bound(p))):
            # A link to a rule section (`[[Areas/X/X#For agents]]`) is read as one to a rule note.
            self.mention({"from": p.id, "target": f"{r}#{heading}", "via": via, "rule_file": True})
            return
        text = self.v.text(r) or ""
        rule_sections = [h for h in self.v.rule_headings if section(text, h)] if reads else []
        if heading and heading in self.v.rule_headings:
            rule_sections = [heading]
        if reads:
            if heading and not rule_sections:
                txt = section(text, heading)
                if txt:
                    self.push(Piece(f"section:{r}#{heading}", stage, arrives, self.v.where(r), txt, via, moment,
                                    p.id, always, rule=False))
                    return
            self.push(Piece(f"note:{r}", stage, arrives, self.v.where(r), text, via, moment, p.id, always,
                            rule=False))
        else:
            self.mention({"from": p.id, "target": r, "via": via})
        for h in rule_sections:
            self.push(Piece(f"section:{r}#{h}", stage, arrives, self.v.where(r), section(text, h), via, moment,
                            p.id, always))

    @staticmethod
    def placed(p: Piece, moment: str) -> tuple[int, bool]:
        """A pointer's stage, and whether its target is on the path whatever the conversation brings."""
        # "at the start" and a standing condition ("While the setup is on trial") hold from the moment the pointer is met.
        always = p.always and (moment == "when met" or moment.lower().startswith(("at the start", "while")))
        stage = max(p.stage, START_READ) if always else LATER
        if p.stage >= BRIDGE and always:
            stage = p.stage
        return stage, always

    def pattern_pointer(self, p: Piece, via: str, sentence: str, at: int, target: str, pattern: re.Pattern,
                        shaped: str | None) -> None:
        """A backticked path naming a kind of note (`Reference/Servers/<name>/<name>.md`): every vault note matching
        it is reached, with the line's moment, as if the line pointed to each [P261, P262]. When the sentence names a
        rule section (the `## For agents` section of its folder note), each match's section is the piece, and only
        notes that have one count. Otherwise a match that is a rule file is the piece, and any other match, when the
        line says to read it, is a note read for its rule sections. A line that brings nothing is one mention."""
        reads = reads_it(sentence, at)
        moment = shaped or ("when met" if reads else "unplaced")
        stage, always = self.placed(p, moment)
        arrives = f"pointed to by {p.id}, matching `{target}`"
        named = NAMED_SECTION.search(sentence)
        heading = named.group(1) if named and named.group(1) in self.v.rule_headings else None
        # A rule note or section is reached only by a pointer in the convention's shapes [P259], as a link to one is.
        to_rules = moment != "unplaced" and not (shaped is None and self.bound(p))
        own = (self.piece_rel(p) or "").lower()
        brought = False
        for r in self.v.matching(pattern):
            if r.lower() == own:
                continue
            if heading:
                body = self.v.section_of(r, heading)
                if body and to_rules:
                    self.push(Piece(f"section:{r}#{heading}", stage, arrives, self.v.where(r), body, via, moment,
                                    p.id, always))
                    brought = True
            elif self.v.is_rule_file(r):
                if to_rules:
                    self.push(self.note_piece(r, stage, arrives, p.id, via, moment, always))
                    brought = True
            elif reads:
                text = self.v.text(r) or ""
                self.push(Piece(f"note:{r}", stage, arrives, self.v.where(r), text, via, moment, p.id, always,
                                rule=False))
                for h in self.v.rule_headings:
                    body = section(text, h)
                    if body:
                        self.push(Piece(f"section:{r}#{h}", stage, arrives, self.v.where(r), body, via, moment,
                                        p.id, always))
                brought = True
        if not brought:
            self.mention({"from": p.id, "target": target, "via": via})

    def mention(self, entry: dict, into: list | None = None) -> None:
        """A mention (or, `into` broken, a pointer to nothing): each link counts, but a piece placed again and
        followed again doesn't count its links twice."""
        into = self.mentions if into is None else into
        if not (self.again and entry in into):
            into.append(entry)

    @staticmethod
    def bound(p: Piece) -> bool:
        """Whether the pointer convention binds this piece's links: a rule note or section, or memory's lines as
        their mirror note states them. A hook's message, a tool's text or a main note's text isn't written under it."""
        return p.id.startswith(("note:", "section:", "memory:")) and not p.rules_only

    def piece_rel(self, p: Piece) -> str | None:
        return p.id.split(":", 1)[1].split("#")[0] if p.id.startswith(VAULT_IDS) else None

    def note_piece(self, r, stage, arrives, parent, via, moment, always) -> Piece:
        return Piece(f"note:{r}", stage, arrives, self.v.where(r), self.v.text(r) or "", via, moment, parent, always)

    def file_piece(self, path: str, stage, arrives, parent=None, via=None, always=True, moment=None) -> Piece:
        """A file Claude Code loads on the PC: a vault note read from the vault, any other file from the snapshot."""
        r = self.v.rel(path)
        if r is not None:
            pid, text = f"note:{r}", self.v.text(self.v.lookup(r) or r)
        else:
            pid, text = f"file:{path}", self.snap.file_text(path) if self.snap else None
        if text is None:
            self.broken.append({"from": parent, "target": path, "via": via})
            text = ""
        return Piece(pid, stage, arrives, path, text, via, moment or ("when met" if via else None), parent, always)

    # --- the stages ----------------------------------------------------------------------------------------
    def build(self) -> "PathBuilder":
        surface = self.surfaces.get(self.start.surface)
        if surface is None:
            raise StartingPointsError(f"unknown surface {self.start.surface!r}: {STARTING_POINTS_PATH} has "
                                      f"{', '.join(self.surfaces)}")
        for load in surface.get("loads", []):
            loader = getattr(self, f"load_{load}", None)
            if loader is None:
                raise StartingPointsError(f"{STARTING_POINTS_PATH}: the surface {self.start.surface!r} loads "
                                          f"{load!r}, which this server does not know")
            loader()
        self.load_bridge()
        for extra in self.start.extras:
            loader = getattr(self, f"load_{extra}", None)
            if loader is None:
                raise StartingPointsError(f"unknown extra {extra!r}")
            loader()
        self.drain()
        self.load_work()
        if self.start.surface == "claude-code":
            self.load_hook_messages()
        self.drain()
        for server in surface.get("tools", []):
            self.load_tools(server)
        self.drain()
        self.note_not_followed()
        done: dict[str, list[dict]] = {}
        for pid in self.order:
            self.pieces[pid].moments = self.moments_of(pid, done, frozenset())
        # The order shown is the stages' (stable within a stage: the start's pieces, then the area's, then what the
        # hooks say at other events, then the tools).
        self.order.sort(key=lambda i: self.pieces[i].stage)
        seen = set()
        for pid in self.order:
            note = "note:" + pid.removeprefix("section:").split("#")[0]
            if pid.startswith("section:") and note in seen:  # listed on its own as a rule section, already read
                self.pieces[pid].arrives += f" (its text is in {note}, read whole earlier on this path)"
            seen.add(pid)
        return self

    def moments_of(self, pid: str, done: dict, visiting: frozenset) -> list[dict]:
        """Every moment that leads to a piece, in the order the agent meets them, each with the piece whose line
        says it, that line, and the pieces between them (`through`) (owner, 2026-11-19: "Yes with option 1").

        A pointer that holds from when it is met ("when met", "At the start", "While…"), in a piece that itself
        waits on a moment, leads to its target only when that piece is read: the moments it gives are that
        piece's own, through it (test bench, 2026-11-19: the Relay note's "At the start" was shown alone). Any other
        pointer's moment is its own. An arrival no line brings gives its moment when it has one (a hook's message,
        a nested CLAUDE.md), nothing otherwise (what loads at the start, what open_area serves)."""
        if pid in done:
            return done[pid]
        out: list[dict] = []
        for moment, src, line in self.pieces[pid].arrivals:
            if src is None or src not in self.pieces:
                if moment and moment != "when met":
                    out.append({"moment": moment, "piece": src, "line": line, "through": []})
                continue
            holds = moment == "when met" or (moment or "").lower().startswith(("at the start", "while"))
            if holds and not self.pieces[src].always and src not in visiting and src != pid:
                inherited = self.moments_of(src, done, visiting | {pid})
                if inherited:
                    out += [{**e, "through": [*e["through"], src]} for e in inherited]
                    continue
            out.append({"moment": moment, "piece": src, "line": line, "through": []})
        unique = []
        for e in out:
            if e not in unique:
                unique.append(e)
        if not visiting:  # one cut short by a loop is not kept
            done[pid] = unique
        return unique

    def pc_folder(self) -> tuple[str, dict] | None:
        """The snapshot's entry for this start's folder (the home folder when none is given)."""
        if self.snap is None:
            self.not_followed.append("what Claude Code loads at the start and its session-start hooks: no PC "
                                     f"has written a snapshot to {SNAPSHOT_FOLDER}/ yet")
            return None
        folder = self.start.folder or self.snap.home
        found = self.snap.folder(folder)
        repo = worktree_of(folder)
        if repo and found and found[0].lower() == repo.lower():
            # Claude Code loads a worktree's own CLAUDE.md files, not those of the folders above it (Q134; the PC
            # snapshot's rule): the repo's own, as its main checkout has them.
            self.not_followed.append(f"the CLAUDE.md files of the worktree {folder}: taken as {repo}'s main checkout "
                                     "has them, without those of the folders above it, which Claude Code doesn't "
                                     "load in a worktree (Q134)")
            return found
        if found is None:
            # What loads in every folder (the user CLAUDE.md, its imports, the user rules) is the home folder's; the
            # folder's own CLAUDE.md files, and what the session-start hooks say there, can't be known.
            found = self.snap.folder(self.snap.home)
            self.not_followed.append(f"the CLAUDE.md files of {folder}: {self.snap.machine}'s snapshot holds neither "
                                     "that folder nor one above it, so only what Claude Code loads in every folder "
                                     "is on the path" + (f", as it loads in {found[0]}" if found else ""))
        elif found[0].lower() != pc_norm(folder).lower():
            self.not_followed.append(f"CLAUDE.md files below {found[0]} in {folder}: "
                                     f"{self.snap.machine}'s snapshot holds only {found[0]}, so its are used")
        return found

    def user_folder(self) -> tuple[str, dict] | None:
        """The snapshot's entry to read what loads in every folder from (the user CLAUDE.md, its imports, the user
        rules): this start's folder, or the home folder when the snapshot holds neither it nor one above it."""
        if self.snap is None:
            return None
        return self.snap.folder(self.start.folder or self.snap.home) or self.snap.folder(self.snap.home)

    def load_user_claude_md(self) -> None:
        found = self.pc_folder()
        if found is None:
            return
        for entry in found[1].get("loads", []):
            if entry.get("arrives", "").startswith("loaded at the start by Claude Code (user CLAUDE.md)"):
                self.load_entry(entry)

    def load_user_rules(self) -> None:
        """The files of `~/.claude/rules/`, which Claude Code loads at the start, after the user CLAUDE.md."""
        found = self.user_folder()
        for entry in (found[1].get("loads", []) if found else []):
            if entry.get("arrives", "").startswith("loaded at the start by Claude Code (user rules)"):
                self.load_entry(entry)

    def load_folder_claude_md(self) -> None:
        if not self.start.folder or self.snap is None:
            return
        found = self.snap.folder(self.start.folder)
        repo = worktree_of(self.start.folder)
        loaded = set()
        for entry in (found[1].get("loads", []) if found else []):
            how = entry.get("arrives", "").lower()
            mine = f"loaded at the start by claude code ({repo.lower()}" if repo else ""
            if repo and not (how.startswith(mine) and how[len(mine):len(mine) + 1] in (")", " ", "/")):
                continue  # in a worktree, the folders above it load nothing (`pc_folder`)
            if folder_load(entry):
                self.load_entry(entry)
                loaded.add(entry["id"].lower())
        # A folder below the start folder that the snapshot holds (a repo inside the related folder): Claude Code
        # loads its own CLAUDE.md files when it reads a file there, not at the start.
        base = pc_norm(self.start.folder).lower()
        for key in sorted(k for k in self.snap.folders if k.startswith(base.rstrip("/") + "/")):
            sub = self.snap.folder_names.get(key, key)
            for entry in self.snap.folders[key].get("loads", []):
                if folder_load(entry) and entry["id"].lower() not in loaded and entry.get("exists") is not False:
                    loaded.add(entry["id"].lower())
                    piece = self.file_piece(self.entry_path(entry), LATER, f"{NESTED_LOAD} {sub}",
                                            moment=f"when it reads a file in {sub}", always=False)
                    self.nested.add(piece.id)  # its imports are expanded too
                    self.push(piece)

    def load_entry(self, entry: dict) -> None:
        """One file the snapshot lists as loaded at the start. A file that isn't there loads nothing."""
        if entry.get("exists") is False:
            return
        self.push(self.file_piece(self.entry_path(entry), LOADED, entry["arrives"]))

    def entry_path(self, entry: dict) -> str:
        pid = entry["id"]
        if pid.startswith("note:"):
            return f"{self.snap.vault}/{pid[5:]}"
        return pc_norm(pid.removeprefix("file:"))

    def load_session_start_hooks(self) -> None:
        if self.snap is None:
            return
        folder = pc_norm(self.start.folder or self.snap.home)
        source = self.hook_source(folder)
        scripts = {posixpath.basename(a.replace("\\", "/")): a for h in self.snap.hooks for a in h.get("args", [])
                   if str(a).endswith(".py")}
        for hook in (self.snap.folders[source.lower()].get("session_start", []) if source else []):
            if not hook.get("text"):
                continue  # a hook that prints nothing puts nothing in front of the agent
            script = hook["id"].rsplit("/", 1)[-1]
            matcher = hook.get("matcher") or ""
            text, how = hook["text"], f"SessionStart hook (matcher {matcher or 'any'}), run on a sample startup"
            if source.lower() != folder.lower():
                text = self.hook_text_here(hook["id"], text, source, folder)
                how += f" in {source}, with this folder's path in place of that one (the snapshot doesn't hold {folder})"
            self.push(Piece(hook["id"], HOOK, how, scripts.get(script, script), text))

    def hook_source(self, folder: str) -> str | None:
        """The folder whose sampled session-start messages stand for this one's: itself when the snapshot holds it.
        Otherwise (an unlisted folder, a worktree) the hooks still run, being the user's: a held folder of the same
        kind stands for it (`hook_text_here`): the vault for a folder in it, else the deepest held folder above it
        outside the vault and the home folder, else the first such (test bench, 2026-11-19: a start in an unlisted
        folder got no hook)."""
        snap = self.snap
        if snap.holds(folder):
            return snap.folder_names.get(folder.lower(), folder)
        if self.v.rel(folder) is not None:
            return snap.folder_names.get(snap.vault.lower()) if snap.holds(snap.vault) else None
        if folder.lower() == snap.home.lower():
            return None
        mine = {snap.vault.lower(), snap.home.lower()}
        others = [k for k in snap.folders if k not in mine and self.v.rel(k) is None]
        above = [k for k in others if folder.lower().startswith(k + "/")]
        pick = max(above, key=len) if above else min(others) if others else None
        return snap.folder_names.get(pick, pick) if pick else None

    def hook_text_here(self, hid: str, text: str, source: str, folder: str) -> str:
        """A hook's message in a folder the snapshot doesn't hold, from its message in `source`: what it says in the
        home folder (what it says everywhere: the role, the machine's rules), then what it adds in `source`, with that
        folder's path replaced by this one's there only (the machine's rules may name `source` too). The area-start
        hook's message differs between folders outside the vault by the folder's path alone."""
        home = self.snap.folders.get(self.snap.home.lower(), {})
        base = next((h.get("text") or "" for h in home.get("session_start", []) if h.get("id") == hid), "")
        start, rest = (base, text[len(base):]) if base and text.startswith(base) else ("", text)
        for a, b in ((source, folder), (source.replace("/", BACKSLASH), folder.replace("/", BACKSLASH))):
            rest = rest.replace(a, b)
        return start + rest

    def load_hook_messages(self) -> None:
        """What the PC's hooks say at events other than a session's start (the snapshot's `hook_messages`), each
        message a piece waiting on its moment, `hook:<event>/<script>#<n>`. Not a rule file: its links aren't
        followed. A script that can't say its messages is listed under `not_followed`."""
        if self.snap is None or self.snap.hook_messages is None:
            return
        scripts = {}
        for h in self.snap.hooks:
            for a in h.get("args", []):
                if str(a).endswith(".py"):
                    scripts.setdefault((h.get("event", ""), posixpath.basename(str(a).replace("\\", "/"))), str(a))
        for key, entry in self.snap.hook_messages.items():
            if not isinstance(entry, dict):
                continue
            event = entry.get("event") or key.removeprefix("hook:").split("/", 1)[0]
            script = key.rsplit("/", 1)[-1]
            messages = entry.get("messages")
            if messages is None:
                self.not_followed.append(f"what {key} says: {entry.get('why') or 'the snapshot could not say it'}")
                continue
            how = f"{event} hook (matcher {entry.get('matcher') or 'any'})"
            for n, m in enumerate(messages, 1):
                if not isinstance(m, dict) or not m.get("text"):
                    continue
                self.push(Piece(f"{key}#{n}", LATER, f"{how}: {m.get('when') or 'when its event comes'}",
                                scripts.get((event, script), script), m["text"], None, m.get("when") or "unplaced",
                                always=False, rule=False))

    def relay(self) -> bool:
        """The voice relay: a claude.ai chat handed the relay's notes."""
        return self.start.surface == "claude-ai" and "relay_notes" in self.start.extras

    def on_bridge(self) -> bool:
        """Whether this agent has the voice bridge connected: every claude.ai chat (the bridge is one of the account's
        connectors, and memory's lines tell a chat how to use it), the Coordinator, and a Claude Code session the
        bridge starts (for the relay or the Coordinator)."""
        return self.start.surface == "claude-ai" or (self.start.surface == "claude-code" and (
            self.start.started_by in ("relay", "coordinator") or (self.start.role or "").lower() == "coordinator"))

    def load_bridge(self) -> None:
        """The voice bridge's own texts, as the PC's snapshot has them from `claude-voice texts` (`bridge`).

        The relay gets the server instructions and tool texts when the bridge's tools load and the guide with its
        first result from the bridge; a Claude Code session on the bridge gets the instructions and tools when they
        load (deferred) and the guide when it first calls the bridge, and the bridge's notes under the messages it
        relays to it, each at its `when`. None of them is a rule file: their links aren't followed."""
        if not self.on_bridge() or self.snap is None or not self.snap.bridge_known:
            return
        b, machine = self.snap.bridge, self.snap.machine
        if b is None:
            self.not_followed.append(f"the voice bridge's texts: {machine}'s snapshot says the bridge isn't on that PC")
            return
        if b.get("error"):
            self.not_followed.append(f"the voice bridge's texts: `{b.get('command')}` failed on {machine}: {b['error']}")
            return
        where = f"{BRIDGE_SERVER}: `{b.get('command') or 'claude-voice texts'}` on {machine}"

        def text(entry) -> str:
            return entry.get("text") or "" if isinstance(entry, dict) else ""

        claude_code = self.start.surface == "claude-code"
        tools_arrive = "when the tool is loaded (deferred in Claude Code)" if claude_code else STAGE_NAMES[TOOLS]
        if text(b.get("instructions")):
            self.push(Piece(f"instructions:{BRIDGE_SERVER}", TOOLS, "handed to the client when it connects to the "
                            "bridge", where, text(b["instructions"]), rule=False))
        if text(b.get("guide")):
            if not self.relay():  # a chat or a session that may call the bridge: the guide waits for its first call
                self.push(Piece(f"guide:{BRIDGE_SERVER}", LATER, "with its first result from the bridge (or its guide "
                                "tool)", where, text(b["guide"]), None, "when it first calls the bridge", always=False,
                                rule=False))
            else:
                self.push(Piece(f"guide:{BRIDGE_SERVER}", BRIDGE, "with the relay's first result from the bridge",
                                where, text(b["guide"]), rule=False))
        for t in b.get("tools") or []:
            if isinstance(t, dict) and t.get("name") and text(t):
                self.push(Piece(f"tool:{BRIDGE_SERVER}:{t['name']}", TOOLS, tools_arrive, where, text(t), rule=False))
        if claude_code:  # the notes come under what the bridge relays to a session
            for n, note in enumerate(b.get("notes") or [], 1):
                if text(note):
                    when = note.get("when") or "unplaced"
                    self.push(Piece(f"bridge:{BRIDGE_SERVER}#{n}", LATER, f"from the bridge: {when}", where,
                                    text(note), None, when, always=False, rule=False))

    def load_memory(self) -> None:
        text = self.v.text(MEMORY_NOTE) or ""
        lines = section(text, "Lines") or ""
        how = "loaded at the start by claude.ai (memory, as its mirror states it)"
        self.push(Piece("memory:/preferences.md", LOADED, how, f"{self.v.where(MEMORY_NOTE)} ## Lines", lines))
        profile = section(text, "Profile")
        if profile:
            self.push(Piece("memory:/profile.md", LOADED, how, f"{self.v.where(MEMORY_NOTE)} ## Profile", profile,
                            rule=False))

    def load_relay_notes(self) -> None:
        if self.snap is None:
            self.not_followed.append("the relay's notes: no PC has written a snapshot yet")
            return
        for pid in self.snap.relay_notes:
            if not pid.startswith("note:"):
                continue
            r = self.v.lookup(pid[5:]) or pid[5:]
            self.push(Piece(f"note:{r}", BRIDGE, f"handed to the relay with the bridge's guide (relay_notes in "
                            f"{self.snap.relay_config})", self.v.where(r), self.v.text(r) or "", "relay_notes",
                            "when met"))

    def load_work(self) -> None:
        """What `open_area` serves, read from the files: the area's `## For agents`, its main note without that
        section and its Tasks, Vault conventions' "Working on an area", coding-practices for a code project, and
        the tool's own instruction lines as served."""
        name, asked = self.work.area, "work.area"
        home = self.snap.home if self.snap else None
        if (not name and self.start.folder and self.v.rel(self.start.folder) is None
                and pc_norm(self.start.folder).lower() != (home or "").lower()):
            # The session-start hook has it open the area that lists its folder.
            name, asked = self.start.folder, "start.folder"
        if not name:
            return
        area = self.v.area(name)
        if not area or not area["main"]:
            self.broken.append({"from": asked, "target": name, "via": f"no area in {AREAS_NOTE} lists it"})
            return
        main = area["main"]
        text = self.v.text(main) or ""
        how = f"served by open_area on {area['name']}"
        rules = section(text, "For agents")
        if rules:
            self.push(Piece(f"section:{main}#For agents", WORK, how + " (for_agents)", self.v.where(main), rules))
        # Not a rule file, but the agent reads it: its pointers are followed and flagged as outside the rule files.
        self.push(Piece(f"{OPEN_AREA_ID}{main}", WORK, how + " (main_note_text, without For agents and Tasks)",
                        self.v.where(main), without_sections(text, ["For agents", "Tasks"]), rules_only=True))
        conv = section(self.v.text(CONVENTIONS_NOTE) or "", "Working on an area")
        if conv:
            self.push(Piece(f"section:{CONVENTIONS_NOTE}#Working on an area", WORK, how + " (area_conventions)",
                            self.v.where(CONVENTIONS_NOTE), conv))
        if frontmatter_flag(text, "code_project"):
            self.push(self.note_piece(CODING_GUIDE, WORK, how + " (code_project: true)", None,
                                      "code_project: true in the main note's frontmatter", "when met", True))
        self.push(Piece(f"tool:{SERVER}:open_area/instructions", WORK, how + " (instructions)",
                        f"{SERVER}: open_area's result", self.setup.served.open_area_instructions, rule=False))
        self.not_followed.append("open_area's data lines: the area map, ledger entries and open tasks (built from "
                                 "the vault and the ledger at each call, not rules)")

    def load_tools(self, server: str) -> None:
        if server != SERVER:
            self.not_followed.append(f"tool texts of {server}")
            return
        served = self.setup.served
        deferred = self.start.surface == "claude-code"
        how = "when the tool is loaded (deferred in Claude Code)" if deferred else STAGE_NAMES[TOOLS]
        self.push(Piece(f"instructions:{SERVER}", TOOLS, "handed to the client when it connects (claude.ai is "
                        "reported to drop them)", f"{SERVER}: the server instructions", served.instructions,
                        rule=False))
        # On every path: every agent on the connector reads the guide first, as the server's instructions and its
        # tool's first line say (the fork's decision "Agents read the guide": one that skips it is a bug to fix).
        self.push(Piece(f"guide:{SERVER}", TOOLS, "returned by get_vault_guide, which an agent on the connector "
                        "calls first (the server's instructions and the tool's first line say so), ahead of the "
                        f"vault's conventions note ({served.guide_note})", f"{SERVER}: get_vault_guide", served.guide,
                        rule=False))
        # The conventions note comes in the same result, after the guide's own part: on the path as the guide is.
        r = self.v.lookup(served.guide_note)
        if r is not None:
            line = next((t.splitlines()[0] for n, t in served.tools if n == "get_vault_guide" and t), None)
            self.push(self.note_piece(r, TOOLS, f"returned by get_vault_guide after the connector's own guide "
                                      f"(guide:{SERVER})", f"tool:{SERVER}:get_vault_guide", line,
                                      "when get_vault_guide is called", True))
        for name, text in served.tools:
            self.push(Piece(f"tool:{SERVER}:{name}", TOOLS, how, f"{SERVER}: tools/list", text, rule=False))

    def note_not_followed(self) -> None:
        nf = self.not_followed
        nf.append(by_design(self.v))
        if self.start.surface == "claude-code":
            if self.snap is None or self.snap.hook_messages is None:  # a snapshot from before `hook_messages`
                events = sorted({h.get("event", "") for h in (self.snap.hooks if self.snap else [])} - {"SessionStart"})
                nf.append("hook messages at other events" + (f" ({', '.join(events)})" if events else "")
                          + ": they print only when their moment comes, in code")
            nf.append("what the Claude desktop app adds at a session's start (its own SessionStart context)")
        if self.on_bridge() and not (self.snap and self.snap.bridge_known):  # a snapshot from before `bridge`
            nf.append("the voice bridge's tool texts and guide, and its note under each relayed message (in its code)")
        if self.start.surface == "claude-ai":
            nf.append("claude.ai's own system prompt and memory instructions (not the owner's)")


@functools.lru_cache(maxsize=4096)
def unfenced(text: str) -> tuple[str, ...]:
    """The lines of `text` outside fenced code blocks: code, not links. Kept per text, as `section`."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return tuple(out)


def file_targets(line: str) -> list[tuple[str, int]]:
    """The docs a line of a file outside the vault points to: Markdown links to a `.md` file (not a URL) and
    backticked `.md` paths, each with where it sits in the line."""
    out = [(m.group(1), m.start()) for m in MD_LINK.finditer(line)
           if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]+:", m.group(1))]  # a URL; a drive letter is one character
    out += [(m.group(1), m.start()) for m in BACKTICK_MD.finditer(line)]
    return sorted(out, key=lambda x: x[1])


def note_pattern(target: str) -> re.Pattern | None:
    """A backticked vault path holding `<placeholder>` segments, as a pattern over vault paths; None for anything
    else (a home-folder or absolute path, an import, a path with no placeholder).

    A placeholder stands for one folder or file name, a name used twice for the same one (case ignored). A
    placeholder opening the path stands for any folder, its last name being the one a repeat compares with:
    `<folder>/<folder>.md` is every folder note, at any depth.
    """
    t = target.strip().replace("\\", "/")
    if not PLACEHOLDER.search(t) or t.startswith(("~", "/", "@")) or "@" in t or re.match(r"^[A-Za-z]:", t):
        return None
    out, groups, pos = [], {}, 0
    for m in PLACEHOLDER.finditer(t):
        out.append(re.escape(t[pos:m.start()]))
        name = m.group(0).lower()
        if name in groups:
            out.append(f"(?P={groups[name]})")
        else:
            groups[name] = f"p{len(groups)}"
            any_depth = m.start() == 0 and t[m.end():m.end() + 1] == "/"
            out.append((r"(?:[^/]+/)*" if any_depth else "") + f"(?P<{groups[name]}>[^/]+)")
        pos = m.end()
    out.append(re.escape(t[pos:]))
    return re.compile("".join(out), re.I)


def sentence_at(line: str, pos: int) -> tuple[str, int]:
    """The sentence of the line holding position pos, with the next one when it reads it back ("Read that note"),
    and pos within it."""
    spans, start = [], 0
    for m in re.finditer(r"(?:(?<=[.!?])|(?<=[.!?]\*\*)|(?<=[.!?][*_]))\s+(?=[A-Z\[*])", line):
        spans.append((start, m.start()))
        start = m.end()
    spans.append((start, len(line)))
    for i, (a, b) in enumerate(spans):
        if a <= pos <= b:
            text = line[a:b]
            if i + 1 < len(spans):
                nxt = line[spans[i + 1][0]:spans[i + 1][1]]
                if re.match(r"^(?:Read|Follow) (?:that|this|it)\b", nxt):
                    text += " " + nxt
            return text, pos - a
    return line, pos


def reads_it(sentence: str, at: int) -> bool:
    """Whether the sentence tells the agent to read the link at `at`: a reading verb before it in the same clause
    ("read [[X]]", "Read the vault note X"), or one reading it back right after ("[[X]]: read it first").
    A clause ends at ; ( or )."""
    before = re.split(r"[;()]", sentence[:at])[-1]
    after = re.split(r"[;()]", sentence[at:])[0]
    return bool(re.search(r"\b(?:read|reread|follow)\b(?!\s+(?:it|that|this)\b)[^.:]*$", before, re.I)
                or re.search(r"\b(?:read|follow)\s+(?:it|that note|this note|them)\b", after, re.I))


def pointer_moment(line: str, sentence: str, target: str | None = None) -> str | None:
    """When a pointer in one of the convention's shapes says to read its target (`POINTER_LEAD`): the guide index's
    "when to read it" (for the line's leading link only, when `target` names the link), or the opening of its bullet
    or sentence; None for a line in none of the shapes."""
    s = re.sub(r"^\s*(?:>\s*)*(?:[-*]|\d+\.)?\s*", "", line).strip()  # quote or callout marks, then a bullet
    s = re.sub(r"\s*\[[PQFC]\d+(?:,\s*[PQFC]\d+)*\]", "", s)  # ledger refs aren't part of a moment
    index = INDEX_SHAPE.match(s)
    if index and target is not None and index_target(index.group(1)) != index_target(target):
        index = None  # another link on an index line isn't the note the line is about
    if index:  # the guide index's shape: the guide, then when to read it
        return re.split(r"(?<=[a-z\)])\.(?:\s|$)|\s\|\s", index.group(2))[0].strip().rstrip(".")
    candidates = [sentence]
    if re.match(r"^\s*(?:>\s*)*(?:[-*]|\d+\.)\s", line):  # a bullet's opening covers the whole bullet
        candidates.insert(0, s)
    for cand in candidates:
        lead = POINTER_LEAD.match(NOT_LETTERS.sub("", cand))
        if lead:
            m = lead.group(1).replace("**", "").strip()  # a bold opening is still the opening
            if m.count("(") > m.count(")"):  # cut by a comma inside a parenthesis: keep what comes before it
                m = m[: m.rfind("(")].strip()
            return m
    return None


def index_target(link: str) -> str | None:
    """What an index line's leading link names, to compare with a pointer's target: a wikilink's note (None for
    `[[#Heading]]`), a Markdown link's path or a backticked path, without an anchor, case ignored. Anything else
    (a pointer's target as found) is taken as the path itself."""
    m = re.match(r"^\[\[([^\]|#]*)", link)
    if m:
        return m.group(1).strip().rstrip("\\").casefold() or None
    m = re.match(r"^\[[^\]\n]*\]\(<?([^)\s<>]+?)>?\)$", link) or re.match(r"^`([^`\n]+)`$", link)
    path = m.group(1) if m else link
    return path.split("#")[0].strip().replace("\\", "/").casefold() or None


#: Words that point back into the line a moment was cut from ("When choosing it", "When working in this area",
#: "When starting one"): such a moment doesn't stand alone.
BACK_REFERENCE = re.compile(r"\b(?:it|its|it's|this|these|those|there|here|that(?=\s*(?:[.,;:)]|$))"
                            r"|such(?!\s+as\b)|one(?!\s+of\b))\b", re.I)


def stands_alone(moment: str | None) -> bool:
    """Whether a moment reads right without its line: it holds no word pointing back into the line (`it`, `its`,
    `this`, `these`, `those`, `there`, `here`, `that` ending a clause, `such` but not "such as", `one` but not
    "one of"). They, them and their are left out: in the vault they are the owner's pronouns ("about their
    areas"). The test is on words, not meaning: a word caught wrongly only costs a line shown whole; a noun
    naming what the line is about ("When the area is new") isn't caught. Only the moment's own clause counts: what
    is in parentheses or quotes, and anything after a colon or semicolon (the guide index's "when to read it" may
    go on: "…: it starts with an upstream check"), is left out."""
    own = re.sub(r"\([^()]*\)|\"[^\"]*\"|“[^”]*”", "", moment or "")
    return not BACK_REFERENCE.search(re.split(r"[:;]", own)[0])


def moment_of(line: str, sentence: str, at: int = 0) -> str:
    """When the pointing line says to read the target: a pointer shape's moment (`pointer_moment`), "when met" for
    a plain reading verb aimed at it (outside the rule notes, such as a hook's message), else "unplaced"."""
    return pointer_moment(line, sentence) or ("when met" if reads_it(sentence, at) else "unplaced")


# --- runs ----------------------------------------------------------------------------------------------------

def handwritten_paths(v: Vault) -> dict[int, str]:
    text = section(v.text(WHERE_NOTE) or "", HANDWRITTEN_HEADING) or ""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\d+)\.\s+(.*)", line)
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    return out


def by_design(v: Vault) -> str:
    """What the tools leave out by design, for `not_followed`: `WHERE_NOTE`'s paragraph saying it, read at each call
    so the list has one home, without its ledger refs."""
    text = section(v.text(WHERE_NOTE) or "", HANDWRITTEN_HEADING) or ""
    para = next((line.strip() for line in text.splitlines() if line.startswith(BY_DESIGN_OPENING)), None)
    if para is None:
        return (f"what the tools leave out by design: \"{HANDWRITTEN_HEADING}\" in {WHERE_NOTE} holds no paragraph "
                f"opening \"{BY_DESIGN_OPENING}\"")
    return re.sub(r"\s*\[[PQFC]\d+(?:,\s*[PQFC]\d+)*\]", "", para)


def run(setup: Setup, start: Start, work: Work) -> PathBuilder:
    return PathBuilder(setup, start, work).build()


def structure_of(b: PathBuilder) -> dict:
    pieces = [b.pieces[i] for i in b.order]
    tools = [p for p in pieces if tool_text(p)]
    always = [p for p in pieces if p.always and not tool_text(p)]
    later = [p for p in pieces if not p.always and not tool_text(p)]
    unplaced = [{"piece": p.id, "from": src, "via": line} for p in pieces for moment, src, line in p.arrivals
                if moment == "unplaced"]
    return {
        "size": {"always_chars": sum(p.chars for p in always), "always_pieces": len(always),
                 "when_moment_chars": sum(p.chars for p in later), "when_moment_pieces": len(later),
                 "tool_text_chars": sum(p.chars for p in tools), "tool_texts": len(tools)},
        "unplaced_pointers": unplaced,
        "pointers_outside_rule_files": outside_rules(b),
        "into_areas": into_areas(b),
        "mentions_not_followed": len(b.mentions),
        "mentions": [m for m in b.mentions if not m.get("rule_file")],
        "rule_file_mentions": [m for m in b.mentions if m.get("rule_file")],
        "broken_pointers": b.broken,
        "skipped_imports": b.skipped,
        "import_warnings": b.import_warnings,
        "not_followed": b.not_followed,
    }


def tool_text(p: Piece) -> bool:
    """A server's own text (a tool's, its instructions, its guide), at the stage the tools load; not a vault note
    arriving there (the conventions note, with the guide)."""
    return p.stage == TOOLS and not p.id.startswith(VAULT_IDS)


def pointing(p: Piece) -> list[tuple[str, str | None]]:
    """The pieces pointing to `p`, each once, with the first line by which it does."""
    out: dict[str, str | None] = {}
    for _, src, line in p.arrivals:
        if src and src not in out:
            out[src] = line
    return list(out.items())


def outside_rules(b: PathBuilder) -> list[dict]:
    """Rule files reached through a pointer that sits outside every rule file (a main note's text, a non-rule note)."""
    out = []
    for p in b.pieces.values():
        for parent, line in pointing(p):
            if not parent.startswith(("note:", OPEN_AREA_ID)):
                continue
            src = parent.split(":", 1)[1]
            if not b.v.is_rule_file(src) and p.id.startswith("note:") and b.v.is_rule_file(p.id.split(":", 1)[1]):
                out.append({"from": parent, "to": p.id, "via": line})
    return out


def into_areas(b: PathBuilder) -> list[dict]:
    """Pointers into an area's local pieces: from a shared piece (any rule file outside Areas/), or from one area's
    local pieces into another's (which brings the combinations back). A shared piece is met by every agent of a
    kind; a local one only by agents working in that area, folder, machine or repo [P276]."""
    out = []
    for p in b.pieces.values():
        for parent, _ in pointing(p):
            if not p.id.startswith(VAULT_IDS):
                continue
            target = p.id.split(":", 1)[1].split("#")[0]
            to_area = b.v.area_of_note(target)
            if not to_area:
                continue
            src = parent.split(":", 1)[1].split("#")[0] if parent.startswith(VAULT_IDS) else None
            from_area = b.v.area_of_note(src) if src else None
            if from_area and from_area["name"] == to_area["name"]:
                continue
            out.append({"from": parent, "to": p.id,
                        "kind": "local into another local" if from_area else "shared into local"})
    return out


def rule_sections_in(text: str, headings: list[str]) -> tuple[str, ...]:
    """The rule sections `text` holds, by heading, in `headings`' order."""
    return tuple(h for h in headings if section(text, h))


def every_rule_file(v: Vault, sections_of: Callable[[str], tuple[str, ...]] | None = None) -> list[str]:
    """Every rule note, and every rule section of another note. `sections_of(r)` gives a note's rule sections
    (`rule_sections_in`) when the caller keeps them (a kept drawing, by each note's stat); otherwise each note is
    read here."""
    out = []
    for r in v.by_rel.values():
        if v.is_rule_file(r):
            out.append(f"note:{r}")
        elif r.endswith(".md"):
            held = sections_of(r) if sections_of else rule_sections_in(v.text(r) or "", v.rule_headings)
            out += [f"section:{r}#{h}" for h in held]
    return sorted(out)


def run_starts(setup: Setup):
    """Every kind of agent a path is drawn for: the typical agents, a claude.ai chat opening each area, and a
    Claude Code session in each related folder the PC's snapshot holds, as `(label, start, work)`."""
    v, snap = setup.vault, setup.snapshot
    for t in setup.points.typical:
        yield t.name, t, Work()
    for a in v.areas:
        yield f"a claude.ai chat working on {a['name']}", Start("claude-ai"), Work(a["name"])
        for f in a["folders"]:
            if snap and snap.holds(f):
                yield f"a Claude Code worker in {f}", Start("claude-code", f, role="worker"), Work()


def iter_runs(setup: Setup):
    """`run_starts` with each path drawn, only when the caller asks for the next one."""
    for label, s, w in run_starts(setup):
        yield label, s, w, run(setup, s, w)


def all_runs(setup: Setup) -> list[tuple[str, Start, Work, PathBuilder]]:
    return list(iter_runs(setup))


def registered_hook_scripts(setup: Setup) -> set[str]:
    """The vault's hook scripts the PC registers (`note:<path>`): they reach agents as hooks, whatever points to them."""
    out = set()
    for h in setup.snapshot.hooks if setup.snapshot else []:
        for a in h.get("args", []):
            r = setup.vault.rel(str(a)) if str(a).endswith(".py") else None
            if r:
                out.add(f"note:{r}")
    return out


def reached_places(setup: Setup, places, runs) -> set[str]:
    """Which of `places` (piece ids, `note:<path>` or `section:<path>#<heading>`) some path reaches: one of `runs`
    has it on its path, or it is a hook script the PC registers. This is what "reached" means for the rule files
    no path reaches. Stops drawing paths once every place is found, so `runs` may be `iter_runs`."""
    places = set(places)
    found = places & registered_hook_scripts(setup)
    if found == places:
        return found
    for *_, b in runs:
        found |= places & b.pieces.keys()
        if found == places:
            break
    return found


def unreached_rule_files(setup: Setup, runs, every: list[str] | None = None) -> list[str]:
    """Rule files no run reaches (`reached_places`); `every` is `every_rule_file`, when the caller has it."""
    every = every_rule_file(setup.vault) if every is None else every
    reached = reached_places(setup, every, runs)
    return [r for r in every if r not in reached]


def across_runs(runs) -> dict:
    """The structural facts over every run, each pointer once."""
    seen, across = set(), {"pointers_outside_rule_files": [], "into_areas": []}
    for *_, b in runs:
        st = structure_of(b)
        for k in across:
            for x in st[k]:
                key = (k, x["from"], x["to"])
                if key not in seen:
                    seen.add(key)
                    across[k].append(x)
    return across


def chain(b: PathBuilder, pid: str) -> list[str]:
    out, cur = [], pid
    while cur and cur in b.pieces and cur not in out:
        out.append(cur)
        cur = b.pieces[cur].parent
    return list(reversed(out))


def matches(place: str, pid: str) -> bool:
    place = place.removeprefix("note:").removeprefix("section:")
    body = pid.split(":", 1)[1] if ":" in pid else pid
    if pid.startswith("hook:"):  # `hook:<event>/<script>` also names each of its messages, `…#<n>`
        return place in (pid, body) or ("#" not in place and place in (pid.split("#")[0], body.split("#")[0]))
    if pid.startswith("bridge:"):  # `bridge:claude-voice` also names each of its notes, `…#<n>`
        return place in (pid, body, pid.split("#")[0])
    if pid.startswith(("tool:", "memory:", "guide:", "instructions:")):
        return pid == place or body == place
    return body.lower() == place.lower() or (("#" not in place) and body.lower().split("#")[0] == place.lower())


def place_exists(v: Vault, place: str) -> bool | None:
    """Whether a vault place exists: the note, and for `note.md#Heading` a heading of that name in it (any level).
    None for a place outside the vault (a hook, a tool's text, memory)."""
    if not place.startswith(("note:", "section:")) and re.match(r"^[a-z_]+:", place) and not re.match(
            r"^[A-Za-z]:[\\/]", place):
        return None
    note, _, heading = place.removeprefix("note:").removeprefix("section:").partition("#")
    r = v.lookup(note)
    if r is None:
        return False
    return not heading or any(re.fullmatch(rf"#{{1,6}}\s+{re.escape(heading.strip())}\s*#*\s*", line)
                              for line in unfenced(v.text(r) or ""))


def reached_by(setup: Setup, place: str, runs: list | None = None) -> dict:
    """Who reaches `place`, over `runs` (`all_runs`, drawn here when not given)."""
    v = setup.vault
    runs = all_runs(setup) if runs is None else runs
    hits, how = [], "the place itself"
    # A bare hook or bridge id names each of its messages: each one met is a hit of its own.
    every = place.startswith(("hook:", "bridge:")) and "#" not in place
    for label, s, w, b in runs:
        ids = [i for i in b.order if matches(place, i)]
        for i in ids if every else ids[:1]:
            hits.append((label, s, w, b, i))
    if hits and "#" not in place and not place.startswith(("hook:", "bridge:", "tool:", "memory:", "guide:",
                                                           "instructions:")) and all(
            h[4].startswith("section:") for h in hits):
        # A whole note no path reads, whose rule sections paths do reach (a machine note's `## For agents`).
        how = "its rule sections (the note itself is on no path)"
    if not hits:  # a place no path holds (one that doesn't exist yet, or a part of a note): its note, then its area
        note = place.removeprefix("note:").removeprefix("section:").split("#")[0]
        for label, s, w, b in runs:
            ids = [i for i in b.order if matches(note, i)]
            if ids:
                hits.append((label, s, w, b, ids[0]))
        exists = place_exists(v, place)
        how = "its note" + ("" if "#" not in place else " (it holds that section)" if exists
                            else " (the section isn't in it yet)" if v.lookup(note) else "")
        if not hits:
            area = v.area_of_note(note)
            told = "it exists" if exists else "it doesn't exist yet" if exists is False else None
            reach = "it, its note or its area" if "#" in place else "it or its area"
            how = (f"its area ({area['name']})" if area else
                   f"nothing: {told}, and no path reaches {reach}" if told
                   else "nothing: no path reaches its note or its area")
            if area:
                for label, s, w, b in runs:
                    if w.area == area["name"] or (s.folder and v.area(s.folder) == area):
                        first = next((i for i in b.order if b.pieces[i].stage == WORK), b.order[0])
                        hits.append((label, s, w, b, first))
    groups: dict[tuple, list[str]] = {}
    #: Each group's chains as they are: when they are all one (a single area), it shows that one, not a placeholder.
    concrete: dict[tuple, set] = {}
    for label, s, w, b, pid in hits:
        c = chain(b, pid)
        # The place itself keeps its own id: only the pieces leading to it stand for any area's.
        key = (s.surface, tuple(x if x == pid else generic(v, x) for x in c), b.pieces[pid].always,
               re.sub(r" on .*? \(", " on <the area> (", b.pieces[c[0]].arrives))
        groups.setdefault(key, []).append(label)
        concrete.setdefault(key, set()).add((tuple(c), b.pieces[c[0]].arrives))
    # Group by run kind, not by label: a typical agent's name may share the per-folder runs' prefix.
    typical_names = {t.name for t in setup.points.typical}
    chat_runs = {l for l, *_ in runs if l not in typical_names and l.startswith("a claude.ai chat working on")}
    worker_runs = {l for l, *_ in runs if l not in typical_names and l.startswith("a Claude Code worker in ")}
    reached = []
    for (surface, c, always, first), labels in groups.items():
        if len(concrete[(surface, c, always, first)]) == 1:
            (c, first), = concrete[(surface, c, always, first)]
        chats = [l for l in labels if l in chat_runs]
        workers = [l for l in labels if l in worker_runs]
        named = [l for l in labels if l not in chats and l not in workers]
        if chats and set(chats) == chat_runs:
            named.append("a claude.ai chat, any area")
        else:
            named += chats
        if workers and set(workers) == worker_runs:
            named.append("a Claude Code worker, any related folder")
        else:
            named += workers
        reached.append({"agents": named, "surface": surface, "via": list(c), "always": always, "first_arrives": first})
    union: dict[str, Piece] = {}
    for _, _, _, b, _ in hits:
        for i in b.order:
            union.setdefault(i, b.pieces[i])
    # The facts on those paths, each pointer once; what can't be followed, each line once.
    hit_runs = [(label, s, w, b) for label, s, w, b, _ in hits]
    structure = across_runs(hit_runs)
    structure["not_followed"] = list(dict.fromkeys(x for *_, b in hit_runs for x in b.not_followed))
    for key, attr in (("skipped_imports", "skipped"), ("import_warnings", "import_warnings")):
        structure[key] = []
        for *_, b in hit_runs:
            structure[key] += [e for e in getattr(b, attr) if e not in structure[key]]
    return {"place": place, "matched_by": how, "reached_by": reached, "pieces": union, "structure": structure}


def generic(v: Vault, pid: str) -> str:
    """A piece id with the area's own part replaced, so paths through different areas group together."""
    body = pid.split(":", 1)[1] if ":" in pid else pid
    area = v.area_of_note(body.split("#")[0]) if pid.startswith(VAULT_IDS) else None
    if area and area["main"] and body.split("#")[0] == area["main"]:
        return pid.replace(body.split("#")[0], "<the area's main note>")
    return pid
