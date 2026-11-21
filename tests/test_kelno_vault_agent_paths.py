"""The kelno_vault plugin's agent path tools: `agent_path` and `agent_path_reached_by`, on a small vault.

The reading rules themselves were checked against the PC prototype on the real vault (the done check in the
vault note "Agent paths - path tool"); these tests pin how the tools put the pieces together: what loads
first, the session-start hook and its role note, what opening the area brings, the served texts, hashes and
`known`, the size budget, private notes, and the backward question.
"""

import asyncio
import hashlib
import json
import os

import pytest

from src.auth.session import current_user_id
from src.mcp_server.auth import current_permission
from src.mcp_server import tools
from src.plugins.kelno_vault import agent_path_tools as apt
from src.plugins.kelno_vault import agent_paths as ap
from src.plugins.kelno_vault.agent_path_result import PathStart, PathWork, ReadFrom
from src.services import vault as vault_service

RULES = "_/" + "Rule files.md"

SNAPSHOT = {
    "format": 1, "machine": "PC", "made": "2026-11-18T10:00:00+01:00", "vault": "D:/vault", "home": "C:/Users/me",
    "folders": {
        "C:/Users/me": {"loads": [
            {"id": "file:C:/Users/me/.claude/CLAUDE.md", "exists": True,
             "arrives": "loaded at the start by Claude Code (user CLAUDE.md)"},
            {"id": "note:_/agent/Common-rules.md", "exists": True,
             "arrives": "imported by file:C:/Users/me/.claude/CLAUDE.md"}], "session_start": []},
        "D:/work/repo": {"loads": [
            {"id": "file:C:/Users/me/.claude/CLAUDE.md", "exists": True,
             "arrives": "loaded at the start by Claude Code (user CLAUDE.md)"},
            {"id": "note:_/agent/Common-rules.md", "exists": True,
             "arrives": "imported by file:C:/Users/me/.claude/CLAUDE.md"},
            {"id": "file:D:/work/repo/CLAUDE.md", "exists": True,
             "arrives": "loaded at the start by Claude Code (D:/work/repo)"}],
            "session_start": [{"id": "hook:SessionStart/area-start.py", "matcher": "startup", "hash": "x", "text": (
                "You work in the owner's setup. Read the vault note D:\\vault\\_\\agent\\roles\\Worker.md and "
                "follow it, unless you are the Coordinator: then read your own role note, "
                "D:\\vault\\_\\agent\\roles\\Coordinator.md, instead.")},
                {"id": "hook:SessionStart/quiet.py", "matcher": "startup", "hash": "y", "text": ""}]},
    },
    "files": {
        "file:C:/Users/me/.claude/CLAUDE.md": {"text": "@D:/vault/_/agent/Common-rules.md\n"},
        "file:D:/work/repo/CLAUDE.md": {"text": "Repo rules.\n"},
    },
    "hooks": [
        {"event": "SessionStart", "matcher": "startup", "command": "python",
         "args": ["D:/vault/_/claude-code-hooks/area-start.py"]},
        {"event": "PreToolUse", "matcher": "Bash", "command": "python", "args": ["D:/vault/_/claude-code-hooks/guard.py"]},
    ],
    "relay_notes": ["note:_/agent/roles/Relay.md"],
    "relay_config": "C:/Users/me/.config/claude-voice/config.toml",
}

STARTING_POINTS = """
[surface.claude-code]
loads = ["user_claude_md", "folder_claude_md", "session_start_hooks"]
tools = ["obsidian-mcp"]

[surface.claude-ai]
loads = ["memory"]
tools = ["obsidian-mcp"]

[[typical]]
name = "a claude.ai chat"
surface = "claude-ai"

[[typical]]
name = "a Claude Code worker in a repo"
surface = "claude-code"
folder = "D:/work/repo"
role = "worker"
"""

NOTES = {
    RULES: "The rule files.\n\n```rule-files\n_/agent/*\nCLAUDE.md\n## For agents\n```\n",
    "CLAUDE.md": "@_/agent/Common-rules.md\n",
    "_/agent/Common-rules.md": (
        "Rules every agent follows.\n\n"
        "- Before working on one of the owner's areas, read [[_/agent/Vault conventions|Vault conventions]].\n"
        "- See also [[Notes/Some note]] for fun.\n"
        "- [[_/agent/guides/coding-practices|coding-practices]]: any code work.\n"
        "- The owner keeps [[_/agent/Notes kept]] beside these.\n"),
    "_/agent/Vault conventions.md": "Conventions.\n\n## Working on an area\n\n- Start with open_area.\n",
    "_/agent/Working on an area.md": "- Keep the Current position current.\n",
    "_/agent/Notes kept.md": "Kept.\n",
    "_/agent/Memory reference.md": (
        "Mirror.\n\n## Lines\n\n- At the start of every chat, read [[_/agent/Common-rules|Common-rules]].\n"),
    "_/agent/roles/Worker.md": "Worker rules.\n",
    "_/agent/roles/Coordinator.md": "Coordinator rules.\n",
    "_/agent/roles/Relay.md": "Relay rules.\n",
    "_/agent/guides/coding-practices.md": "Code rules.\n",
    "_/agent/Where agent instructions go.md": (
        "## Checking a rule against agent paths\n\n1. A chat: memory, then Common-rules.\n"
        "3. A worker: Common-rules, the hook, Worker.\n\n"
        "The tools don't follow, by design: in a repo, anything beyond what Claude Code loads there. When one of "
        "these matters, point to it from a rule file the tools follow. [P277]\n"),
    "Notes/Some note.md": "Just a note.\n",
    "Areas/Areas.md": ("| Area | Related folders | Vault folder |\n| --- | --- | --- |\n"
                       "| Repo | `D:\\work\\repo` | [[Areas/Repo/Repo\\|Repo]] |\n"),
    "Areas/Repo/Repo.md": ("---\ncode_project: true\n---\nMain note.\n\n## For agents\n\n"
                           "- Read [[Areas/Repo/Repo notes]] before changing the build.\n\n"
                           "## Current position\n\nFine.\n"),
    "Areas/Repo/Repo notes.md": "How the build works.\n",
    "Areas/Repo/Secret (private)/Plan.md": "Private plan.\n",
    ap.STARTING_POINTS_PATH: STARTING_POINTS,
    f"{ap.SNAPSHOT_FOLDER}/PC.json": json.dumps(SNAPSHOT),
}


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    vault_service.clear_user_vault_cache()

    async def no_ledger(uid):
        return ""

    monkeypatch.setattr(tools, "_ledger_guide", no_ledger)
    # The kept drawing lives in this process, fresh for each test (production keeps it in the helper process).
    monkeypatch.setattr(apt, "DRAW_IN_HELPER", False)
    monkeypatch.setattr(apt, "_KEPT", {})
    monkeypatch.setattr(apt, "_SECTIONS", {})
    monkeypatch.setattr(apt, "_DRAWERS", {})
    for path, text in NOTES.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, encoding="utf-8")
    user = current_user_id.set(None)
    yield tmp_path
    current_user_id.reset(user)


def path(start=None, work=None, known=None):
    return asyncio.run(apt.agent_path_impl(start, work, known))


def moments(piece):
    """A piece's moments: those that bring it when it waits on one, else those its pointing lines give (round 5)."""
    return piece.when or [x.when for x in piece.lines or []]


def always(piece):
    """On the path whatever the conversation brings (round 5: `every_time`, what brings it)."""
    return piece.every_time is not None


def drawn(start, work=None) -> dict:
    """The engine's pieces for a start, from the kept drawing (their stage, `always`, `arrives` and moments' details,
    which the result no longer shows since round 5)."""
    path(start, work)  # draws, or brings the kept drawing up to date
    (k,) = apt._KEPT.values()
    w = ap.Work(work.area, work.kind) if work else ap.Work()
    return k.run_for(apt._start_of(start, k.setup.points), w).pieces


def ids(result):
    return [p.id for p in result.pieces]


def test_hash_is_the_first_12_hex_digits_of_sha256_of_the_text_as_the_pc_reads_it():
    assert ap.digest("abc") == hashlib.sha256(b"abc").hexdigest()[:12]
    assert ap.as_text(b"a\r\nb\rc") == "a\nb\nc"


def test_agent_path_draws_a_path_with_texts_hashes_and_known(vault):
    first = path(PathStart(typical=2))
    assert first.error is None and first.machine == "PC" and first.snapshot_made == SNAPSHOT["made"]
    order = ids(first)
    # What Claude Code loads, then the hook, then the role note it names for a worker only.
    assert order[:3] == ["file:C:/Users/me/.claude/CLAUDE.md", "file:D:/work/repo/CLAUDE.md",
                         "note:_/agent/Common-rules.md"]
    assert order[3] == "hook:SessionStart/area-start.py" and "hook:SessionStart/quiet.py" not in order
    assert order[4] == "note:_/agent/roles/Worker.md" and "note:_/agent/roles/Coordinator.md" not in order
    # The repo's area, as open_area serves it, then what its rules point to.
    eng = drawn(PathStart(typical=2))
    work = [i for i in order if eng[i].stage == ap.WORK]
    # coding-practices, met earlier behind the guide index's moment, is on the path from the opening of an area
    # worked as a code project, whatever comes: it arrives there.
    assert work == ["section:Areas/Repo/Repo.md#For agents", "open_area:Areas/Repo/Repo.md",
                    "note:_/agent/Working on an area.md",
                    "note:_/agent/guides/coding-practices.md",
                    "tool:obsidian-mcp:open_area/instructions", "note:Areas/Repo/Repo notes.md"]
    # A pointer that says when comes later; a link that says neither when nor "read" is a mention, not followed
    # [P250] (round 5: no longer listed).
    by_id = {p.id: p for p in first.pieces}
    assert moments(by_id["note:_/agent/Vault conventions.md"]) == ["Before working on one of the owner's areas"]
    assert "note:_/agent/Notes kept.md" not in by_id and "note:Notes/Some note.md" not in by_id
    assert first.structure.model_dump(exclude_none=True).keys() <= {"size", "not_followed"}  # faults only
    # One copy, with the guide index's moment (code_project brings it with no line).
    assert moments(by_id["note:_/agent/guides/coding-practices.md"]) == ["when the area is opened", "any code work"]
    assert always(by_id["note:_/agent/guides/coding-practices.md"])
    # This server's texts as served come last.
    assert {"instructions:obsidian-mcp", "guide:obsidian-mcp", "tool:obsidian-mcp:read_note"} <= set(order)
    served = dict(asyncio.run(apt._served(None)).tools)
    assert "Read a note" in served["read_note"] and by_id["tool:obsidian-mcp:read_note"].hash == ap.digest(
        served["read_note"])
    # Every text on a first call, its hash the digest of it, until the budget: the tool texts don't all fit.
    assert len(first.model_dump_json()) <= apt.PAGE_CHARS
    for p in first.pieces:
        assert p.status is None and (p.text is None) == bool(p.text_withheld)
        assert p.text is None or (p.hash == ap.digest(p.text) and p.chars == len(p.text))
    assert all(p.text is not None for p in first.pieces if not ap.tool_text(eng[p.id]))
    assert any("PreToolUse" in line for line in first.structure.not_followed)

    known = {p.id: p.hash for p in first.pieces}
    again = path(PathStart(typical=2), known=known)
    assert not again.pieces and again.waiting == 0  # held pieces aren't listed again
    assert again.left_path is None
    gone = path(PathStart(typical=2), known={**known, "note:_/agent/Old rule.md": "0123456789ab"})
    assert gone.left_path == ["note:_/agent/Old rule.md"]

    (vault / "_/agent/roles/Worker.md").write_text("Worker rules, changed.\n", encoding="utf-8")
    changed = path(PathStart(typical=2), known=known)
    worker = next(p for p in changed.pieces if p.id == "note:_/agent/roles/Worker.md")
    assert worker.status == "changed" and worker.text == "Worker rules, changed.\n"
    assert [p.id for p in changed.pieces if p.text is not None] == ["note:_/agent/roles/Worker.md"]


def test_a_claude_ai_chat_starts_from_memory_and_reads_what_its_lines_point_to(vault):
    result = path(PathStart(surface="claude-ai"), PathWork(area="Repo"))
    order = ids(result)
    assert order[:2] == ["memory:/preferences.md", "note:_/agent/Common-rules.md"]
    assert moments(result.pieces[1]) == ["At the start of every chat"]
    assert "section:Areas/Repo/Repo.md#For agents" in order
    assert not any(i.startswith(("file:", "hook:")) for i in order)


def test_the_relay_role_adds_the_relays_notes(vault):
    result = path(PathStart(surface="claude-ai", role="relay"))
    assert "note:_/agent/roles/Relay.md" in ids(result)
    assert not any("changed nothing" in n for n in result.notes or [])


def test_held_pieces_come_short(vault):
    first = path(PathStart(typical=2))
    # A call with known lists only what it brings: nothing here, every piece held.
    again = path(PathStart(typical=2), known={p.id: p.hash for p in first.pieces})
    assert not again.pieces and again.waiting == 0 and again.left_path is None
    assert again.structure is None and any("comes only with a call without known" in n for n in again.notes)
    reached = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, None))
    held = {p.id: p.hash for p in reached.pieces}
    again = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", held, None))
    assert reached.reached_by and again.reached_by is None and again.structure is None and not again.pieces


def test_a_result_whose_metadata_passes_the_budget_is_cut_down_and_every_text_arrives(vault, monkeypatch):
    monkeypatch.setattr(apt, "PAGE_CHARS", 12_000)
    first = path(PathStart(typical=2))
    assert len(first.model_dump_json()) <= 12_000
    # Pieces whose text waits come in short form; those with a text carry their details.
    waiting = [p for p in first.pieces if p.text_withheld]
    assert waiting and all(p.where is None and p.chars is None and p.every_time is None and p.hash for p in waiting)
    assert all(p.where and p.chars is not None and (p.every_time or p.when) for p in first.pieces
               if p.text is not None)
    assert any("short form" in n for n in first.notes)
    # Passing what it now holds, the caller gets the rest: every text arrives within a few calls.
    # (A text too long for one result is read in parts: counted as held here.)
    held, rounds = {}, 0
    while rounds < 30:
        r = path(PathStart(typical=2), known=held or None)
        assert len(r.model_dump_json()) <= 12_000
        held |= {p.id: p.hash for p in r.pieces if p.text is not None or p.text_in_parts}
        rounds += 1
        if rounds > 1:  # a call with known lists only what it brings
            assert all(p.text is not None or p.text_in_parts or p.private for p in r.pieces)
        if not r.waiting and not any(p.text_withheld for p in r.pieces) and not any(
                "left out" in n for n in r.notes or []):
            break
    assert rounds < 30
    monkeypatch.setattr(apt, "PAGE_CHARS", 400_000)
    full = path(PathStart(typical=2))
    assert set(held) == {p.id for p in full.pieces if not p.private}


def test_the_overview_stays_within_the_budget(vault, monkeypatch):
    # Small enough that every list and piece line must be cut; the rest (names, notes, not_followed) can't be.
    monkeypatch.setattr(apt, "PAGE_CHARS", 2_700)
    over = path()
    assert len(over.model_dump_json()) <= 2_700
    assert any("cut short" in n for n in over.notes)
    assert any(line.startswith("… ") for t in over.typical for line in t.pieces)


def test_a_hook_script_changed_since_the_snapshot_is_said(vault):
    script = "_/claude-code-hooks/area-start.py"
    (vault / script).parent.mkdir(parents=True, exist_ok=True)
    (vault / script).write_text("print('hello')\n", encoding="utf-8")
    snap = {**SNAPSHOT, "format": 2, "hook_scripts": {f"note:{script}": ap.digest("print('hello')\n"),
                                                      "file:C:/elsewhere/other.py": None}}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")
    same = path(PathStart(typical=2))
    assert same.error is None and not any("changed since the snapshot" in n for n in same.notes or [])
    (vault / script).write_text("print('changed')\n", encoding="utf-8")
    changed = path(PathStart(typical=2))
    assert any(n.startswith(f"{script} changed since the snapshot") and "hook:SessionStart/area-start.py" in n
               and "agent-paths-snapshot.py run" in n and "ask the owner" in n for n in changed.notes)
    reached = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, None))
    assert any("changed since the snapshot" in n for n in reached.notes)


def test_user_rules_load_at_the_start_and_a_skipped_import_is_a_structural_fact(vault):
    # A missing user CLAUDE.md loads nothing; a file in ~/.claude/rules/ loads at the start; an import outside the
    # session's folder is skipped by Claude Code, listed by the snapshot, not loaded.
    rules = "loaded at the start by Claude Code (user rules)"
    loads = [
        {"id": "file:C:/Users/me/.claude/CLAUDE.md", "exists": False,
         "arrives": "loaded at the start by Claude Code (user CLAUDE.md)"},
        {"id": "file:C:/Users/me/.claude/rules/mine.md", "exists": True, "arrives": rules},
        {"id": "file:D:/work/repo/CLAUDE.md", "exists": True,
         "arrives": "loaded at the start by Claude Code (D:/work/repo)"},
        {"id": "file:D:/work/repo/CLAUDE.fork.md", "exists": True, "arrives": "imported by file:D:/work/repo/CLAUDE.md"}]
    skipped = [{"id": "note:_/agent/roles/Coordinator.md", "import": "@D:/vault/_/agent/roles/Coordinator.md",
                "in": "file:D:/work/repo/CLAUDE.md", "why": "outside the session's folder"}]
    snap = {**SNAPSHOT, "format": 2, "folders": {
        "C:/Users/me": {"loads": loads[:2], "skipped_imports": [], "session_start": []},
        "D:/work/repo": {**SNAPSHOT["folders"]["D:/work/repo"], "loads": loads, "skipped_imports": skipped}},
        "files": {"file:D:/work/repo/CLAUDE.md": {"text": (
            "Repo rules.\n@CLAUDE.fork.md\n@D:/vault/_/agent/roles/Coordinator.md\n")},
            "file:D:/work/repo/CLAUDE.fork.md": {"text": "Fork rules.\n"},
            "file:C:/Users/me/.claude/rules/mine.md": {"text": "My rules.\n"}}}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")
    (vault / ap.STARTING_POINTS_PATH).write_text(STARTING_POINTS.replace(
        '"user_claude_md", "folder_claude_md"', '"user_claude_md", "user_rules", "folder_claude_md"'), encoding="utf-8")

    first = path(PathStart(typical=2))
    assert first.error is None
    by_id = {p.id: p for p in first.pieces}
    # The rules file comes first; the missing user CLAUDE.md is nothing.
    assert ids(first)[:3] == ["file:C:/Users/me/.claude/rules/mine.md", "file:D:/work/repo/CLAUDE.md",
                              "file:D:/work/repo/CLAUDE.fork.md"]
    mine = by_id["file:C:/Users/me/.claude/rules/mine.md"]
    assert mine.every_time == rules and mine.text == "My rules.\n" and "file:C:/Users/me/.claude/CLAUDE.md" not in by_id
    assert not first.structure.broken_pointers
    # The import in the folder loads; the one outside it is a fact, and the role note it names isn't loaded by it.
    assert by_id["file:D:/work/repo/CLAUDE.fork.md"].every_time == "imported by file:D:/work/repo/CLAUDE.md"
    assert first.structure.skipped_imports == [{
        "from": "file:D:/work/repo/CLAUDE.md", "import": "@D:/vault/_/agent/roles/Coordinator.md",
        "target": "note:_/agent/roles/Coordinator.md", "why": "outside the session's folder"}]
    assert "note:_/agent/roles/Coordinator.md" not in by_id
    # The home folder (a Claude Code start with no folder) gets the rules file too.
    home = path(PathStart(surface="claude-code"))
    assert ids(home)[0] == "file:C:/Users/me/.claude/rules/mine.md" and not home.structure.skipped_imports
    # A backward question carries the skipped import too.
    reached = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, None))
    assert reached.structure.skipped_imports and reached.structure.skipped_imports[0]["import"].endswith(
        "Coordinator.md")


def test_an_import_the_snapshot_warns_about_stays_on_the_path_and_its_warning_is_a_structural_fact(vault):
    # Two imports on consecutive lines both failed to load in tests (Q134): the snapshot marks each with a
    # `warning`, still listing it loaded; the tool keeps the file on the path and reports the warning.
    warn = "on a line next to another import: put a blank line between (Q134)"
    user = "file:C:/Users/me/.claude/CLAUDE.md"
    loads = [
        {"id": user, "exists": True, "arrives": "loaded at the start by Claude Code (user CLAUDE.md)"},
        {"id": "note:_/agent/Common-rules.md", "exists": True, "arrives": f"imported by {user}", "warning": warn},
        {"id": "note:_/agent/roles/Worker.md", "exists": True, "arrives": f"imported by {user}", "warning": warn}]
    snap = {**SNAPSHOT, "format": 2, "folders": {
        "C:/Users/me": {"loads": loads, "skipped_imports": [], "session_start": []},
        "D:/work/repo": {**SNAPSHOT["folders"]["D:/work/repo"], "loads": loads, "skipped_imports": []}},
        "files": {user: {"text": "@D:/vault/_/agent/Common-rules.md\n@D:/vault/_/agent/roles/Worker.md\n"}}}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")

    home = path(PathStart(surface="claude-code"))
    assert home.error is None
    assert ids(home)[:2] == [user, "note:_/agent/Common-rules.md"] and "note:_/agent/roles/Worker.md" in ids(home)
    # The piece itself says it may not load, not only the structure.
    assert {p.id: p.every_time for p in home.pieces}["note:_/agent/Common-rules.md"] == (
        f"imported by {user} (may not load: {warn})")
    assert home.structure.import_warnings == [
        {"from": user, "import": "@D:/vault/_/agent/Common-rules.md", "target": "note:_/agent/Common-rules.md",
         "warning": warn},
        {"from": user, "import": "@D:/vault/_/agent/roles/Worker.md", "target": "note:_/agent/roles/Worker.md",
         "warning": warn}]
    reached = asyncio.run(apt.agent_path_reached_by_impl("_/agent/Common-rules.md", None, None))
    # Each warned import once, however many of the paths reaching the place hold it.
    assert [w["target"] for w in reached.structure.import_warnings] == [
        "note:_/agent/Common-rules.md", "note:_/agent/roles/Worker.md"]


def test_a_snapshot_in_an_unknown_format_is_left_out_with_a_note(vault):
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps({**SNAPSHOT, "format": 3}), encoding="utf-8")
    result = path(PathStart(typical=2))
    assert result.machine is None and any("format 3" in n for n in result.notes)


def test_a_role_that_picks_nothing_on_the_path_is_said(vault):
    chat = path(PathStart(surface="claude-ai", role="coordinator"))
    assert ids(chat) == ids(path(PathStart(surface="claude-ai")))
    assert "role: coordinator changed nothing: no line on this path picks a role note." in chat.notes
    worker = path(PathStart(surface="claude-code", folder="D:/work/repo", role="worker"))
    assert not any("changed nothing" in n for n in worker.notes or [])


def test_a_folder_the_snapshot_does_not_hold_uses_the_one_above_and_says_so(vault):
    result = path(PathStart(surface="claude-code", folder="D:\\work\\repo\\sub"))
    assert "file:D:/work/repo/CLAUDE.md" in ids(result)
    assert any("holds only D:/work/repo" in line for line in result.structure.not_followed)


def test_texts_past_the_budget_are_withheld_and_come_with_the_next_call(vault, monkeypatch):
    monkeypatch.setattr(apt, "PAGE_CHARS", 25_000)
    first = path(PathStart(typical=2))
    assert len(first.model_dump_json()) <= 25_000
    withheld = [p for p in first.pieces if p.text_withheld]
    assert withheld and any("text_withheld" in n for n in first.notes)
    held = {p.id: p.hash for p in first.pieces if p.text is not None}
    second = path(PathStart(typical=2), known=held)
    got = {p.id for p in second.pieces if p.text is not None}
    assert got and got <= {p.id for p in withheld}


def test_a_text_no_result_can_carry_comes_in_parts_with_read_from(vault, monkeypatch):
    monkeypatch.setattr(apt, "PAGE_CHARS", 25_000)
    long_text = "Worker rules, é. " * 2_000
    (vault / "_/agent/roles/Worker.md").write_text(long_text, encoding="utf-8")
    first = path(PathStart(typical=2))
    worker = next(p for p in first.pieces if p.id == "note:_/agent/roles/Worker.md")
    assert worker.text_in_parts is True and worker.text is None and not worker.text_withheld
    assert any("text_in_parts" in n and "read_from" in n for n in first.notes)
    # Holding every other text, the next call still doesn't promise it.
    held = {p.id: p.hash for p in first.pieces if p.id != worker.id}
    again = path(PathStart(typical=2), known=held)
    assert [p.id for p in again.pieces if p.status == "new"] == [worker.id]
    assert not any("not in this result" in n for n in again.notes)
    # Part by part, each within the budget, until the whole text, whose hash is the piece's.
    got, offset, calls = "", 0, 0
    while offset is not None:
        part = asyncio.run(apt.agent_path_impl(PathStart(typical=2), None, None,
                                               ReadFrom(id=worker.id, offset=offset)))
        assert part.error is None and len(part.model_dump_json()) <= 25_000
        assert [p.id for p in part.pieces] == [worker.id] and part.structure is None
        (p,) = part.pieces
        assert p.text_offset == offset == len(got)
        got += p.text
        offset, calls = p.next_offset, calls + 1
    assert got == long_text and ap.digest(got) == worker.hash and calls > 1
    assert "last part" in part.notes[-1]
    # A piece not on the path, or an offset past the end, is refused.
    off = asyncio.run(apt.agent_path_impl(PathStart(typical=2), None, None, ReadFrom(id="note:Nope.md")))
    assert "not on this path" in off.error
    past = asyncio.run(apt.agent_path_impl(PathStart(typical=2), None, None,
                                           ReadFrom(id=worker.id, offset=len(long_text) + 1)))
    assert "past the end" in past.error


def test_a_note_in_a_private_folder_keeps_its_text(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "private_folder_names", ["* (private)"])
    (vault / "Areas/Repo/Repo.md").write_text(
        NOTES["Areas/Repo/Repo.md"].replace("before changing the build.", "before changing the build.\n"
                                            "- Read [[Areas/Repo/Secret (private)/Plan]] before a release."),
        encoding="utf-8")
    result = path(PathStart(typical=2))
    plan = next(p for p in result.pieces if p.id == "note:Areas/Repo/Secret (private)/Plan.md")
    assert plan.private is True and plan.text is None and plan.hash == ap.digest("Private plan.\n")


def test_agent_path_refuses_a_start_without_surface_or_typical_agent(vault):
    assert "start needs a surface" in path(PathStart(folder="D:/work/repo")).error
    assert "give it alone" in path(PathStart(typical=2, surface="claude-ai")).error
    assert "No typical agent 9" in path(PathStart(typical=9)).error


def test_agent_path_without_start_gives_the_overview(vault):
    result = path()
    assert [t.number for t in result.typical] == [1, 2]
    assert all(" pieces every time, plus up to " in t.every_time and t.every_time.endswith(" tool texts as tools are "
               "loaded") for t in result.typical)
    worker = result.typical[1]
    assert worker.pieces[0].startswith("1. file:C:/Users/me/.claude/CLAUDE.md")
    # The tool texts as one line, where they load: before the pieces waiting on a moment.
    tools_at = next(i for i, line in enumerate(worker.pieces) if line.startswith(f"{i + 1}. tool:obsidian-mcp:*"))
    assert "when: " not in worker.pieces[tools_at - 1] and "when: " in worker.pieces[-1]
    assert any("when: Before working on one of the owner's areas" in line for line in worker.pieces)
    # Nothing points to these on this vault (memory is read through its mirror's lines, not the note; Notes
    # kept is only mentioned).
    assert result.rule_files_no_path_reaches == [
        "note:_/agent/Memory reference.md", "note:_/agent/Notes kept.md",
        "note:_/agent/Where agent instructions go.md", "note:_/agent/roles/Coordinator.md",
        "note:_/agent/roles/Relay.md"]
    assert result.pieces is None
    # What "no path reaches" means, against not_followed, is said in the result itself.
    assert any(n.startswith("No path reaches") and "`not_followed` is different" in n for n in result.notes)
    nowhere = asyncio.run(apt.agent_path_reached_by_impl("Notes/Nowhere.md", None, None))
    assert nowhere.matched_by.startswith("nothing") and any(n.startswith("No path reaches") for n in nowhere.notes)
    reached = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, None))
    assert not any(n.startswith("No path reaches") for n in reached.notes or [])


def test_reached_by_names_the_agents_and_chains_reaching_a_place(vault):
    result = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, None))
    assert result.error is None and result.matched_by == "the place itself"
    (hit,) = result.reached_by
    # Grouped by run kind: the typical worker keeps its own name beside the per-folder runs.
    assert hit["agents"] == ["a Claude Code worker in a repo", "a Claude Code worker in any related folder"]
    assert hit["via"] == ["hook:SessionStart/area-start.py", "note:_/agent/roles/Worker.md"]
    assert "note:_/agent/roles/Worker.md" in {p.id for p in result.pieces}

    future = asyncio.run(apt.agent_path_reached_by_impl("Areas/Repo/Not yet.md", None, None))
    assert future.matched_by == "its area (Repo)" and future.reached_by


def test_reached_by_refuses_an_unknown_machine(vault):
    result = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, "NOPE"))
    assert "No snapshot from the machine 'NOPE'" in result.error and "PC" in result.error


def test_without_a_snapshot_the_pc_side_is_said_to_be_missing(vault):
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").unlink()
    result = path(PathStart(typical=2))
    assert result.machine is None and not any(i.startswith(("file:", "hook:")) for i in ids(result))
    assert any("no PC has written a snapshot" in line for line in result.structure.not_followed)


def test_between_rule_notes_only_the_conventions_pointer_shapes_are_followed(vault):
    # P259: a pointer's bullet or sentence opens with At the start, While, When or Before (anything before the
    # first word that isn't a letter ignored), or takes the guide index's shape; any other link is a mention.
    (vault / "_/agent/Common-rules.md").write_text(
        "Rules every agent follows.\n\n"
        "- When working on one of the owner's areas, read [[_/agent/Vault conventions|Vault conventions]].\n"
        "> **While** the setup is on trial, read [[_/agent/roles/Coordinator]].\n"
        "- [[_/agent/guides/coding-practices|coding-practices]]: any code work | see [[_/agent/Memory reference]].\n"
        "- **Landing.** When you merge, read [[_/agent/Where agent instructions go]].\n"
        "- **Roles**: when in doubt, read [[_/agent/roles/Worker]].\n"
        "- Read [[_/agent/Notes kept]] first.\n"
        "- Whenever stuck, read [[_/agent/roles/Relay]].\n"
        "- See [[Areas/Repo/Repo#For agents|Repo's rules]].\n"
        "- [[#Tasks]]: see [[_/agent/roles/Relay]].\n"
        "- The index's shape, as code: `[[_/agent/Memory reference]]: when`.\n"
        "```\n[[_/agent/Memory reference]]\n```\n"
        "- Old copies sit beside it, such as `Reference/Old/KDO~20261022-153000.md`.\n"
        "- The old list was in `_/agent/Renamed.md`.\n"
        "- Before filing, read `_/agent/Gone.md`.\n"
        "- See [[_/agent/Missing]] too.\n", encoding="utf-8")
    result = path(PathStart(surface="claude-ai"))
    by_id = {p.id: p for p in result.pieces}
    assert moments(by_id["note:_/agent/Vault conventions.md"])[0] == "When working on one of the owner's areas"
    coordinator = by_id["note:_/agent/roles/Coordinator.md"]
    assert moments(coordinator)[0] == "While the setup is on trial" and always(coordinator)
    # The index's moment, cut at a table cell, for its leading link only.
    assert moments(by_id["note:_/agent/guides/coding-practices.md"])[0] == "any code work"
    # A bold title that is a sentence of its own: the next sentence opens the pointer.
    assert moments(by_id["note:_/agent/Where agent instructions go.md"])[0] == "When you merge"
    # A bold label in front, a bare "read", another opening word, a second link on an index line, a rule section's
    # link in no shape: mentions, not on the path (round 5: no longer listed). A link written as code isn't a link.
    for gone in ("note:_/agent/roles/Worker.md", "note:_/agent/Notes kept.md", "note:_/agent/roles/Relay.md",
                 "note:_/agent/Memory reference.md", "section:Areas/Repo/Repo.md#For agents"):
        assert gone not in by_id
    # A path to nothing is reported, but for a Syncthing version's name (outside the vault).
    assert {b["target"] for b in result.structure.broken_pointers} == {
        "_/agent/Renamed.md", "_/agent/Gone.md", "_/agent/Missing"}
    # A hook's message isn't written under the convention: its plain "Read the vault note …" still points.
    assert "note:_/agent/roles/Worker.md" in ids(path(PathStart(typical=2)))


def test_a_backticked_path_naming_a_kind_of_note_reaches_every_match(vault):
    # P261, P262: a vault path with <placeholder> segments names a kind of note; every match is reached, with the
    # line's moment. When the sentence names a rule section, each match's section is the piece, and only notes
    # that have one count.
    (vault / "_/agent/Vault conventions.md").write_text(
        "Conventions.\n\n## Folder notes\n\n"
        "A folder note is `<folder>/<folder>.md`: one line per subfolder.\n"
        "Before filing into a folder or changing it, read the `## For agents` section of its folder note, "
        "`<folder>/<folder>.md`, if it has one. [P261]\n\n## Working on an area\n\n- Start with open_area.\n",
        encoding="utf-8")
    (vault / "_/agent/Common-rules.md").write_text(
        NOTES["_/agent/Common-rules.md"] + "- Before reaching another machine (SSH, a deploy), read its note, "
        "`Reference/Servers/<name>/<name>.md`: ask the owner first. [P262]\n", encoding="utf-8")
    for note, text in {"Inbox/Inbox.md": "Inbox.\n\n## For agents\n\n- File by date.\n",
                       "Log/Log.md": "Log, no rules.\n",
                       "Reference/Servers/box/box.md": "A box.\n\n## For agents\n\n- Restart freely.\n",
                       "Reference/Servers/pi/pi.md": "A Pi.\n",
                       "Reference/Servers/pi/old/old.md": "Not a machine note.\n"}.items():
        (vault / note).parent.mkdir(parents=True, exist_ok=True)
        (vault / note).write_text(text, encoding="utf-8")

    result = path(PathStart(surface="claude-ai"))
    by_id = {p.id: p for p in result.pieces}
    inbox = by_id["section:Inbox/Inbox.md#For agents"]
    assert moments(inbox) == ["Before filing into a folder or changing it"] and not always(inbox)
    eng = drawn(PathStart(surface="claude-ai"))
    assert eng[inbox.id].stage == ap.LATER and inbox.text == "## For agents\n\n- File by date."
    assert "matching `<folder>/<folder>.md`" in eng[inbox.id].arrives
    # Every folder note's For agents section, at any depth, the area's main note's too; a folder note without
    # one brings nothing, and the folder notes aren't read whole.
    assert {"section:Areas/Repo/Repo.md#For agents", "section:Reference/Servers/box/box.md#For agents"} <= set(by_id)
    assert not any(i.startswith(("note:Log/", "section:Log/", "note:Inbox/")) for i in by_id)
    # The machine notes: each read whole, with its rule sections, and nothing below a machine's own folder.
    for machine in ("box", "pi"):
        assert moments(by_id[f"note:Reference/Servers/{machine}/{machine}.md"]) == [
            "Before reaching another machine (SSH, a deploy)"]  # its whole condition, up to "read"
    assert "note:Reference/Servers/pi/old/old.md" not in by_id
    # The line that only says what a folder note is points to nothing: no match is on the path through it.

    reached = asyncio.run(apt.agent_path_reached_by_impl("Inbox/Inbox.md", None, None))
    # The note itself is on no path: its rule section is (test bench, 2026-11-19: "the place itself").
    assert reached.matched_by == "its rule sections (the note itself is on no path)"
    assert all(hit["via"][-1] == "section:Inbox/Inbox.md#For agents" for hit in reached.reached_by)
    assert {a for hit in reached.reached_by for a in hit["agents"]} >= {"a claude.ai chat",
                                                                       "a Claude Code worker in a repo"}
    machine = asyncio.run(apt.agent_path_reached_by_impl("Reference/Servers/pi/pi.md", None, None))
    assert machine.reached_by and all(hit["via"][-1] == "note:Reference/Servers/pi/pi.md"
                                      for hit in machine.reached_by)


def test_a_note_pattern_reads_placeholders_as_names():
    folder_note = ap.note_pattern("<folder>/<folder>.md")
    assert folder_note.fullmatch("Inbox/Inbox.md") and folder_note.fullmatch("Areas/My mind/My mind.md")
    assert not folder_note.fullmatch("Areas/My mind/Other.md") and not folder_note.fullmatch("Inbox.md")
    machine = ap.note_pattern("Reference/Servers/<name>/<name>.md")
    assert machine.fullmatch("reference/servers/Pi/pi.md") and not machine.fullmatch("Reference/Servers/a/b/b.md")
    assert ap.note_pattern("Log/<date> Current position.md").fullmatch("Log/2026-11-19 Current position.md")
    for not_a_pattern in ("_/agent/Common-rules.md", "@<vault path>/_/agent/Common-rules.md",
                          "~/.claude/<id>.keep.md", "D:/<vault>/x.md"):
        assert ap.note_pattern(not_a_pattern) is None


def test_what_hooks_say_at_other_events_is_on_claude_codes_paths(vault):
    when = "before a Bash command naming a rule note (refuses it)"
    snap = {**SNAPSHOT, "format": 2, "hook_messages": {
        "hook:PreToolUse/guard.py": {"event": "PreToolUse", "matcher": "Bash", "messages": [
            {"when": when, "text": "Refused: use edit_note. Read the vault note D:\\vault\\_\\agent\\roles\\Relay.md.",
             "hash": "aaaaaaaaaaaa"},
            {"when": "before a direct edit (refuses it)", "text": "Refused, again.", "hash": "bbbbbbbbbbbb"}]},
        "hook:UserPromptSubmit/keep.py": {"event": "UserPromptSubmit", "matcher": "", "messages": None,
                                          "why": "the script has no messages role"},
        "hook:PostToolUse/quiet.py": {"event": "PostToolUse", "matcher": "", "messages": []}}}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")

    result = path(PathStart(typical=2))
    by_id = {p.id: p for p in result.pieces}
    guard = by_id["hook:PreToolUse/guard.py#1"]
    eng = drawn(PathStart(typical=2))[guard.id]
    assert moments(guard) == [when] and not always(guard) and eng.stage == ap.LATER
    assert guard.where == "D:/vault/_/claude-code-hooks/guard.py" and eng.arrives.startswith(
        "PreToolUse hook (matcher Bash): ")
    assert guard.hash == ap.digest(guard.text) and "hook:PreToolUse/guard.py#2" in by_id
    assert not any(i.startswith(("hook:PostToolUse/", "hook:UserPromptSubmit/")) for i in by_id)
    # Not a rule file: what a message points to isn't followed from it.
    assert "note:_/agent/roles/Relay.md" not in by_id
    nf = result.structure.not_followed
    assert not any(line.startswith("hook messages at other events") for line in nf)
    assert "what hook:UserPromptSubmit/keep.py says: the script has no messages role" in nf
    # Only Claude Code runs hooks.
    assert not any(p.id.startswith("hook:") for p in path(PathStart(surface="claude-ai")).pieces)

    reached = asyncio.run(apt.agent_path_reached_by_impl("hook:PreToolUse/guard.py", None, None))
    assert reached.matched_by == "the place itself" and reached.reached_by
    assert all(hit["surface"] == "claude-code" for hit in reached.reached_by)


def test_the_voice_bridges_texts_reach_the_relay_and_the_sessions_on_the_bridge(vault):
    def entry(text):
        return {"text": text, "hash": ap.digest(text)}

    bridge = {"command": "claude-voice texts", "instructions": entry("The claude-voice bridge."),
              "guide": entry("The bridge's guide."), "tools": [{"name": "health", **entry("Health check.")},
                                                               {"name": "guide", **entry("The guide tool.")}],
              "notes": [{"when": "under a message the relay sends into a session", **entry("[From the bridge]")},
                        {"when": "about once an hour", **entry("[Name check: <session name>]")}]}
    started = STARTING_POINTS.replace('role = "worker"\n', 'started_by = "relay"\nrole = "worker"\n')
    (vault / ap.STARTING_POINTS_PATH).write_text(started + (
        '\n[[typical]]\nname = "the voice relay"\nsurface = "claude-ai"\nextras = ["relay_notes"]\n'
        '\n[[typical]]\nname = "a session changing the rules"\nsurface = "claude-code"\nfolder = "D:/work/repo"\n'
        'role = "worker"\n'), encoding="utf-8")
    snap = {**SNAPSHOT, "format": 2, "bridge": bridge}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")

    # The relay: the guide with its first bridge result, the rest when the tools load; no notes.
    relay = {p.id: p for p in path(PathStart(typical=3)).pieces}
    assert relay["guide:claude-voice"].every_time == "with the relay's first result from the bridge"
    assert relay["tool:claude-voice:health"].every_time == ap.STAGE_NAMES[ap.TOOLS]
    assert "instructions:claude-voice" in relay and not any(i.startswith("bridge:") for i in relay)
    # A session the relay started: the guide when it first calls the bridge, the notes at their moments.
    worker = path(PathStart(typical=2))
    by_id = {p.id: p for p in worker.pieces}
    assert moments(by_id["guide:claude-voice"]) == ["when it first calls the bridge"] and not always(by_id["guide:claude-voice"])
    note = by_id["bridge:claude-voice#2"]
    assert moments(note) == ["about once an hour"] and not always(note)
    assert drawn(PathStart(typical=2))[note.id].stage == ap.LATER
    assert not any("voice bridge" in line for line in worker.structure.not_followed)
    # A session the bridge didn't start: no bridge. A claude.ai chat has it among its connectors: the guide when
    # it first calls the bridge, the tools when they load, no notes (those come under relayed messages).
    assert not any("claude-voice" in i for i in ids(path(PathStart(typical=4))))
    chat = {p.id: p for p in path(PathStart(typical=1)).pieces}
    assert moments(chat["guide:claude-voice"]) == ["when it first calls the bridge"] and not always(chat["guide:claude-voice"])
    assert "tool:claude-voice:health" in chat and not any(i.startswith("bridge:") for i in chat)
    # The overview folds each server's tool texts into one line.
    over = path()
    relay_lines = next(t for t in over.typical if t.number == 3).pieces
    assert any(line.split(". ", 1)[1].startswith("tool:claude-voice:* (3 texts") for line in relay_lines)
    reached = asyncio.run(apt.agent_path_reached_by_impl("bridge:claude-voice", None, None))
    assert {a for hit in reached.reached_by for a in hit["agents"]} == {"a Claude Code worker in a repo"}

    # The command failed on the PC, or the bridge isn't there: said, never guessed.
    for value, said in (({"command": "claude-voice texts", "error": "exit 1"}, "`claude-voice texts` failed on PC: exit 1"),
                        (None, "PC's snapshot says the bridge isn't on that PC")):
        (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps({**snap, "bridge": value}), encoding="utf-8")
        result = path(PathStart(typical=2))
        assert not any("claude-voice" in i for i in ids(result))
        assert any(said in line for line in result.structure.not_followed)


def test_what_comes_with_the_bridges_guide_besides_it_is_placed_where_it_arrives(vault):
    # P311: the note on the guide, the relay notes' opening and the line for one the bridge can't read were on no
    # path (`claude-voice texts` didn't print them).
    def entry(text, when=None):
        return {"text": text, "hash": ap.digest(text), **({"when": when} if when else {})}

    note_when = "with the guide, when the conversation's first call to the bridge isn't to guide"
    unread_when = "in place of one of the owner's notes for relays that the bridge can't read"
    bridge = {"command": "claude-voice texts", "instructions": entry("The claude-voice bridge."),
              "guide": entry("The bridge's guide."), "tools": [{"name": "health", **entry("Health check.")}],
              "notes": [{"when": "under a message the relay sends into a session", **entry("[From the bridge]")}],
              "relay": {"guide_note": entry("No need to call guide.", note_when),
                        "opening": entry("The owner's notes for you as the relay.", "before the owner's notes"),
                        "unread": entry("The bridge couldn't read this note (<path>).", unread_when)}}
    started = STARTING_POINTS.replace('role = "worker"\n', 'started_by = "relay"\nrole = "worker"\n')
    (vault / ap.STARTING_POINTS_PATH).write_text(started + (
        '\n[[typical]]\nname = "the voice relay"\nsurface = "claude-ai"\nextras = ["relay_notes"]\n'), encoding="utf-8")
    snap = {**SNAPSHOT, "format": 2, "bridge": bridge}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")

    # The relay: the opening before its notes, always; the note on the guide and the unread line at their moments.
    relay = path(PathStart(typical=3))
    by_id = {p.id: p for p in relay.pieces}
    order = [p.id for p in relay.pieces]
    opening = by_id["bridge:claude-voice#opening"]
    eng = drawn(PathStart(typical=3))
    assert eng[opening.id].stage == ap.BRIDGE and always(opening)
    assert order.index("bridge:claude-voice#opening") < order.index("note:_/agent/roles/Relay.md")
    for pid, when in (("bridge:claude-voice#guide_note", note_when), ("bridge:claude-voice#unread", unread_when)):
        assert eng[pid].stage == ap.BRIDGE and not always(by_id[pid])
        assert moments(by_id[pid]) == [when]
    # A session on the bridge and a claude.ai chat: the note on the guide waits on its moment, as the guide does;
    # the relay's own texts aren't theirs.
    for n in (2, 1):
        mine = {p.id: p for p in path(PathStart(typical=n)).pieces}
        assert moments(mine["bridge:claude-voice#guide_note"]) == [note_when]
        eng = drawn(PathStart(typical=n))
        assert eng["bridge:claude-voice#guide_note"].stage == eng["guide:claude-voice"].stage
        assert "bridge:claude-voice#opening" not in mine and "bridge:claude-voice#unread" not in mine
    # The backward question finds each one by its id.
    reached = asyncio.run(apt.agent_path_reached_by_impl("bridge:claude-voice#opening", None, None))
    assert {a for hit in reached.reached_by for a in hit["agents"]} == {"the voice relay"}
    # Without relay notes, no opening and no unread line; a snapshot without `relay` gives none of the three.
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps({**snap, "relay_notes": []}), encoding="utf-8")
    assert not {"bridge:claude-voice#opening", "bridge:claude-voice#unread"} & set(ids(path(PathStart(typical=3))))
    old = {**snap, "bridge": {k: v for k, v in bridge.items() if k != "relay"}}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(old), encoding="utf-8")
    assert not any(i.startswith("bridge:claude-voice#") and not i[-1].isdigit() for i in ids(path(PathStart(typical=3))))


def test_in_a_repo_the_tools_stop_at_what_claude_code_loads(vault):
    """P277: a repo's links to its own docs put nothing on the path, in a pointer shape or not, and count as no
    mention; a link into the vault is still a vault pointer. Every path says what is left out by design, in the
    words of the vault's paragraph, its ledger refs dropped."""
    claude_md = ("Repo rules.\n"
                 "- [parser.md](docs/parser.md): any change to the parser\n"
                 "- Before changing the build, read [the build notes](docs/build.md#steps).\n"
                 "- When unsure, read `AGENTS.md`.\n"
                 "- Read [design](docs/design.md) first.\n"
                 "- Before writing Python, read `D:/vault/_/agent/guides/coding-practices.md`.\n")
    snap = {**SNAPSHOT, "format": 2,
            "files": {**SNAPSHOT["files"], "file:D:/work/repo/CLAUDE.md": {"text": claude_md},
                      "file:D:/work/repo/AGENTS.md": {"text": "Agents.\n"}},
            # A snapshot from before P277 still carrying `docs`: ignored.
            "docs": {"file:D:/work/repo/docs/parser.md": {"path": "D:/work/repo/docs/parser.md", "hash": "a" * 12,
                                                          "chars": 10}}}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")
    plain = path(PathStart(typical=2))
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps({**snap, "files": SNAPSHOT["files"]}),
                                                         encoding="utf-8")
    result = plain
    repo = [i for i in ids(result) if i.startswith("file:D:/work/repo/")]
    assert repo == ["file:D:/work/repo/CLAUDE.md"]
    coding = next(p for p in result.pieces if p.id == "note:_/agent/guides/coding-practices.md")
    assert "file:D:/work/repo/CLAUDE.md" in [x.piece for x in coding.lines]


def test_what_is_left_out_by_design_is_said_once_in_both_descriptions(vault):
    # Round 5: it was a line of every path's not_followed, read from the vault's Where agent instructions go.
    assert apt.BY_DESIGN.startswith("What the path tools don't follow, by design:")
    assert apt.BY_DESIGN in apt.agent_path.__doc__ and apt.BY_DESIGN in apt.agent_path_reached_by.__doc__
    over = path()
    lines = [x for t in over.typical for x in t.structure.not_followed or []] + list(
        over.across_every_area.not_followed or []) + list(path(PathStart(typical=2)).structure.not_followed or [])
    assert not any("by design" in x for x in lines)


# ── reached_by_no_path: a rule write no path reaches ─────────────────


@pytest.fixture
def drawing(vault, monkeypatch):
    """The reach map drawn in a thread of this process, again at once when asked."""
    monkeypatch.setattr(apt, "DRAW_IN_HELPER", False)
    monkeypatch.setattr(apt, "REDRAW_AFTER_SECONDS", 0.0)
    monkeypatch.setattr(apt, "_DRAWERS", {})
    return vault


def draw_now():
    """The vault's reach map, drawn now and put where a write finds it."""
    m = apt.draw_reach_map(None, asyncio.run(apt._served(None)))
    apt._drawer(None).map = m
    return m


@pytest.fixture
def writing(drawing, monkeypatch):
    """The vault above, its reach map drawn, written through the note tools with this vault's plugin loaded."""
    from src import plugins

    monkeypatch.setattr(plugins, "active", lambda: plugins.load(("kelno_vault",)))
    monkeypatch.setattr(tools.settings, "write_precondition_required", False)

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)
    draw_now()
    permission = current_permission.set("readwrite")
    yield drawing
    current_permission.reset(permission)


def reached(path_, places, created=False):
    return asyncio.run(apt.reached_after_write(None, path_, places, created))


def counting_draws(monkeypatch):
    drawn = []
    real = ap.run
    monkeypatch.setattr(ap, "run", lambda *a: drawn.append(1) or real(*a))
    return drawn


def test_the_reach_map_is_what_the_overview_counts_as_reached(drawing):
    m = draw_now()
    every = ap.every_rule_file(apt._setup(None, None, asyncio.run(apt._served(None))).vault)
    in_map = {p for p in every if p in m.registered or any(p in r.reached for r in m.runs)}
    assert in_map == set(every) - set(path().rule_files_no_path_reaches)


def test_a_write_is_answered_from_the_map_without_drawing(drawing, monkeypatch):
    draw_now()
    drawn = counting_draws(monkeypatch)
    assert reached("_/agent/Common-rules.md", ["note:_/agent/Common-rules.md"]) == {"note:_/agent/Common-rules.md"}
    # A place matched whatever its case; a new note no path looked for is not reached.
    assert reached("_/agent/Common-rules.md", ["note:_/agent/common-RULES.md"]) == {"note:_/agent/common-RULES.md"}
    assert reached("_/agent/Orphan.md", ["note:_/agent/Orphan.md"], created=True) == set()
    assert reached("_/agent/Notes kept.md", ["note:_/agent/Notes kept.md"]) == set()
    assert drawn == []


def test_a_change_since_the_map_is_weighed_by_what_it_could_reach(drawing):
    (drawing / "_/agent/Orphan.md").write_text("- One.\n", encoding="utf-8")
    draw_now()
    # A note no path reads changed: the answers stand.
    (drawing / "Notes/Other.md").write_text("Mind [[_/agent/Orphan]].\n", encoding="utf-8")
    assert reached("_/agent/Orphan.md", ["note:_/agent/Orphan.md"]) == set()
    # A note on a path changed: a place reached by a chain that didn't change stays reached; one that wasn't
    # reached isn't guessed, as the changed note may now point to it, or to a note that does.
    common = drawing / "_/agent/Common-rules.md"
    common.write_text(common.read_text(encoding="utf-8") + "- When alone, read [[_/agent/Orphan]].\n",
                      encoding="utf-8")
    assert reached("_/agent/roles/Worker.md", ["note:_/agent/roles/Worker.md"]) == {"note:_/agent/roles/Worker.md"}
    with pytest.raises(apt.ReachUnknown, match="being drawn again"):
        reached("_/agent/Orphan.md", ["note:_/agent/Orphan.md"])
    # Drawn again, the map has it.
    draw_now()
    assert reached("_/agent/Orphan.md", ["note:_/agent/Orphan.md"]) == {"note:_/agent/Orphan.md"}


def test_a_new_note_a_path_looked_for_or_a_note_a_path_reads_is_not_guessed(drawing):
    common = drawing / "_/agent/Common-rules.md"
    common.write_text(common.read_text(encoding="utf-8") + "- When lost, read [[_/agent/Lost]].\n", encoding="utf-8")
    areas = drawing / ap.AREAS_NOTE
    areas.write_text(areas.read_text(encoding="utf-8") + "| New | | [[Areas/New/New\\|New]] |\n", encoding="utf-8")
    draw_now()
    for created in (True, False):  # a note the map didn't have is new, whether or not this write creates it
        with pytest.raises(apt.ReachUnknown, match="being drawn again"):
            reached("_/agent/Lost.md", ["note:_/agent/Lost.md"], created=created)
    # An area's main note the list of areas names, created after the map.
    with pytest.raises(apt.ReachUnknown, match="being drawn again"):
        reached("Areas/New/New.md", ["section:Areas/New/New.md#For agents"], created=True)
    # A rule section new in a note some path reads (the area's main note, opened for its sections).
    with pytest.raises(apt.ReachUnknown, match="being drawn again"):
        reached("Areas/Repo/Repo.md", ["section:Areas/Repo/Repo.md#Other rules"])
    # A change to what the setup reads: every path may change.
    (drawing / ap.STARTING_POINTS_PATH).write_text(STARTING_POINTS + "\n# changed\n", encoding="utf-8")
    with pytest.raises(apt.ReachUnknown, match="being drawn again"):
        reached("_/agent/Common-rules.md", ["note:_/agent/Common-rules.md"])


def test_a_snapshots_made_counts_only_when_several_compete(drawing):
    snap = drawing / f"{ap.SNAPSHOT_FOLDER}/PC.json"
    draw_now()
    snap.write_text(json.dumps({**SNAPSHOT, "made": "2026-11-19T10:00:00+01:00"}), encoding="utf-8")
    assert reached("_/agent/Common-rules.md", ["note:_/agent/Common-rules.md"])
    other = drawing / f"{ap.SNAPSHOT_FOLDER}/PC2.json"
    other.write_text(json.dumps({**SNAPSHOT, "machine": "PC2"}), encoding="utf-8")
    draw_now()
    other.write_text(json.dumps({**SNAPSHOT, "machine": "PC2", "made": "2026-11-20T10:00:00+01:00"}),
                     encoding="utf-8")
    with pytest.raises(apt.ReachUnknown, match="being drawn again"):
        reached("_/agent/Common-rules.md", ["note:_/agent/Common-rules.md"])


async def test_the_map_is_drawn_first_then_again_after_a_write_to_a_note_on_a_path(drawing):
    d = apt._drawer(None)
    with pytest.raises(apt.ReachUnknown, match="for the first time since the server started"):
        await apt.reached_after_write(None, "_/agent/Common-rules.md", ["note:_/agent/Common-rules.md"])
    await d._task
    first = d.map
    assert first is not None
    assert await apt.reached_after_write(None, "_/agent/Orphan.md", ["note:_/agent/Orphan.md"], True) == set()
    assert d._task.done() and d.map is first  # a note no path reads: the map still holds
    await apt.reached_after_write(None, "_/agent/Common-rules.md", ["note:_/agent/Common-rules.md"])
    await d._task
    assert d.map is not first
    # A write with no rule place to a note the map read draws it again too.
    second = d.map
    apt.warm_reach_map(None, "Areas/Repo/Repo.md")
    await d._task
    assert d.map is not second
    # Requests while a draw waits or runs make one more draw, not one each.
    drawn = []
    real = apt.update_reach_map
    apt.update_reach_map, restore = (lambda *a: drawn.append(1) or real(*a)), real
    try:
        for _ in range(3):
            d.request(None)
        await d._task
    finally:
        apt.update_reach_map = restore
    assert drawn == [1]


async def test_a_map_that_cannot_be_drawn_says_why(drawing):
    (drawing / f"{ap.SNAPSHOT_FOLDER}/PC.json").unlink()
    assert "no PC has written a snapshot" in apt.draw_reach_map(None, await apt._served(None))
    d = apt._drawer(None)
    with pytest.raises(apt.ReachUnknown):
        await apt.reached_after_write(None, "_/agent/Common-rules.md", ["note:_/agent/Common-rules.md"])
    await d._task
    with pytest.raises(apt.ReachUnknown, match="no PC has written a snapshot"):
        await apt.reached_after_write(None, "_/agent/Common-rules.md", ["note:_/agent/Common-rules.md"])


def test_the_helper_process_draws_the_same_map(vault, monkeypatch):
    from tests.conftest import CONTROLLED_TEST_ENV

    for key, value in CONTROLLED_TEST_ENV.items():
        monkeypatch.setenv(key, value)
    served = asyncio.run(apt._served(None))
    here = apt.draw_reach_map(None, served)
    there = apt._helper().submit(apt._update_in_helper, str(vault), served).result(timeout=300)
    assert [r.reached for r in there.runs] == [r.reached for r in here.runs]
    assert there.inputs == here.inputs and there.registered == here.registered


def test_a_rule_write_no_path_reaches_says_so_and_how_to_tell_why(writing):
    created = asyncio.run(tools.create_note_impl("_/agent/Orphan.md", "- A rule. [P1]\n"))
    assert (writing / "_/agent/Orphan.md").exists()
    assert "reached_by_no_path: after this write, no agent path reaches these rules" in created
    block = created.split("reached_by_no_path:")[1]
    assert "\n- note:_/agent/Orphan.md\n" in block
    assert "either way it needs fixing" in block and "To tell which, look for what is meant to send agents" in block
    assert '"How to write an instruction" (`_/agent/Where agent instructions go.md`)' in block
    assert '"Checking a rule against agent paths" in `_/agent/Where agent instructions go.md` says' in block
    assert "isn't yours to make in this session, tell the owner." in block
    # A rule some path reaches says nothing; nor does a write changing no rule line.
    common = asyncio.run(tools.edit_note_impl("_/agent/Common-rules.md", "- A new line. [P2]", append=True))
    assert "reached_by_no_path" not in common
    plain = asyncio.run(tools.create_note_impl("Notes/Plain.md", "- Not a rule.\n"))
    assert "reached_by_no_path" not in plain


def test_a_rule_section_is_checked_by_its_own_place(writing):
    # The area's For agents section is reached (opening the area serves it); the same section in a note
    # nothing points to isn't, and the note's other lines don't count.
    area = asyncio.run(tools.edit_note_impl("Areas/Repo/Repo.md", "\n- Mind the build. [P3]\n",
                                            section="For agents"))
    assert "reached_by_no_path" not in area
    draw_now()  # as the background does after a write to a note on a path
    lonely = asyncio.run(tools.create_note_impl("Notes/Lonely.md", "Text.\n"))
    outside = asyncio.run(tools.edit_note_impl("Notes/Lonely.md", "More text.\n", append=True))
    assert "reached_by_no_path" not in lonely + outside
    added = asyncio.run(tools.edit_note_impl("Notes/Lonely.md", "\n## For agents\n\n- Be kind. [P3]\n",
                                             append=True))
    assert "\n- section:Notes/Lonely.md#For agents\n" in added.split("reached_by_no_path:")[1]
    # In a note some path reads (a rule line links it), a new rule section isn't guessed.
    linked = asyncio.run(tools.edit_note_impl("Notes/Some note.md", "\n## For agents\n\n- Be kind. [P3]\n",
                                              append=True))
    assert "reached_by_no_path: not checked (the agent paths are being drawn again" in linked
    # A heading the path tools don't read as a rule section (case, a closing `#`) is no place of theirs.
    for heading in ("## For Agents", "## For agents ##"):
        odd = asyncio.run(tools.create_note_impl(f"Notes/Odd {len(heading)}.md", f"{heading}\n\n- Be kind. [P3]\n"))
        assert "reached_by_no_path" not in odd, heading


def test_a_rule_write_says_in_one_line_when_reach_cannot_be_checked(writing):
    (writing / ap.STARTING_POINTS_PATH).unlink()
    result = asyncio.run(tools.edit_note_impl("_/agent/Common-rules.md", "- A new line. [P2]", append=True))
    line = result.split("reached_by_no_path:")[1].split("\n\n")[0]
    assert line.startswith(" not checked (the agent paths are being drawn again") and "\n" not in line
    assert line.endswith("`agent_path_reached_by` on each changed place tells whether an agent path reaches it.")



def _all_answers():
    """Every kind of call, as JSON: the overview, each typical agent, a work path, two backward questions."""
    calls = [path(), path(PathStart(typical=1)), path(PathStart(typical=2)),
             path(PathStart(surface="claude-ai"), PathWork(area="Repo")),
             asyncio.run(apt.agent_path_reached_by_impl("_/agent/Common-rules.md", None, None)),
             asyncio.run(apt.agent_path_reached_by_impl("Areas/Repo/Repo.md#For agents", None, None))]
    return [c.model_dump_json() for c in calls]


def _same_as_fresh():
    """The kept drawing's answers equal a fresh drawing's; the kept drawing then goes on, so a change seen only
    over two steps shows too."""
    kept = _all_answers()
    again = _all_answers()  # a second call on the kept drawing changes nothing
    saved, sections = dict(apt._KEPT), {k: dict(v) for k, v in apt._SECTIONS.items()}
    apt._KEPT.clear()
    apt._SECTIONS.clear()
    fresh = _all_answers()
    apt._KEPT.clear()
    apt._KEPT.update(saved)
    apt._SECTIONS.clear()
    apt._SECTIONS.update(sections)
    assert kept == again
    assert kept == fresh


def _write_like_syncthing(p, text):
    """Syncthing's write: a temp file renamed into place, the source's mtime set on it."""
    st = p.stat()
    tmp = p.with_name(".syncthing." + p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, p)


def test_the_kept_drawing_follows_each_change_as_a_fresh_drawing_would(vault, monkeypatch):
    drawn = counting_draws(monkeypatch)
    _same_as_fresh()
    # A note no path reads: nothing is drawn again.
    _all_answers()
    before = len(drawn)
    (vault / "Notes/Unrelated.md").write_text("Nothing here.\n", encoding="utf-8")
    _all_answers()
    assert len(drawn) == before
    # A rule note on every path gains a pointer to a note that doesn't exist yet, then does.
    common = vault / "_/agent/Common-rules.md"
    common.write_text(common.read_text(encoding="utf-8") + "- When stuck, read [[_/agent/Stuck]].\n", encoding="utf-8")
    _same_as_fresh()
    (vault / "_/agent/Stuck.md").write_text("Stuck rules.\n", encoding="utf-8")
    _same_as_fresh()
    # A note some path reads, rewritten as Syncthing does, with its old mtime and the same size.
    _write_like_syncthing(vault / "Notes/Some note.md", "Just a nope.\n")
    _same_as_fresh()
    # The area's main note gains a pointer; a rule section added to a note only the overview's list reads.
    area = vault / "Areas/Repo/Repo.md"
    area.write_text(area.read_text(encoding="utf-8").replace(
        "## Current position", "- When building, read [[_/agent/Stuck]].\n\n## Current position"), encoding="utf-8")
    _same_as_fresh()
    (vault / "Notes/Unrelated.md").write_text("Text.\n\n## For agents\n\n- Be kind.\n", encoding="utf-8")
    _same_as_fresh()
    # A note removed, a folder renamed.
    (vault / "_/agent/Stuck.md").unlink()
    _same_as_fresh()
    (vault / "Notes").rename(vault / "Notes moved")
    _same_as_fresh()
    # The snapshot: its date alone, then its content.
    snap = vault / f"{ap.SNAPSHOT_FOLDER}/PC.json"
    snap.write_text(json.dumps({**SNAPSHOT, "made": "2026-11-20T10:00:00+01:00"}), encoding="utf-8")
    assert json.loads(_all_answers()[1])["snapshot_made"] == "2026-11-20T10:00:00+01:00"
    _same_as_fresh()
    snap.write_text(json.dumps({**SNAPSHOT, "relay_notes": []}), encoding="utf-8")
    _same_as_fresh()
    # The list of areas.
    areas = vault / ap.AREAS_NOTE
    areas.write_text(areas.read_text(encoding="utf-8") + "| New | | [[Areas/New/New\\|New]] |\n", encoding="utf-8")
    _same_as_fresh()


def test_a_note_read_for_one_section_stays_watched_after_an_edit_elsewhere(vault):
    """Adversarial review, 2026-11-19: an edit outside the section kept the note unwatched, so a later edit of the
    section was missed, by the tools and by the write check."""
    common = vault / "_/agent/Common-rules.md"
    common.write_text(common.read_text(encoding="utf-8") + "- Before reaching a machine, read the `## For agents` "
                      "section of `Reference/Servers/<name>/<name>.md`.\n", encoding="utf-8")
    (vault / "_/agent/Deploy.md").write_text("Deploy rules.\n", encoding="utf-8")
    m1 = vault / "Reference/Servers/M1/M1.md"
    m1.parent.mkdir(parents=True)
    m1.write_text("Machine.\n\n## For agents\n\n- When deploying, read [[_/agent/Deploy]].\n", encoding="utf-8")
    _same_as_fresh()
    m1.write_text("Machine, edited.\n\n## For agents\n\n- When deploying, read [[_/agent/Deploy]].\n", encoding="utf-8")
    _same_as_fresh()
    m = apt.update_reach_map(None, asyncio.run(apt._served(None)))
    assert "Reference/Servers/M1/M1.md" in m.inputs
    m1.write_text("Machine, edited.\n\n## For agents\n\n- Be careful.\n", encoding="utf-8")
    with pytest.raises(apt.ReachUnknown):
        apt._answer(None, m, "_/agent/Deploy.md", False, ["note:_/agent/Deploy.md"])
    _same_as_fresh()
    assert "note:_/agent/Deploy.md" not in _all_answers()[1]


def test_a_touched_malformed_snapshot_beside_a_good_one_is_no_error(vault):
    bad = vault / f"{ap.SNAPSHOT_FOLDER}/Bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert path().error is None
    st = bad.stat()
    os.utime(bad, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    _same_as_fresh()


def test_an_update_draws_again_only_the_paths_a_change_touches(drawing):
    served = asyncio.run(apt._served(None))
    first = apt.update_reach_map(None, served)
    assert first.redrawn == len(first.runs)
    assert apt.update_reach_map(None, served).redrawn == 0
    area = drawing / "Areas/Repo/Repo.md"
    area.write_text(area.read_text(encoding="utf-8") + "\nMore.\n", encoding="utf-8")
    m = apt.update_reach_map(None, served)
    assert 0 < m.redrawn < len(m.runs)


# --- The fix batch of 2026-11-19 (the test bench's cold reads, "Open points" in the vault note) -------------


def _write(vault, rel, text):
    (vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (vault / rel).write_text(text, encoding="utf-8")


def _snapshot(vault, **changes):
    _write(vault, f"{ap.SNAPSHOT_FOLDER}/PC.json", json.dumps({**SNAPSHOT, **changes}))


def reached_by(place, known=None):
    return asyncio.run(apt.agent_path_reached_by_impl(place, known, None))


def test_work_area_takes_the_slug_or_tag_open_area_takes(vault):
    _write(vault, "Areas/Repo/Repo.md", NOTES["Areas/Repo/Repo.md"].replace(
        "---\ncode_project: true\n", "---\ncode_project: true\narea_tag: area/repo-work\n"))
    for name in ("repo-work", "#area/repo-work", "area/repo-work", "Repo"):
        result = path(PathStart(surface="claude-ai"), PathWork(area=name))
        assert "section:Areas/Repo/Repo.md#For agents" in ids(result), name
        assert not result.structure.broken_pointers, name
    nope = path(PathStart(surface="claude-ai"), PathWork(area="no-such-area"))
    assert nope.structure.broken_pointers[0]["target"] == "no-such-area"


def test_a_folder_the_snapshot_holds_nothing_of_still_loads_what_every_folder_loads(vault):
    result = path(PathStart(surface="claude-code", folder="Z:/elsewhere", role="worker"))
    order = ids(result)
    assert order[:2] == ["file:C:/Users/me/.claude/CLAUDE.md", "note:_/agent/Common-rules.md"]
    # The hooks are the user's: they run there too (`test_a_start_in_a_folder_the_snapshot_doesnt_hold_...`).
    assert "hook:SessionStart/area-start.py" in order
    assert any(line.startswith("the CLAUDE.md files of Z:/elsewhere: ") for line in result.structure.not_followed)


def test_known_is_kept_by_piece_id_not_by_text(vault):
    # Two pieces with the same text, both on the worker's path: holding one doesn't hold the other.
    _write(vault, "_/agent/guides/coding-practices.md", NOTES["_/agent/Vault conventions.md"])
    first = path(PathStart(typical=2))
    by_id = {p.id: p for p in first.pieces}
    code, conv = by_id["note:_/agent/guides/coding-practices.md"], by_id["note:_/agent/Vault conventions.md"]
    assert code.hash == conv.hash
    one = path(PathStart(typical=2), known={code.id: code.hash})
    assert [p.status for p in one.pieces if p.id == conv.id] == ["new"]
    assert code.id not in ids(one)
    both = path(PathStart(typical=2), known={code.id: code.hash, conv.id: conv.hash})
    assert conv.id not in ids(both) and code.id not in ids(both)


def test_without_a_role_each_role_note_waits_on_its_role(vault):
    result = path(PathStart(surface="claude-code", folder="D:/work/repo"))
    by_id = {p.id: p for p in result.pieces}
    worker, coordinator = by_id["note:_/agent/roles/Worker.md"], by_id["note:_/agent/roles/Coordinator.md"]
    assert not always(worker) and moments(worker) == ["if its role is Worker"]
    assert not always(coordinator) and moments(coordinator) == ["if its role is Coordinator"]
    assert any(n.startswith("No role given") for n in result.notes)
    picked = path(PathStart(surface="claude-code", folder="D:/work/repo", role="worker"))
    assert always({p.id: p for p in picked.pieces}["note:_/agent/roles/Worker.md"])
    assert not any(n.startswith("No role given") for n in picked.notes or [])


def test_the_arborist_role_picks_its_own_note_from_the_hooks_line(vault):
    # The hook's line since 2026-11-21 names three role notes (P328): the Arborist picks its own, like the Coordinator.
    _write(vault, "_/agent/roles/Arborist.md", "Arborist rules.\n")
    folders = json.loads(json.dumps(SNAPSHOT["folders"]))
    folders["D:/work/repo"]["session_start"][0]["text"] = (
        "You work in the owner's setup. Read the vault note D:\\vault\\_\\agent\\roles\\Worker.md and follow it; if "
        "you are the Coordinator, read your own role note, D:\\vault\\_\\agent\\roles\\Coordinator.md, instead; if you "
        "are the Arborist, read yours, D:\\vault\\_\\agent\\roles\\Arborist.md, instead.")
    _snapshot(vault, folders=folders)
    (vault / ap.STARTING_POINTS_PATH).write_text(STARTING_POINTS + (
        '\n[[typical]]\nname = "the Arborist"\nsurface = "claude-code"\nfolder = "D:/work/repo"\n'
        'started_by = "coordinator"\nrole = "arborist"\n'), encoding="utf-8")
    arborist = {p.id: p for p in path(PathStart(typical=3)).pieces}
    assert always(arborist["note:_/agent/roles/Arborist.md"])
    assert "note:_/agent/roles/Worker.md" not in arborist and "note:_/agent/roles/Coordinator.md" not in arborist
    assert ids(path(PathStart(surface="claude-code", folder="D:/work/repo", role="arborist"))).count(
        "note:_/agent/roles/Arborist.md") == 1
    worker = {p.id: p for p in path(PathStart(typical=2)).pieces}
    assert always(worker["note:_/agent/roles/Worker.md"]) and "note:_/agent/roles/Arborist.md" not in worker
    unpicked = {p.id: p for p in path(PathStart(surface="claude-code", folder="D:/work/repo")).pieces}
    assert moments(unpicked["note:_/agent/roles/Arborist.md"]) == ["if its role is Arborist"]
    (hit,) = reached_by("_/agent/roles/Arborist.md").reached_by
    assert "the Arborist" in hit["agents"]


def test_a_note_read_as_soon_as_a_waiting_piece_points_to_it_waits_on_that_moment(vault):
    _write(vault, "_/agent/Vault conventions.md", NOTES["_/agent/Vault conventions.md"]
           + "\n## Details\n\nRead [[Notes/Some note]] for the details.\n")
    by_id = {p.id: p for p in path(PathStart(typical=2)).pieces}
    some = by_id["note:Notes/Some note.md"]
    assert not always(some) and moments(some) == ["Before working on one of the owner's areas"]
    # Its line is Vault conventions', which reads it as soon as it is met: the moment is Common-rules' line, through
    # Vault conventions (round 5: the result shows the line, the engine keeps the way through).
    assert [x.piece for x in some.lines] == ["note:_/agent/Vault conventions.md"]
    eng = drawn(PathStart(typical=2))
    assert eng[some.id].moments[0]["piece"] == "note:_/agent/Common-rules.md"
    assert eng[some.id].moments[0]["through"] == ["note:_/agent/Vault conventions.md"]
    # The connector's guide is on every path: every agent on the connector calls get_vault_guide first.
    assert "calls first" in by_id["guide:obsidian-mcp"].every_time


def test_a_hook_message_holding_a_rule_sections_lines_brings_that_section_at_the_start(vault):
    _write(vault, "Reference/Servers/PC/PC.md", "PC.\n\n## For agents\n\n- One suite at a time.\n")
    folders = json.loads(json.dumps(SNAPSHOT["folders"]))
    hook = folders["D:/work/repo"]["session_start"][0]
    hook["text"] += ("\n\nThis session runs on PC. The rules for working on it, from the `## For agents` section of "
                     "D:\\vault\\Reference\\Servers\\PC\\PC.md:\n- One suite at a time.")
    _snapshot(vault, folders=folders)
    by_id = {p.id: p for p in path(PathStart(typical=2)).pieces}
    rules = by_id["section:Reference/Servers/PC/PC.md#For agents"]
    assert always(rules) and drawn(PathStart(typical=2))[rules.id].stage == ap.START_READ
    assert rules.every_time == "copied into hook:SessionStart/area-start.py's message"
    _write(vault, "Reference/Servers/PC/PC.md", "PC.\n\n## For agents\n\n- Two suites at a time.\n")
    by_id = {p.id: p for p in path(PathStart(typical=2)).pieces}
    assert by_id["section:Reference/Servers/PC/PC.md#For agents"].every_time.endswith(
        "(the copy differs from the section now)")


def test_a_backward_question_with_known_says_how_to_get_who_reaches_the_place(vault):
    again = reached_by("_/agent/roles/Worker.md", known={"note:_/agent/roles/Worker.md": "0123456789ab"})
    assert again.reached_by is None
    assert any("call once without it if you haven't had them" in n for n in again.notes)


def test_pieces_come_in_the_order_of_their_stages(vault):
    result = path(PathStart(typical=2))
    eng = drawn(PathStart(typical=2))
    stages = [eng[p.id].stage for p in result.pieces]
    assert stages == sorted(stages)
    # The area's pieces come before what waits on a moment (they were taken in after it).
    order = ids(result)
    assert order.index("open_area:Areas/Repo/Repo.md") < order.index("note:_/agent/Vault conventions.md")


def test_memorys_mirrored_lines_are_followed_only_where_memory_loads(vault):
    _write(vault, "_/agent/Common-rules.md", NOTES["_/agent/Common-rules.md"]
           + "- Before saving anything to Claude's memory, read [[_/agent/Memory reference]].\n")
    _write(vault, "_/agent/Memory reference.md", NOTES["_/agent/Memory reference.md"]
           + "- At the start of every chat, read [[_/agent/Chat only]].\n")
    _write(vault, "_/agent/Chat only.md", "For chats.\n")
    worker = ids(path(PathStart(typical=2)))
    assert "note:_/agent/Memory reference.md" in worker and "note:_/agent/Chat only.md" not in worker
    assert "note:_/agent/Chat only.md" in ids(path(PathStart(surface="claude-ai")))


def test_a_backward_question_keeps_the_places_own_id_in_its_chain(vault):
    result = reached_by("Areas/Repo/Repo.md#For agents")
    assert result.matched_by == "the place itself"
    assert all(hit["via"][-1] == "section:Areas/Repo/Repo.md#For agents" for hit in result.reached_by)


def test_a_bare_hook_id_is_reached_through_each_of_its_messages(vault):
    _snapshot(vault, format=2, hook_messages={"hook:Stop/check.py": {"event": "Stop", "matcher": "", "messages": [
        {"when": "at the end of a turn", "text": "First.", "hash": "a"},
        {"when": "at the end of the first turn", "text": "Second.", "hash": "b"}]}})
    result = reached_by("hook:Stop/check.py")
    assert sorted(hit["via"][-1] for hit in result.reached_by) == ["hook:Stop/check.py#1", "hook:Stop/check.py#2"]
    assert [hit["via"][-1] for hit in reached_by("hook:Stop/check.py#2").reached_by] == ["hook:Stop/check.py#2"]


def test_what_a_backward_question_matched_says_whether_the_place_exists(vault):
    assert reached_by("Notes/Some note.md").matched_by == "nothing: it exists, and no path reaches it or its area"
    assert reached_by("Notes/Missing.md").matched_by == (
        "nothing: it doesn't exist yet, and no path reaches it or its area")
    assert reached_by("Areas/Repo/Repo.md#Current position").matched_by == "its note (it holds that section)"
    assert reached_by("Areas/Repo/Repo.md#Nope").matched_by == "its note (the section isn't in it yet)"


def test_the_main_note_as_open_area_serves_it_has_an_id_of_its_own(vault):
    by_id = {p.id: p for p in path(PathStart(typical=2)).pieces}
    served = by_id["open_area:Areas/Repo/Repo.md"]
    assert "note:Areas/Repo/Repo.md" not in by_id and "## For agents" not in served.text
    assert served.text != NOTES["Areas/Repo/Repo.md"]


def test_a_first_call_counts_its_withheld_texts_in_waiting(vault, monkeypatch):
    monkeypatch.setattr(apt, "PAGE_CHARS", 25_000)
    first = path(PathStart(typical=2))
    withheld = sum(1 for p in first.pieces if p.text_withheld)
    assert withheld and first.waiting == withheld
    assert any(n.startswith(f"{withheld} texts are not in this result") for n in first.notes)
    full = path(PathStart(surface="claude-ai"))
    assert full.waiting is None or full.waiting == sum(1 for p in full.pieces if p.text_withheld)


def test_the_result_names_a_typical_agent_as_the_call_does(vault):
    result = path(PathStart(typical=2))
    assert result.start["typical"] == 2 and "number" not in result.start
    # Round 5: a waiting piece's line says `when:`, as its field does (it said `moment:` while the field was named so).
    lines = [line for t in path().typical for line in t.pieces]
    assert any(" when: " in line for line in lines) and not any(" moment: " in line for line in lines)


def test_a_structure_lists_faults_only_when_there_are_any(vault):
    # Round 5: an empty list, and into_areas and the mention lists, are gone.
    st = json.loads(path(PathStart(typical=2)).model_dump_json())["structure"]
    assert set(st) <= {"size", "not_followed"} and st["not_followed"]
    across = json.loads(path().model_dump_json())["across_every_area"]
    assert set(across) <= {"pointers_outside_rule_files"}



def test_a_backward_question_reached_through_one_area_shows_that_area(vault):
    for hit in reached_by("Areas/Repo/Not yet.md").reached_by:
        assert "<the area" not in json.dumps(hit), hit
        assert hit["every_time"].startswith("served by open_area on Repo (")


def test_a_claude_md_below_the_start_folder_loads_when_the_session_works_there(vault):
    folders = json.loads(json.dumps(SNAPSHOT["folders"]))
    folders["D:/work/repo/sub"] = {"loads": folders["D:/work/repo"]["loads"] + [
        {"id": "file:D:/work/repo/sub/CLAUDE.md", "exists": True,
         "arrives": "loaded at the start by Claude Code (D:/work/repo/sub)"}], "session_start": []}
    files = {**SNAPSHOT["files"], "file:D:/work/repo/sub/CLAUDE.md": {"text": "Sub rules.\n@more.md\n"},
             "file:D:/work/repo/sub/more.md": {"text": "More sub rules.\n"}}
    _snapshot(vault, folders=folders, files=files)
    by_id = {p.id: p for p in path(PathStart(typical=2)).pieces}
    sub = by_id["file:D:/work/repo/sub/CLAUDE.md"]
    assert not always(sub) and moments(sub) == ["when it reads a file in D:/work/repo/sub"]
    assert drawn(PathStart(typical=2))[sub.id].arrives == "loaded by Claude Code when it reads a file in D:/work/repo/sub"
    more = by_id["file:D:/work/repo/sub/more.md"]  # its imports are expanded, waiting on the same moment
    assert not always(more) and moments(more) == moments(sub)
    assert always(by_id["file:D:/work/repo/CLAUDE.md"])  # the start folder's own is loaded at the start


def test_a_rule_section_listed_after_its_note_read_whole_says_so(vault):
    _write(vault, "_/agent/Common-rules.md", NOTES["_/agent/Common-rules.md"]
           + "- When the build breaks, read [[Areas/Repo/Repo notes]].\n")
    _write(vault, "Areas/Repo/Repo notes.md", "How the build works.\n\n## For agents\n\n- Ask first.\n")
    by_id = {p.id: p for p in path(PathStart(typical=2)).pieces}
    assert "note:Areas/Repo/Repo notes.md" in by_id
    assert by_id["section:Areas/Repo/Repo notes.md#For agents"].every_time.endswith(
        "(its text is in note:Areas/Repo/Repo notes.md, read whole earlier on this path)")


# --- Every moment that leads to a piece (owner, 2026-11-19: "Yes with option 1") ----------------------------------


def test_a_piece_shows_every_moment_leading_to_it_each_with_its_line(vault):
    _write(vault, "_/agent/Common-rules.md", NOTES["_/agent/Common-rules.md"]
           + "- When choosing it, read [[_/agent/Vault conventions]] for the names.\n")
    result = path(PathStart(typical=2))
    conv = {p.id: p for p in result.pieces}["note:_/agent/Vault conventions.md"]
    # In the order the agent meets them, each with the piece holding the line; no route picked.
    assert moments(conv) == ["Before working on one of the owner's areas", "When choosing it"]
    assert {x.piece for x in conv.lines} == {"note:_/agent/Common-rules.md"}
    first, second = conv.lines
    assert first.line.startswith("- Before working on one of the owner's areas, read")
    # A moment that doesn't stand alone comes with its whole line.
    assert second.line == "- When choosing it, read [[_/agent/Vault conventions]] for the names."
    # The short form (a piece held, or one whose text comes later) keeps only that line.
    held = path(PathStart(typical=2), known={p.id: p.hash for p in result.pieces})
    assert held.pieces == [] or all(p.id != conv.id for p in held.pieces)
    short = apt._no_lines(conv.lines)
    assert short[0].line is None and short[1].line == second.line


def test_whether_a_moment_stands_alone():
    for moment in ("When in voice", "Before working on one of the owner's areas", "When the owner talks about their areas",
                   "any session in a fork (such as claude-voice)", "At the start"):
        assert ap.stands_alone(moment), moment
    for moment in ("When choosing it", "When starting one", "When working in this area", "When writing its text",
                   "Before doing that", "When it's a rule change"):
        assert not ap.stands_alone(moment), moment


def test_a_pointer_in_a_waiting_note_leads_with_that_notes_moment(vault):
    # "At the start" in a note read only "When in voice" holds from when the note is read: its moment is the
    # voice line's, through the Relay note (test bench, 2026-11-19: it showed "At the start" alone).
    _write(vault, "_/agent/Common-rules.md", NOTES["_/agent/Common-rules.md"]
           + "- When in voice, read [[_/agent/roles/Relay]].\n")
    _write(vault, "_/agent/roles/Relay.md", "Relay rules.\n\n- At the start, read [[_/agent/Notes kept]].\n")
    kept = {p.id: p for p in path(PathStart(surface="claude-ai")).pieces}["note:_/agent/Notes kept.md"]
    assert not always(kept) and moments(kept) == ["When in voice"]
    assert [x.piece for x in kept.lines] == ["note:_/agent/roles/Relay.md"]
    eng = drawn(PathStart(surface="claude-ai"))["note:_/agent/Notes kept.md"]
    assert eng.moments[0]["piece"] == "note:_/agent/Common-rules.md"
    assert eng.moments[0]["through"] == ["note:_/agent/roles/Relay.md"]
    over = path()
    assert any("note:_/agent/Notes kept.md" in line and "when: When in voice" in line for line in over.typical[0].pieces)


def test_an_areas_for_agents_arrives_at_the_areas_opening_beside_the_folder_filing_route(vault):
    _write(vault, "_/agent/Vault conventions.md", NOTES["_/agent/Vault conventions.md"]
           + "\n## Folder notes\n\n- Before filing a note into a folder, read the `## For agents` section of its "
             "folder note, `<folder>/<folder>.md`.\n")
    result = path(PathStart(typical=2))
    section = {p.id: p for p in result.pieces}["section:Areas/Repo/Repo.md#For agents"]
    # Met first behind the filing rule (waiting), then at the area's opening: it arrives there, whatever comes.
    assert always(section) and drawn(PathStart(typical=2))[section.id].stage == ap.WORK
    assert section.every_time == "served by open_area on Repo (for_agents)"
    assert moments(section) == ["when the area is opened", "Before filing a note into a folder"]
    # What it points to is placed the same way: read as soon as it's met.
    assert always({p.id: p for p in result.pieces}["note:Areas/Repo/Repo notes.md"])
    # The backward question shows the area's own chain: the section, served by open_area.
    hits = reached_by("Areas/Repo/Repo.md#For agents").reached_by
    assert any(hit["via"] == ["section:Areas/Repo/Repo.md#For agents"]
               and hit["every_time"] == "served by open_area on Repo (for_agents)" for hit in hits)


def test_the_guide_brings_the_vaults_conventions_note_with_it(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_guide_note", "_/agent/Vault conventions.md")
    by_id = {p.id: p for p in path(PathStart(typical=2)).pieces}
    conv, guide = by_id["note:_/agent/Vault conventions.md"], by_id["guide:obsidian-mcp"]
    # On the path as the guide is: every agent on the connector calls get_vault_guide first.
    eng = drawn(PathStart(typical=2))
    assert always(conv) and always(guide) and eng[conv.id].stage == eng[guide.id].stage == ap.TOOLS
    assert conv.every_time.startswith("returned by get_vault_guide after the connector's own guide")
    # The arrival placing it first; the other route after.
    assert moments(conv) == ["when get_vault_guide is called", "Before working on one of the owner's areas"]
    assert conv.lines[0].piece == "tool:obsidian-mcp:get_vault_guide"
    # It counts as the vault note it is, not as a tool text.
    over = path().typical[1]
    assert any(line.split(". ", 1)[1].startswith("note:_/agent/Vault conventions.md") for line in over.pieces)


def test_a_start_in_a_folder_the_snapshot_doesnt_hold_still_gets_the_session_start_hooks(vault):
    folders = json.loads(json.dumps(SNAPSHOT["folders"]))
    everywhere = folders["D:/work/repo"]["session_start"][0]["text"]
    folders["C:/Users/me"]["session_start"] = [{"id": "hook:SessionStart/area-start.py", "matcher": "startup",
                                                "hash": "h", "text": everywhere}]
    folders["D:/work/repo"]["session_start"][0]["text"] = (
        everywhere + "\n\nThis session runs in D:\\work\\repo, outside the vault: open_area on D:\\work\\repo.")
    _snapshot(vault, folders=folders)
    result = path(PathStart(surface="claude-code", folder="D:/work/elsewhere", role="worker"))
    by_id = {p.id: p for p in result.pieces}
    hook = by_id["hook:SessionStart/area-start.py"]
    # The hooks are the user's, so they run in every folder; the folder's own part, with this folder's path.
    assert hook.text == everywhere + "\n\nThis session runs in D:\\work\\elsewhere, outside the vault: open_area on D:\\work\\elsewhere."
    assert "in D:/work/repo, with this folder's path" in hook.every_time
    assert always(by_id["note:_/agent/roles/Worker.md"])
    # A start folder no area lists is said as the start's, not as a work.area naming nothing.
    assert {"from": ["start.folder"], "target": "D:/work/elsewhere",
            "via": f"no area in {ap.AREAS_NOTE} lists it"} in result.structure.broken_pointers
    assert not any(n.startswith("work.area") for n in result.notes or [])


def test_a_worktree_start_loads_its_repos_own_claude_md_not_those_above_it(vault):
    folders = json.loads(json.dumps(SNAPSHOT["folders"]))
    folders["D:/work/repo"]["loads"].insert(2, {"id": "file:D:/work/CLAUDE.md", "exists": True,
                                                "arrives": "loaded at the start by Claude Code (D:/work)"})
    _snapshot(vault, folders=folders, files={**SNAPSHOT["files"], "file:D:/work/CLAUDE.md": {"text": "Work.\n"}})
    assert "file:D:/work/CLAUDE.md" in ids(path(PathStart(typical=2)))
    tree = path(PathStart(surface="claude-code", folder="D:/work/repo/.claude/worktrees/fix", role="worker"))
    order = ids(tree)
    assert "file:D:/work/repo/CLAUDE.md" in order and "file:D:/work/CLAUDE.md" not in order
    assert "hook:SessionStart/area-start.py" in order
    assert any(line.startswith("the CLAUDE.md files of the worktree") for line in tree.structure.not_followed)


# --- The backward question returns the path up to the place (owner, 2026-11-19, round 3) ------------------------


def test_reached_by_gives_texts_up_to_the_place_and_every_other_piece_as_a_line(vault):
    result = reached_by("_/agent/roles/Worker.md")
    (hit,) = result.reached_by
    assert hit["via"] == ["hook:SessionStart/area-start.py", "note:_/agent/roles/Worker.md"]
    assert hit["pointed_to_by"] == ["hook:SessionStart/area-start.py"]
    with_text = ids(result)
    # The chain, the place included, and the start pieces its agents share, in arrival order, with their texts.
    assert with_text[:3] == ["file:C:/Users/me/.claude/CLAUDE.md", "file:D:/work/repo/CLAUDE.md",
                             "note:_/agent/Common-rules.md"]
    assert {"hook:SessionStart/area-start.py", "note:_/agent/roles/Worker.md"} <= set(with_text)
    assert all(p.text is not None for p in result.pieces)
    # Every other piece on the path, one line each; the tool texts as one line per server.
    assert "note:_/agent/Vault conventions.md" not in with_text
    conv = [line for line in result.other_pieces if line.startswith("note:_/agent/Vault conventions.md  ")]
    assert len(conv) == 1 and "when: Before working on one of the owner's areas" in conv[0]
    assert any(line.startswith("tool:obsidian-mcp:* (") for line in result.other_pieces)
    assert not any(line.startswith("tool:obsidian-mcp:read_note") for line in result.other_pieces)
    assert any(n.startswith("other_pieces: every other piece on those paths") and "read_from={id, offset: 0}" in n
               for n in result.notes)
    # Any piece on those paths can be read, not only those listed with their text.
    part = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, None,
                                                       ReadFrom(id="note:_/agent/Vault conventions.md")))
    assert [p.id for p in part.pieces] == ["note:_/agent/Vault conventions.md"]
    assert part.pieces[0].text == NOTES["_/agent/Vault conventions.md"]
    # With known, the lines came with the first call, as the structure did.
    again = reached_by("_/agent/roles/Worker.md", known={p.id: p.hash for p in result.pieces})
    assert again.other_pieces is None and again.structure is None and not again.pieces


def test_read_from_on_a_servers_tool_line_gives_its_texts_whole_page_by_page(vault, monkeypatch):
    # Agent paths, 2026-11-20: read_from refused the `tool:<server>:*` id the line showed, and the line named none
    # of its members.
    place = "_/agent/roles/Worker.md"
    result = reached_by(place)
    (line,) = [x for x in result.other_pieces if x.startswith("tool:obsidian-mcp:* (")]
    count = int(line.split("(")[1].split(" ")[0])
    combined = line.split("#")[1].split(" ")[0]
    assert any("tool:<server>:* line gives its texts whole" in n for n in result.notes)
    monkeypatch.setattr(apt, "PAGE_CHARS", 20_000)
    got, offset, calls = [], 0, 0
    while offset is not None:
        page = asyncio.run(apt.agent_path_reached_by_impl(place, None, None,
                                                          ReadFrom(id="tool:obsidian-mcp:*", offset=offset)))
        assert page.error is None and page.structure is None and page.reached_by is None
        assert page.pieces and all(p.text is not None and p.text_offset is None for p in page.pieces)
        assert len(page.pieces) == 1 or len(page.model_dump_json()) <= 20_000
        got += page.pieces
        assert f"texts {offset + 1} to {len(got)} of {count}" in page.notes[-1]
        offset = len(got) if "Next: read_from=" in page.notes[-1] else None
        calls += 1
    assert len(got) == count and calls > 1 and len({p.id for p in got}) == count
    assert all(ap.digest(p.text) == p.hash for p in got)
    assert ap.digest("".join(p.hash for p in got)) == combined  # the line's texts, in its order
    # Past the last text, or a server with no texts on these paths, is refused.
    past = asyncio.run(apt.agent_path_reached_by_impl(place, None, None,
                                                      ReadFrom(id="tool:obsidian-mcp:*", offset=count)))
    assert past.error and "past the last text" in past.error
    none = asyncio.run(apt.agent_path_reached_by_impl(place, None, None, ReadFrom(id="tool:nope:*")))
    assert none.error and "no tool texts of nope" in none.error


def test_the_structure_is_cut_first_when_that_lets_the_texts_fit(vault):
    from src.plugins.kelno_vault.agent_path_result import PathStructure, ReachedByResult

    def result(n):
        st = PathStructure(broken_pointers=[{"from": [f"open_area:Areas/A{i}/A{i}.md"], "target": "_/agent/x.md",
                                             "via": "Read [[_/agent/x]]."} for i in range(n)])
        return ReachedByResult(place="x", pieces=[], structure=st)

    big = ap.Piece("note:big.md", ap.LOADED, "loaded", "big.md", "x" * 60_000)
    fits, notes = result(400), []
    apt._trim_structure(fits, [big], notes)
    assert fits.structure.cut == ["broken_pointers"] and 0 < len(fits.structure.broken_pointers) < 400
    assert len(fits.model_dump_json()) + 60_010 <= apt.PAGE_CHARS - apt._NOTES_RESERVE
    assert notes and notes[0].startswith(apt._CUT_NOTE)
    # Texts that can't fit anyway come over several calls: the structure stays whole.
    huge = ap.Piece("note:huge.md", ap.LOADED, "loaded", "huge.md", "x" * 90_000)
    whole, notes = result(400), []
    apt._trim_structure(whole, [huge], notes)
    assert whole.structure.cut is None and len(whole.structure.broken_pointers) == 400 and not notes


def test_a_whole_drawing_says_why(vault):
    path(PathStart(typical=2))
    (k,) = apt._KEPT.values()
    assert k.cause == "the first drawing since the server started"
    _write(vault, "Areas/Areas.md", NOTES["Areas/Areas.md"] + "| Other | | |\n")
    path(PathStart(typical=2))
    (k,) = apt._KEPT.values()
    assert k.cause == "changed: Areas/Areas.md" and k.reach_map().cause == k.cause
    # An update draws only what changed, and has no cause to give.
    _write(vault, "_/agent/roles/Worker.md", "Worker rules, again.\n")
    path(PathStart(typical=2))
    (k,) = apt._KEPT.values()
    assert k.cause == "" and k.redrawn < len(k.runs)
    before = ap.ServedTexts([("a", "one"), ("b", "two")], "i", "g", "o")
    now = ap.ServedTexts([("a", "one"), ("b", "three")], "i", "g2", "o")
    assert apt._served_change(before, now) == "served texts changed: guide; tools b"


# --- The test bench's rerun after round 2 (Agent paths, 2026-11-20) ---------------------------------------------


def test_once_a_piece_always_on_the_path_waits_no_piece_waiting_on_a_moment_comes_before_it(vault):
    from src.plugins.kelno_vault.agent_path_result import ReachedByResult

    first = ap.Piece("note:a1.md", ap.LOADED, "loaded", "a1.md", "a" * 30_000)
    held = ap.Piece("note:a2.md", ap.LOADED, "loaded", "a2.md", "b" * 60_000)
    small = ap.Piece("note:w.md", ap.LATER, "later", "w.md", "c" * 1_000, None, "when x", always=False)
    pieces = [first, held, small]
    out = [apt._piece(p, None) for p in pieces]
    result = ReachedByResult(place="x", pieces=out)
    apt._with_texts(result, pieces, out, [], None)
    # The small piece would fit, but it waits on a moment and an always piece before it waits: it waits too.
    assert [o.text is not None for o in result.pieces] == [True, False, False]
    assert result.waiting == 2


def test_the_tool_texts_come_before_the_pieces_waiting_on_a_moment(vault):
    common = vault / "_/agent/Common-rules.md"
    common.write_text(common.read_text(encoding="utf-8") + "- When the build breaks, read [[_/agent/Notes kept]].\n",
                      encoding="utf-8")
    eng = drawn(PathStart(typical=1))
    stages = [eng[p.id].stage for p in path(PathStart(typical=1)).pieces]
    tools_end = max(i for i, s in enumerate(stages) if s == ap.TOOLS)
    assert stages.index(ap.LATER) > tools_end
    # The overview's line for the tool texts stands where they come.
    lines = path().typical[0].pieces
    tools_at = next(i for i, line in enumerate(lines) if ". tool:obsidian-mcp:* (" in line)
    assert tools_at < next(i for i, line in enumerate(lines) if "  when: " in line)


def test_a_piece_with_another_text_on_another_path_is_a_line_of_its_own(vault):
    folders = json.loads(json.dumps(SNAPSHOT["folders"]))
    other = json.loads(json.dumps(folders["D:/work/repo"]))
    other["session_start"][0]["text"] += " This session runs in another folder."
    folders["D:/work/other"] = other
    _snapshot(vault, folders=folders)
    _write(vault, "Areas/Areas.md", NOTES["Areas/Areas.md"].replace("` | [[", "`; `D:/work/other` | [["))
    result = reached_by("_/agent/roles/Worker.md")
    hook = next(p for p in result.pieces if p.id == "hook:SessionStart/area-start.py")
    assert "another folder" not in hook.text
    lines = [line for line in result.other_pieces if line.startswith("hook:SessionStart/area-start.py  ")]
    assert lines == [("hook:SessionStart/area-start.py  1 other version of its text, on the paths of a Claude Code "
                      "worker in D:/work/other")]
    assert any(n.startswith("Some pieces have another text on other paths") for n in result.notes)


def test_an_area_without_for_agents_says_so(vault):
    _write(vault, "Areas/Repo/Repo.md", NOTES["Areas/Repo/Repo.md"].replace(
        "## For agents\n\n- Read [[Areas/Repo/Repo notes]] before changing the build.\n\n", ""))
    result = path(PathStart(typical=2))
    assert "section:Areas/Repo/Repo.md#For agents" not in ids(result)
    assert any(x.startswith("Repo's `## For agents`: its main note, Areas/Repo/Repo.md, has none")
               for x in result.structure.not_followed)


# --- Moments after the cold read (Agent paths, 2026-11-20) -------------------------------------------------------


def test_a_moment_is_the_pointers_whole_condition():
    line = "- Before working on one of the owner's areas, or writing anything in the vault, read [[X]]. Then go on."
    assert ap.pointer_moment(line, line[2:]) == "Before working on one of the owner's areas, or writing anything in the vault"
    # With no instruction in its first sentence, the moment stays its opening.
    plain = "- When the build breaks, the logs are in [[X]]."
    assert ap.pointer_moment(plain, plain[2:]) == "When the build breaks"
    # The guide index's shape is unchanged.
    assert ap.pointer_moment("- [[g|g]]: any code work, in any repository.", "x", "[[g|g]]") == "any code work, in any repository"


def test_a_long_line_is_cut_around_its_pointer():
    line = ("Background on many things. " * 12 + "When relaying through the voice bridge, it also follows "
            "[[_/agent/roles/Relay|Relay]], which the bridge hands it. " + "More words. " * 30)
    out = apt._window(line, "When relaying through the voice bridge", "note:_/agent/roles/Relay.md")
    assert out.startswith("…When relaying through the voice bridge") and "[[_/agent/roles/Relay|Relay]]" in out
    assert len(out) <= apt._VIA_MAX + 130
    far = "When it rains, " + "words " * 80 + "read [[Far note]] then."
    out = apt._window(far, "When it rains", "note:Far note.md")
    assert out.startswith("When it rains") and " … " in out and "[[Far note]]" in out


def test_the_arrival_that_places_a_piece_comes_first(vault):
    by_id = {p.id: p for p in path(PathStart(typical=2)).pieces}
    rules = by_id["section:Areas/Repo/Repo.md#For agents"]
    first = rules.lines[0]
    assert (first.when, first.piece) == ("when the area is opened", "tool:obsidian-mcp:open_area")
    # A line with no moment of its own (a hook's "read …") holds whenever its piece is read: no `when`.
    worker = by_id["note:_/agent/roles/Worker.md"]
    assert worker.lines[0].piece == "hook:SessionStart/area-start.py" and worker.lines[0].when is None


def test_a_line_in_a_waiting_piece_says_after_which_moment(vault):
    _write(vault, "_/agent/guides/coding-practices.md", "Code rules.\n\n- When the tests fail, read [[_/agent/Notes kept]].\n")
    _write(vault, "Areas/Repo/Repo.md", NOTES["Areas/Repo/Repo.md"].replace("code_project: true", "code_project: false"))
    by_id = {p.id: p for p in path(PathStart(typical=1)).pieces}
    guide = by_id["note:_/agent/guides/coding-practices.md"]
    assert not always(guide)
    kept = by_id["note:_/agent/Notes kept.md"]
    (m,) = [x for x in kept.lines if x.piece == "note:_/agent/guides/coding-practices.md"]
    # The line's own moment; the piece it sits in waits on its own, which comes first (round 5: the result no longer
    # says `after`; the engine still knows it).
    assert m.when == "When the tests fail" and guide.when == ["any code work"]
    eng = drawn(PathStart(typical=1))["note:_/agent/Notes kept.md"]
    (e,) = [x for x in eng.moments if x["piece"] == "note:_/agent/guides/coding-practices.md"]
    assert e["after"] == "any code work"


# --- The helper process ends with the server ------------------------------------------------------------------


def test_the_helper_process_ends_when_the_server_shuts_down(monkeypatch):
    import time as _time

    from src.plugins.kelno_vault import plugin

    import concurrent.futures
    import multiprocessing

    # A helper as `_helper` starts it, without its initializer, which imports the server's code in the child.
    helper = concurrent.futures.ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    monkeypatch.setattr(apt, "_HELPER", helper)
    monkeypatch.setattr(apt, "_HELPER_STOP_SECONDS", 0.5)
    assert plugin.startup is apt.helper_lifespan
    assert helper.submit(os.getpid).result(timeout=120) != os.getpid()
    busy = helper.submit(_time.sleep, 60)  # a drawing under way
    (worker,) = helper._processes.values()

    async def serve_then_stop():
        task = asyncio.create_task(plugin.startup())
        await asyncio.sleep(0.1)
        assert not task.done()  # it waits while the server serves
        task.cancel()
        await asyncio.wait([task], timeout=5.0)  # as `plugins.stop_background` gives it
        return task

    assert not busy.done()
    started = _time.monotonic()
    task = asyncio.run(serve_then_stop())
    assert task.cancelled() and _time.monotonic() - started < 5.0
    assert not worker.is_alive() and apt._HELPER is None
    apt.stop_helper()  # nothing left to stop


# --- Round 4: the cold read of reached_by and the test bench's retest (Agent paths, 2026-11-20) -------------------


def test_a_backward_question_fills_its_page_from_the_place_out(vault):
    from src.plugins.kelno_vault.agent_path_result import ReachedByResult

    start = ap.Piece("note:start.md", ap.LOADED, "loaded", "start.md", "s" * 60_000)
    place = ap.Piece("note:place.md", ap.LATER, "later", "place.md", "p" * 30_000, None, "when x", always=False)
    pieces = [start, place]
    out = [apt._piece(p, None) for p in pieces]
    result = ReachedByResult(place="x", pieces=out)
    apt._with_texts(result, pieces, out, [], None, fill_order=["note:place.md", "note:start.md"])
    # Listed in path order, filled from the place out: the distant start piece waits, not the place.
    assert [o.id for o in result.pieces] == ["note:start.md", "note:place.md"]
    assert [o.text is not None for o in result.pieces] == [False, True]


def test_a_section_place_gives_the_section_as_the_place_and_its_note_as_a_line(vault):
    _write(vault, "_/agent/roles/Worker.md",
           "Worker rules.\n\n## Landing\n\nAsk first.\n\n### Detail\n\nMore.\n\n## Other\n\nElse.\n")
    result = reached_by("_/agent/roles/Worker.md#Landing")
    assert result.matched_by.startswith("its note (it holds that section): the section is the place")
    by_id = {p.id: p for p in result.pieces}
    part = by_id["section:_/agent/roles/Worker.md#Landing"]
    # The section, to the next heading of its level, at any level.
    assert part.text == "## Landing\n\nAsk first.\n\n### Detail\n\nMore.\n"
    assert part.hash == ap.digest(part.text) and part.every_time.startswith("in note:_/agent/roles/Worker.md, which is ")
    assert "note:_/agent/roles/Worker.md" not in by_id
    (line,) = [x for x in result.other_pieces if x.startswith("note:_/agent/roles/Worker.md  ")]
    assert line.endswith("(the place's note: the place is its section, given in pieces)")
    whole = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md#Landing", None, None,
                                                        ReadFrom(id="note:_/agent/roles/Worker.md")))
    assert whole.pieces[0].text.startswith("Worker rules.")
    # A heading not in the note yet: the whole note, as before.
    assert "note:_/agent/roles/Worker.md" in ids(reached_by("_/agent/roles/Worker.md#Not yet"))
    assert ap.heading_section("# A\n\n## B\n\nb\n\n# C\n", "B") == "## B\n\nb\n"
    assert ap.heading_section("```\n## B\n```\n", "B") is None


def test_reached_by_tags_each_piece_with_the_groups_that_have_it(vault):
    _write(vault, "_/agent/Memory reference.md", NOTES["_/agent/Memory reference.md"]
           + "- Before writing a rule, read [[_/agent/Common-rules|Common-rules]].\n")
    result = reached_by("_/agent/Common-rules.md")
    groups = {g["group"]: g for g in result.reached_by}
    assert sorted(groups) == list(range(1, len(groups) + 1))
    chat = next(n for n, g in groups.items() if g["surface"] == "claude-ai")
    code = next(n for n, g in groups.items() if g["surface"] == "claude-code")
    # Two lines of memory point to it: one piece pointing, said once.
    assert groups[chat]["pointed_to_by"] == ["memory:/preferences.md"]
    by_id = {p.id: p for p in result.pieces}
    assert by_id["file:C:/Users/me/.claude/CLAUDE.md"].every_time_for == [code]
    assert by_id["memory:/preferences.md"].every_time_for == [chat]
    # Every time for both groups: said as one list (round 5: groups and other_stages gave way to these).
    common = by_id["note:_/agent/Common-rules.md"]
    assert common.every_time_for == sorted([chat, code]) and common.when_for is None
    assert apt._GROUPS_NOTE in result.notes and apt._FIELD_NOTE in result.notes
    again = reached_by("_/agent/Common-rules.md", known={p.id: p.hash for p in result.pieces})
    assert apt._FIELD_NOTE not in (again.notes or [])


def test_what_open_area_serves_with_the_place_comes_with_its_text(vault):
    with_text = ids(reached_by("Areas/Repo/Repo.md"))
    # In front of the agent when the place applies: the rest of open_area's one result.
    assert {"note:_/agent/Working on an area.md", "tool:obsidian-mcp:open_area/instructions"} <= set(with_text)


def test_no_tool_text_comes_while_a_rule_piece_waits(vault):
    from src.plugins.kelno_vault.agent_path_result import AgentPathResult

    first = ap.Piece("note:a1.md", ap.LOADED, "loaded", "a1.md", "a" * 30_000)
    held = ap.Piece("note:a2.md", ap.LOADED, "loaded", "a2.md", "b" * 60_000)
    tool = ap.Piece("tool:obsidian-mcp:x", ap.TOOLS, "when the tools load", "this server", "t" * 1_000)
    pieces = [first, held, tool]
    out = [apt._piece(p, None) for p in pieces]
    result = AgentPathResult(pieces=out)
    apt._with_texts(result, pieces, out, [], None)
    assert [o.text is not None for o in result.pieces] == [True, False, False]


def test_the_field_notes_come_with_a_first_call(vault):
    first = path(PathStart(typical=2))
    assert apt._FIELD_NOTE in first.notes
    again = path(PathStart(typical=2), known={p.id: p.hash for p in first.pieces})
    assert apt._FIELD_NOTE not in (again.notes or [])


def test_pointers_alike_but_for_where_they_come_from_are_one_entry():
    items = [{"from": f"open_area:Areas/A{i}/A{i}.md", "to": "note:_/agent/x.md", "via": "> When here, read x."}
             for i in range(3)] + [{"from": "note:y.md", "to": "note:_/agent/x.md", "via": "other"}]
    assert apt._grouped_from(items) == [
        {"from": [f"open_area:Areas/A{i}/A{i}.md" for i in range(3)], "to": "note:_/agent/x.md",
         "via": "> When here, read x."},
        {"from": ["note:y.md"], "to": "note:_/agent/x.md", "via": "other"}]
    assert apt._grouped_from(None) is None and apt._grouped_from([]) == []
    none = [f"{a}'s `## For agents`: its main note, Areas/{a}/{a}.md, has none, so open_area serves none; nothing is "
            "left out" for a in ("Dog", "Mail")]
    assert ap.folded_not_followed(["first", *none, "last"]) == [
        "first", ("These areas' main notes have no `## For agents`, so open_area serves none; nothing is left out: "
                  "Dog, Mail"), "last"]
    assert ap.folded_not_followed(none[:1]) == none[:1]
    assert apt._who(["a Claude Code worker in A", "the Coordinator", "a Claude Code worker in B",
                     "a claude.ai chat working on X"]) == (
        "Claude Code workers in A, B; the Coordinator; a claude.ai chat working on X")


def test_one_note_says_which_structure_lists_are_cut():
    from src.plugins.kelno_vault.agent_path_result import PathStructure

    notes, st = ["x"], PathStructure(cut=["broken_pointers"])
    apt._note_cut(notes, st)
    st.cut = ["broken_pointers", "unplaced_pointers"]
    apt._note_cut(notes, st)
    assert notes == ["x", f"{apt._CUT_NOTE} (broken_pointers, unplaced_pointers)."]
