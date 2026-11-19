"""The kelno_vault plugin's agent path tools: `agent_path` and `agent_path_reached_by`, on a small vault.

The reading rules themselves were checked against the PC prototype on the real vault (the done check in the
vault note "Agent paths - path tool"); these tests pin how the tools put the pieces together: what loads
first, the session-start hook and its role note, what opening the area brings, the served texts, hashes and
`known`, the size budget, private notes, and the backward question.
"""

import asyncio
import hashlib
import json

import pytest

from src.auth.session import current_user_id
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
        "3. A worker: Common-rules, the hook, Worker.\n"),
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
        "- [[_/agent/guides/coding-practices|coding-practices]]: any code work.\n"
        "- **Roles**: when in doubt, read [[_/agent/roles/Worker]].\n"
        "- Read [[_/agent/Notes kept]] first.\n"
        "- Whenever stuck, read [[_/agent/roles/Relay]].\n"
        "- The index's shape, as code: `[[_/agent/Memory reference]]: when`.\n"
        "```\n[[_/agent/Memory reference]]\n```\n"
        "- Old copies sit beside it, such as `Reference/Old/KDO~20261022.md`.\n"
        "- Before filing, read `_/agent/Gone.md`.\n"
        "- See [[_/agent/Missing]] too.\n", encoding="utf-8")
    result = path(PathStart(surface="claude-ai"))
    by_id = {p.id: p for p in result.pieces}
    assert by_id["note:_/agent/Vault conventions.md"].moment == "When working on one of the owner's areas"
    coordinator = by_id["note:_/agent/roles/Coordinator.md"]
    assert coordinator.moment == "While the setup is on trial" and coordinator.always
    assert by_id["note:_/agent/guides/coding-practices.md"].moment == "any code work"
    # A bold label in front, a bare "read", another opening word: mentions, not on the path. A link written as
    # code isn't a link at all.
    for gone in ("roles/Worker.md", "Notes kept.md", "roles/Relay.md"):
        assert f"note:_/agent/{gone}" not in by_id
    assert {m["target"] for m in result.structure.rule_file_mentions} == {
        "_/agent/roles/Worker.md", "_/agent/Notes kept.md", "_/agent/roles/Relay.md"}
    assert result.structure.mentions_not_followed == 3
    # A link to nothing is reported; an example path in a plain sentence isn't a pointer.
    assert {b["target"] for b in result.structure.broken_pointers} == {"_/agent/Gone.md", "_/agent/Missing"}
    # A hook's message isn't written under the convention: its plain "Read the vault note …" still points.
    assert "note:_/agent/roles/Worker.md" in ids(path(PathStart(typical=3)))
