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
handwritten = 1

[[typical]]
name = "a Claude Code worker in a repo"
surface = "claude-code"
folder = "D:/work/repo"
role = "worker"
handwritten = 3
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


def ids(result):
    return [p.id for p in result.pieces]


def test_hash_is_the_first_12_hex_digits_of_sha256_of_the_text_as_the_pc_reads_it():
    assert ap.digest("abc") == hashlib.sha256(b"abc").hexdigest()[:12]
    assert ap.as_text(b"a\r\nb\rc") == "a\nb\nc"


def test_agent_path_draws_a_path_with_texts_hashes_and_known(vault):
    first = path(PathStart(typical=3))
    assert first.error is None and first.machine == "PC" and first.snapshot_made == SNAPSHOT["made"]
    order = ids(first)
    # What Claude Code loads, then the hook, then the role note it names for a worker only.
    assert order[:3] == ["file:C:/Users/me/.claude/CLAUDE.md", "file:D:/work/repo/CLAUDE.md",
                         "note:_/agent/Common-rules.md"]
    assert order[3] == "hook:SessionStart/area-start.py" and "hook:SessionStart/quiet.py" not in order
    assert order[4] == "note:_/agent/roles/Worker.md" and "note:_/agent/roles/Coordinator.md" not in order
    # The repo's area, as open_area serves it, then what its rules point to.
    work = [i for i in order if first.pieces[order.index(i)].stage == ap.STAGE_NAMES[ap.WORK]]
    assert work == ["section:Areas/Repo/Repo.md#For agents", "note:Areas/Repo/Repo.md",
                    "section:_/agent/Vault conventions.md#Working on an area",
                    "tool:obsidian-mcp:open_area/instructions", "note:Areas/Repo/Repo notes.md"]
    # A pointer that says when comes later; a link that says neither when nor "read" is a mention, counted and
    # not followed, a rule file's listed by name [P250].
    by_id = {p.id: p for p in first.pieces}
    assert by_id["note:_/agent/Vault conventions.md"].moment == "Before working on one of the owner's areas"
    assert "note:_/agent/Notes kept.md" not in by_id and "note:Notes/Some note.md" not in by_id
    assert first.structure.mentions_not_followed == 2
    assert [m["target"] for m in first.structure.rule_file_mentions] == ["_/agent/Notes kept.md"]
    # Already met through the guide index before the area opens, so code_project adds no second copy.
    assert by_id["note:_/agent/guides/coding-practices.md"].moment == "any code work"
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
    assert all(p.text is not None for p in first.pieces if p.stage != ap.STAGE_NAMES[ap.TOOLS])
    assert any("PreToolUse" in line for line in first.structure.not_followed)

    known = {p.id: p.hash for p in first.pieces}
    again = path(PathStart(typical=3), known=known)
    assert not again.pieces and again.waiting == 0  # held pieces aren't listed again
    assert again.left_path is None
    gone = path(PathStart(typical=3), known={**known, "note:_/agent/Old rule.md": "0123456789ab"})
    assert gone.left_path == ["note:_/agent/Old rule.md"]

    (vault / "_/agent/roles/Worker.md").write_text("Worker rules, changed.\n", encoding="utf-8")
    changed = path(PathStart(typical=3), known=known)
    worker = next(p for p in changed.pieces if p.id == "note:_/agent/roles/Worker.md")
    assert worker.status == "changed" and worker.text == "Worker rules, changed.\n"
    assert [p.id for p in changed.pieces if p.text is not None] == ["note:_/agent/roles/Worker.md"]


def test_a_claude_ai_chat_starts_from_memory_and_reads_what_its_lines_point_to(vault):
    result = path(PathStart(surface="claude-ai"), PathWork(area="Repo"))
    order = ids(result)
    assert order[:2] == ["memory:/preferences.md", "note:_/agent/Common-rules.md"]
    assert result.pieces[1].moment == "At the start of every chat"
    assert "section:Areas/Repo/Repo.md#For agents" in order
    assert not any(i.startswith(("file:", "hook:")) for i in order)


def test_the_relay_role_adds_the_relays_notes(vault):
    result = path(PathStart(surface="claude-ai", role="relay"))
    assert "note:_/agent/roles/Relay.md" in ids(result)
    assert not any("changed nothing" in n for n in result.notes or [])


def test_mentions_are_grouped_by_rule_file_and_held_pieces_come_short(vault):
    first = path(PathStart(typical=3))
    assert first.structure.rule_file_mentions == [
        {"target": "_/agent/Notes kept.md", "from": ["note:_/agent/Common-rules.md"]}]
    # A call with known lists only what it brings: nothing here, every piece held.
    again = path(PathStart(typical=3), known={p.id: p.hash for p in first.pieces})
    assert not again.pieces and again.waiting == 0 and again.left_path is None
    assert again.structure is None and any("first call" in n for n in again.notes)
    reached = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, None))
    held = {p.id: p.hash for p in reached.pieces}
    again = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", held, None))
    assert reached.reached_by and again.reached_by is None and again.structure is None and not again.pieces


def test_a_result_whose_metadata_passes_the_budget_is_cut_down_and_every_text_arrives(vault, monkeypatch):
    monkeypatch.setattr(apt, "PAGE_CHARS", 12_000)
    first = path(PathStart(typical=3))
    assert len(first.model_dump_json()) <= 12_000
    # Pieces whose text waits come in short form; those with a text carry their details.
    waiting = [p for p in first.pieces if p.text_withheld]
    assert waiting and all(p.arrives is None and p.where is None and p.hash for p in waiting)
    assert all(p.arrives and p.where for p in first.pieces if p.text is not None)
    assert any("short form" in n for n in first.notes)
    # Passing what it now holds, the caller gets the rest: every text arrives within a few calls.
    # (A text too long for one result is read in parts: counted as held here.)
    held, rounds = {}, 0
    while rounds < 30:
        r = path(PathStart(typical=3), known=held or None)
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
    full = path(PathStart(typical=3))
    assert set(held) == {p.id for p in full.pieces if not p.private}


def test_the_overview_stays_within_the_budget(vault, monkeypatch):
    monkeypatch.setattr(apt, "PAGE_CHARS", 2_500)
    over = path()
    assert len(over.model_dump_json()) <= 2_500
    assert any("cut short" in n for n in over.notes)
    assert any(line.startswith("… ") for t in over.typical for line in t.pieces)


def test_a_hook_script_changed_since_the_snapshot_is_said(vault):
    script = "_/claude-code-hooks/area-start.py"
    (vault / script).parent.mkdir(parents=True, exist_ok=True)
    (vault / script).write_text("print('hello')\n", encoding="utf-8")
    snap = {**SNAPSHOT, "format": 2, "hook_scripts": {f"note:{script}": ap.digest("print('hello')\n"),
                                                      "file:C:/elsewhere/other.py": None}}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")
    same = path(PathStart(typical=3))
    assert same.error is None and not any("changed since the snapshot" in n for n in same.notes or [])
    (vault / script).write_text("print('changed')\n", encoding="utf-8")
    changed = path(PathStart(typical=3))
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

    first = path(PathStart(typical=3))
    assert first.error is None
    by_id = {p.id: p for p in first.pieces}
    # The rules file comes first; the missing user CLAUDE.md is nothing.
    assert ids(first)[:3] == ["file:C:/Users/me/.claude/rules/mine.md", "file:D:/work/repo/CLAUDE.md",
                              "file:D:/work/repo/CLAUDE.fork.md"]
    mine = by_id["file:C:/Users/me/.claude/rules/mine.md"]
    assert mine.arrives == rules and mine.text == "My rules.\n" and "file:C:/Users/me/.claude/CLAUDE.md" not in by_id
    assert not first.structure.broken_pointers
    # The import in the folder loads; the one outside it is a fact, and the role note it names isn't loaded by it.
    assert by_id["file:D:/work/repo/CLAUDE.fork.md"].arrives == "imported by file:D:/work/repo/CLAUDE.md"
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
    assert {p.id: p.arrives for p in home.pieces}["note:_/agent/Common-rules.md"] == (
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
    result = path(PathStart(typical=3))
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
    first = path(PathStart(typical=3))
    assert len(first.model_dump_json()) <= 25_000
    withheld = [p for p in first.pieces if p.text_withheld]
    assert withheld and any("text_withheld" in n for n in first.notes)
    held = {p.id: p.hash for p in first.pieces if p.text is not None}
    second = path(PathStart(typical=3), known=held)
    got = {p.id for p in second.pieces if p.text is not None}
    assert got and got <= {p.id for p in withheld}


def test_a_text_no_result_can_carry_comes_in_parts_with_read_from(vault, monkeypatch):
    monkeypatch.setattr(apt, "PAGE_CHARS", 25_000)
    long_text = "Worker rules, é. " * 2_000
    (vault / "_/agent/roles/Worker.md").write_text(long_text, encoding="utf-8")
    first = path(PathStart(typical=3))
    worker = next(p for p in first.pieces if p.id == "note:_/agent/roles/Worker.md")
    assert worker.text_in_parts is True and worker.text is None and not worker.text_withheld
    assert any("text_in_parts" in n and "read_from" in n for n in first.notes)
    # Holding every other text, the next call still doesn't promise it.
    held = {p.id: p.hash for p in first.pieces if p.id != worker.id}
    again = path(PathStart(typical=3), known=held)
    assert [p.id for p in again.pieces if p.status == "new"] == [worker.id]
    assert not any("did not fit" in n for n in again.notes)
    # Part by part, each within the budget, until the whole text, whose hash is the piece's.
    got, offset, calls = "", 0, 0
    while offset is not None:
        part = asyncio.run(apt.agent_path_impl(PathStart(typical=3), None, None,
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
    off = asyncio.run(apt.agent_path_impl(PathStart(typical=3), None, None, ReadFrom(id="note:Nope.md")))
    assert "not on this path" in off.error
    past = asyncio.run(apt.agent_path_impl(PathStart(typical=3), None, None,
                                           ReadFrom(id=worker.id, offset=len(long_text) + 1)))
    assert "past the end" in past.error


def test_a_note_in_a_private_folder_keeps_its_text(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "private_folder_names", ["* (private)"])
    (vault / "Areas/Repo/Repo.md").write_text(
        NOTES["Areas/Repo/Repo.md"].replace("before changing the build.", "before changing the build.\n"
                                            "- Read [[Areas/Repo/Secret (private)/Plan]] before a release."),
        encoding="utf-8")
    result = path(PathStart(typical=3))
    plan = next(p for p in result.pieces if p.id == "note:Areas/Repo/Secret (private)/Plan.md")
    assert plan.private is True and plan.text is None and plan.hash == ap.digest("Private plan.\n")


def test_agent_path_refuses_a_start_without_surface_or_typical_agent(vault):
    assert "start needs a surface" in path(PathStart(folder="D:/work/repo")).error
    assert "give it alone" in path(PathStart(typical=3, surface="claude-ai")).error
    assert "No typical agent 9" in path(PathStart(typical=9)).error


def test_agent_path_without_start_gives_the_overview(vault):
    result = path()
    assert [t.number for t in result.typical] == [1, 3]
    worker = result.typical[1]
    assert worker.pieces[0].startswith("1. file:C:/Users/me/.claude/CLAUDE.md")
    assert worker.pieces[-1].startswith(f"{len(worker.pieces)}. tool:obsidian-mcp:*")
    assert any("when: Before working on one of the owner's areas" in line for line in worker.pieces)
    # Nothing points to these on this vault (memory is read through its mirror's lines, not the note; Notes
    # kept is only mentioned).
    assert result.rule_files_no_path_reaches == [
        "note:CLAUDE.md", "note:_/agent/Memory reference.md", "note:_/agent/Notes kept.md",
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
    assert hit["agents"] == ["a Claude Code worker in a repo", "a Claude Code worker, any related folder"]
    assert hit["via"] == ["hook:SessionStart/area-start.py", "note:_/agent/roles/Worker.md"]
    assert "note:_/agent/roles/Worker.md" in {p.id for p in result.pieces}

    future = asyncio.run(apt.agent_path_reached_by_impl("Areas/Repo/Not yet.md", None, None))
    assert future.matched_by == "its area (Repo)" and future.reached_by


def test_reached_by_refuses_an_unknown_machine(vault):
    result = asyncio.run(apt.agent_path_reached_by_impl("_/agent/roles/Worker.md", None, "NOPE"))
    assert "No snapshot from the machine 'NOPE'" in result.error and "PC" in result.error


def test_without_a_snapshot_the_pc_side_is_said_to_be_missing(vault):
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").unlink()
    result = path(PathStart(typical=3))
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
    assert by_id["note:_/agent/Vault conventions.md"].moment == "When working on one of the owner's areas"
    coordinator = by_id["note:_/agent/roles/Coordinator.md"]
    assert coordinator.moment == "While the setup is on trial" and coordinator.always
    # The index's moment, cut at a table cell, for its leading link only.
    assert by_id["note:_/agent/guides/coding-practices.md"].moment == "any code work"
    # A bold title that is a sentence of its own: the next sentence opens the pointer.
    assert by_id["note:_/agent/Where agent instructions go.md"].moment == "When you merge"
    # A bold label in front, a bare "read", another opening word, a second link on an index line, a rule section's
    # link in no shape: mentions, not on the path. A link written as code isn't a link at all.
    for gone in ("note:_/agent/roles/Worker.md", "note:_/agent/Notes kept.md", "note:_/agent/roles/Relay.md",
                 "note:_/agent/Memory reference.md", "section:Areas/Repo/Repo.md#For agents"):
        assert gone not in by_id
    assert {m["target"] for m in result.structure.rule_file_mentions} == {
        "_/agent/roles/Worker.md", "_/agent/Notes kept.md", "_/agent/roles/Relay.md", "_/agent/Memory reference.md",
        "Areas/Repo/Repo.md#For agents"}
    assert result.structure.mentions_not_followed == 6  # Relay twice: an index line led by `[[#Tasks]]` too
    # A path to nothing is reported, but for a Syncthing version's name (outside the vault).
    assert {b["target"] for b in result.structure.broken_pointers} == {
        "_/agent/Renamed.md", "_/agent/Gone.md", "_/agent/Missing"}
    # A hook's message isn't written under the convention: its plain "Read the vault note …" still points.
    assert "note:_/agent/roles/Worker.md" in ids(path(PathStart(typical=3)))


def test_a_backticked_path_naming_a_kind_of_note_reaches_every_match(vault):
    # P261, P262: a vault path with <placeholder> segments names a kind of note; every match is reached, with the
    # line's moment. When the sentence names a rule section, each match's section is the piece, and only notes
    # that have one count.
    before = path(PathStart(surface="claude-ai")).structure.mentions_not_followed
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
    assert inbox.moment == "Before filing into a folder or changing it" and not inbox.always
    assert inbox.stage == ap.STAGE_NAMES[ap.LATER] and inbox.text == "## For agents\n\n- File by date."
    assert "matching `<folder>/<folder>.md`" in inbox.arrives
    # Every folder note's For agents section, at any depth, the area's main note's too; a folder note without
    # one brings nothing, and the folder notes aren't read whole.
    assert {"section:Areas/Repo/Repo.md#For agents", "section:Reference/Servers/box/box.md#For agents"} <= set(by_id)
    assert not any(i.startswith(("note:Log/", "section:Log/", "note:Inbox/")) for i in by_id)
    # The machine notes: each read whole, with its rule sections, and nothing below a machine's own folder.
    for machine in ("box", "pi"):
        assert by_id[f"note:Reference/Servers/{machine}/{machine}.md"].moment == "Before reaching another machine"
    assert "note:Reference/Servers/pi/old/old.md" not in by_id
    # The line that only says what a folder note is points to nothing: one mention, not one per match.
    assert result.structure.mentions_not_followed == before + 1

    reached = asyncio.run(apt.agent_path_reached_by_impl("Inbox/Inbox.md", None, None))
    assert reached.matched_by == "the place itself"
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

    result = path(PathStart(typical=3))
    by_id = {p.id: p for p in result.pieces}
    guard = by_id["hook:PreToolUse/guard.py#1"]
    assert guard.moment == when and not guard.always and guard.stage == ap.STAGE_NAMES[ap.LATER]
    assert guard.where == "D:/vault/_/claude-code-hooks/guard.py" and guard.arrives.startswith(
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
    started = STARTING_POINTS.replace('role = "worker"\nhandwritten = 3', 'started_by = "relay"\nrole = "worker"\n'
                                      'handwritten = 3')
    (vault / ap.STARTING_POINTS_PATH).write_text(started + (
        '\n[[typical]]\nname = "the voice relay"\nsurface = "claude-ai"\nextras = ["relay_notes"]\nhandwritten = 2\n'
        '\n[[typical]]\nname = "a session changing the rules"\nsurface = "claude-code"\nfolder = "D:/work/repo"\n'
        'role = "worker"\nhandwritten = 4\n'), encoding="utf-8")
    snap = {**SNAPSHOT, "format": 2, "bridge": bridge}
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps(snap), encoding="utf-8")

    # The relay: the guide with its first bridge result, the rest when the tools load; no notes.
    relay = {p.id: p for p in path(PathStart(typical=2)).pieces}
    assert relay["guide:claude-voice"].stage == ap.STAGE_NAMES[ap.BRIDGE] and relay["guide:claude-voice"].always
    assert relay["tool:claude-voice:health"].stage == ap.STAGE_NAMES[ap.TOOLS]
    assert "instructions:claude-voice" in relay and not any(i.startswith("bridge:") for i in relay)
    # A session the relay started: the guide when it first calls the bridge, the notes at their moments.
    worker = path(PathStart(typical=3))
    by_id = {p.id: p for p in worker.pieces}
    assert by_id["guide:claude-voice"].moment == "when it first calls the bridge" and not by_id["guide:claude-voice"].always
    note = by_id["bridge:claude-voice#2"]
    assert note.moment == "about once an hour" and note.stage == ap.STAGE_NAMES[ap.LATER] and not note.always
    assert not any("voice bridge" in line for line in worker.structure.not_followed)
    # A session the bridge didn't start, a claude.ai chat: no bridge.
    for other in (4, 1):
        assert not any("claude-voice" in i for i in ids(path(PathStart(typical=other))))
    # The overview folds each server's tool texts into one line.
    over = path()
    relay_lines = next(t for t in over.typical if t.number == 2).pieces
    assert any(line.split(". ", 1)[1].startswith("tool:claude-voice:* (3 texts") for line in relay_lines)
    reached = asyncio.run(apt.agent_path_reached_by_impl("bridge:claude-voice", None, None))
    assert {a for hit in reached.reached_by for a in hit["agents"]} == {"a Claude Code worker in a repo"}

    # The command failed on the PC, or the bridge isn't there: said, never guessed.
    for value, said in (({"command": "claude-voice texts", "error": "exit 1"}, "`claude-voice texts` failed on PC: exit 1"),
                        (None, "PC's snapshot says the bridge isn't on that PC")):
        (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps({**snap, "bridge": value}), encoding="utf-8")
        result = path(PathStart(typical=3))
        assert not any("claude-voice" in i for i in ids(result))
        assert any(said in line for line in result.structure.not_followed)


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
    plain = path(PathStart(typical=3))
    (vault / f"{ap.SNAPSHOT_FOLDER}/PC.json").write_text(json.dumps({**snap, "files": SNAPSHOT["files"]}),
                                                         encoding="utf-8")
    before = path(PathStart(typical=3)).structure.mentions_not_followed

    result = plain
    repo = [i for i in ids(result) if i.startswith("file:D:/work/repo/")]
    assert repo == ["file:D:/work/repo/CLAUDE.md"]
    assert result.structure.mentions_not_followed == before
    coding = next(p for p in result.pieces if p.id == "note:_/agent/guides/coding-practices.md")
    assert "file:D:/work/repo/CLAUDE.md" in [coding.arrives.removeprefix("pointed to by "), *(coding.also_pointed_by or [])]
    by_design = [line for line in result.structure.not_followed if line.startswith("The tools don't follow, by design:")]
    assert by_design == ["The tools don't follow, by design: in a repo, anything beyond what Claude Code loads "
                         "there. When one of these matters, point to it from a rule file the tools follow."]


def test_the_overview_says_what_is_left_out_by_design_once(vault):
    over = path()
    assert not any(line.startswith(ap.BY_DESIGN_OPENING) for t in over.typical
                   for line in t.structure.not_followed or [])
    assert [line for line in over.across_every_area.not_followed if line.startswith(ap.BY_DESIGN_OPENING)]


def test_without_the_vaults_by_design_paragraph_not_followed_says_it_is_missing(vault):
    (vault / ap.WHERE_NOTE).write_text("## Checking a rule against agent paths\n\n1. A chat.\n", encoding="utf-8")
    nf = path(PathStart(typical=3)).structure.not_followed
    assert any(line.startswith("what the tools leave out by design:") and ap.BY_DESIGN_OPENING in line
               for line in nf)


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


def test_into_areas_names_shared_and_local_pieces(vault):
    """`into_areas` speaks of shared and local pieces, keeping "trunk" for its sense in Common-rules (P276)."""
    common = vault / "_/agent/Common-rules.md"
    common.write_text(common.read_text(encoding="utf-8") + "- When the build breaks, read [[Areas/Repo/Repo notes]].\n",
                      encoding="utf-8")
    st = path(PathStart(typical=1)).structure
    assert {"from": "note:_/agent/Common-rules.md", "to": "note:Areas/Repo/Repo notes.md",
            "kind": "shared into local"} in st.into_areas
    source = open(ap.__file__, encoding="utf-8").read()
    assert "local into another local" in source and "leaf into another leaf" not in source


# ── The kept drawing: brought up to date, the same as drawn afresh ───


def _all_answers():
    """Every kind of call, as JSON: the overview, each typical agent, a work path, two backward questions."""
    calls = [path(), path(PathStart(typical=1)), path(PathStart(typical=3)),
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
