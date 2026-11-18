"""Private folders (fork, P98): one definition, one argument, off by default.

`src/services/private_folders.py` decides what is private from
`PRIVATE_FOLDER_NAMES`; `include_private` opens it on the searches, the task
list and the ledger's citing lines. With the setting empty (every
deployment's default) nothing is private and no tool shows the argument:
`_drop_include_private` in `src/mcp_server/server.py` removes it, through the
same SDK internals `_forbid_unknown_arguments` uses, so this module pins them
too. The searches themselves run against a real database in
`tests/integration/test_private_folders_pg.py`. `hybrid_search` filters and
counts the same way while it is switched off (`server.SWITCHED_OFF`), so it
is ready when the owner switches it on.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

from src.config import Settings, settings  # noqa: E402
from src.mcp_server import server, tools  # noqa: E402
from src.services import hybrid, private_folders  # noqa: E402
from src.services.hybrid import HybridResult, HybridRow  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: Every served tool that takes `include_private` when private folders are configured.
PRIVATE_TOOLS = {
    "keyword_search", "semantic_search", "hybrid_search", "find_related", "list_tasks", "ledger_get",
    "ledger_update",
}
#: Switched-off upstream tools that take it too, once switched on.
SWITCHED_OFF_PRIVATE_TOOLS: set = set()


@pytest.fixture
def owner_setting(monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])


# ── the definition ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path, private", [
    ("Diary (private)/2026-11-13.md", True),
    ("Areas/Health/People (Private)/Sam.md", True),          # any depth, any case
    ("A/Diary (PRIVATE)/deeper/still/n.md", True),            # everything under it
    ("(private)/n.md", False),                                # `* (private)` needs the space
    (" (private)/n.md", True),                                # `*` may be empty
    ("Notes/Plans (private).md", False),                      # a file name is not a folder
    ("Notes/Diary (private)-old/n.md", False),                # the whole folder name must match
    ("Notes/Diary (privates)/n.md", False),
    ("Diary (private)", False),                               # no folder in a bare name
    ("Diary (private)\\n.md", True),                          # backslashes read as separators
])
def test_is_private_follows_the_folder_names(owner_setting, path, private):
    assert private_folders.is_private(path) is private


def test_nothing_is_private_without_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", [])
    assert not private_folders.enabled()
    assert private_folders.is_private("Diary (private)/n.md") is False
    assert private_folders.path_regex() is None
    with pytest.raises(RuntimeError):
        private_folders.sql_predicate(None)


def test_globs_are_literal_but_for_star_and_question_mark(monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", ["a.b+[c]", "x?z", "Секрет*"])
    assert private_folders.is_private("a.b+[c]/n.md")
    assert not private_folders.is_private("aXb+[c]/n.md")          # `.` is a dot, not any character
    assert private_folders.is_private("Q/xyz/n.md") and not private_folders.is_private("Q/xz/n.md")
    assert private_folders.is_private("секретное/n.md")             # non-ASCII, any case
    assert not private_folders.is_private("x/yz/n.md")               # `?` never crosses `/`


def test_the_regex_reads_the_same_in_postgres():
    # Only constructs PostgreSQL's ARE reads like Python's `re`: no `(?`, no
    # escaped letter or digit (an ARE class escape such as `\d`).
    import re
    regex = private_folders._regex_for(("a.b+[c] d", "x?z*"))
    assert "(?" not in regex and not re.search(r"\\[A-Za-z0-9]", regex)


def test_the_setting_parses_like_the_other_glob_lists():
    parsed = Settings(_env_file=None, secret_key="x" * 40,
                      private_folder_names='["* (private)", " /Secret* "]').private_folder_names
    assert parsed == ["* (private)", "Secret*"]
    assert Settings(_env_file=None, secret_key="x" * 40,
                    private_folder_names="* (private),Other").private_folder_names == ["* (private)", "Other"]


def test_a_slash_in_a_name_is_trimmed_or_refused():
    # Review: `Private/` used to match nothing at all, silently.
    trimmed = Settings(_env_file=None, secret_key="x" * 40, private_folder_names='["Private/", "/Diary/"]')
    assert trimmed.private_folder_names == ["Private", "Diary"]
    with pytest.raises(ValueError, match="folder names, not paths"):
        Settings(_env_file=None, secret_key="x" * 40, private_folder_names='["Areas/Health"]')


def test_the_hidden_line_is_the_owner_s_wording():
    assert private_folders.hidden_line(2) == "2 matches in private folders not shown."
    assert private_folders.hidden_line(1) == "1 match in private folders not shown."
    assert tools._private_hidden_suffix({"private_hidden": 2}) == "\n\n2 matches in private folders not shown."
    assert tools._private_hidden_suffix({}) == "" and tools._private_hidden_suffix({"private_hidden": 0}) == ""


def test_related_hidden_count_counts_only_what_would_have_been_listed():
    class R:
        def __init__(self, note_id, distance):
            self.note_id, self.distance = note_id, distance

    shown = [R(1, 0.2), R(1, 0.1), R(2, 0.3)]
    assert tools._related_hidden_count(shown, {7: 0.05, 8: 0.25, 9: 0.9}, limit=2) == 2   # ahead of 0.3
    assert tools._related_hidden_count(shown, {7: 0.05, 8: 0.25, 9: 0.9}, limit=5) == 3   # not filled: all
    assert tools._related_hidden_count([], {7: 0.1, 8: 0.2}, limit=1) == 1                # at most limit


# ── hybrid_search's count: the private notes the unfiltered fusion ranks ahead ─


def test_the_hybrid_count_is_the_private_notes_ranked_ahead_of_the_last_shown():
    # Unfiltered: s1 is in both arms (2/61), p1 and p2 one each (1/62): s1, p1, p2.
    keyword, semantic, private = ["s1", "p1"], ["s1", "p2"], {"s1"}
    count = hybrid.count_private_hidden
    assert count(["p1"], keyword, semantic, private, k=60, limit=1) == 1
    assert count(["p1", "p2"], keyword, semantic, private, k=60, limit=2) == 1
    assert count(["p1", "p2"], keyword, semantic, private, k=60, limit=5) == 1     # not full: every one
    # Nothing private ahead of the last shown.
    assert count(["p1"], ["p1", "s9"], [], {"s9"}, k=60, limit=1) == 0
    # The furthest shown row counts, in the unfiltered order, not the page's own.
    assert count(["p2", "p1"], ["p1", "s1", "p2"], [], {"s1"}, k=60, limit=2) == 1
    # A shown row past both unfiltered arms: every private note was ahead of it, at most limit.
    assert count(["x"], ["s1", "s2"], ["s3"], {"s1", "s2", "s3"}, k=60, limit=1) == 1
    assert count(["x", "y"], ["s1", "s2"], ["s3"], {"s1", "s2", "s3"}, k=60, limit=2) == 2
    assert count([], [], [], set(), k=60, limit=10) == 0


# ── hybrid_search's statements, on upstream's scripted session ───────────────


@pytest.fixture
def scripted(monkeypatch):
    """Upstream's offline setting for `tests/test_hybrid_search_offline.py`'s
    scripted session (its autouse fixture is not imported here)."""
    from tests import test_hybrid_search_offline as upstream

    monkeypatch.setattr("src.services.search.combined_tsquery", lambda _q: "tsq")
    monkeypatch.setattr(settings, "fts_configs", ["simple"], raising=False)
    monkeypatch.setattr(settings, "keyword_snippet_rows", 20, raising=False)

    async def _embed(_text, **_kw):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr("src.services.embeddings.get_embedding", _embed)
    return upstream


def _hybrid_log(upstream, *, ranked, semantic, hide_private, limit=10):
    import asyncio

    from src.services import timing

    session = upstream._Session(ranked=ranked, semantic=semantic,
                                snippet=[[upstream.snip(1, hl="x \x02y\x03")]])
    token = timing.begin()
    try:
        result = asyncio.run(hybrid.hybrid_search(session, "q", limit=limit, hide_private=hide_private))
        holder = dict(timing.current())
    finally:
        timing.clear(token)
    return result, session.log, holder


def test_hybrid_counts_after_upstream_s_last_statement(scripted, owner_setting):
    up = scripted
    result, log, holder = _hybrid_log(
        up, hide_private=True,
        ranked=[[up.kw_row(1, "a.md")], [up.kw_row(1, "a.md"), up.kw_row(4, "Diary (private)/q.md")]],
        # The filtered scan finds nothing and re-runs exact; the unfiltered one does not.
        semantic=[[], [up.sem_row(2, "b.md")], [up.sem_row(3, "Diary (private)/p.md")]],
    )
    sqls = [sql for _, sql in log]
    kinds = [k for k, _ in log]
    filtered = ["~*" in sql for sql in sqls]
    semantic = [i for i, k in enumerate(kinds) if k == "semantic"]
    off, on = sqls.index("SET LOCAL enable_indexscan = off"), sqls.index("SET LOCAL enable_indexscan = on")
    # Upstream's step 5: the filtered scan, off, its identical re-run, on.
    assert semantic[:2] == [off - 1, off + 1] and on == off + 2
    assert filtered[semantic[0]] and filtered[semantic[1]]
    # The unfiltered runs come after every upstream statement (the snippets,
    # the transition count), keyword first, with the index scans back on.
    upstream_last = max(i for i, k in enumerate(kinds) if k in ("snippet", "rechunk"))
    ranked = [i for i, k in enumerate(kinds) if k == "ranked"]
    assert filtered[ranked[0]] and not filtered[ranked[1]]
    assert upstream_last < ranked[1] < semantic[2] and not filtered[semantic[2]]
    assert sqls.count("SET LOCAL enable_indexscan = on") == 1, "no exact re-run of its own, nothing to restore"
    assert [r.path for r in result.rows] == ["a.md", "b.md"]
    assert result.private_hidden == 2
    assert holder["exact_fallback"] is True


def test_an_exact_rerun_of_the_unfiltered_scan_is_restored(scripted, owner_setting):
    up = scripted
    result, log, holder = _hybrid_log(
        up, hide_private=True,
        ranked=[[up.kw_row(1, "a.md")], [up.kw_row(1, "a.md")]],
        semantic=[[up.sem_row(2, "b.md")], [], [up.sem_row(3, "Diary (private)/p.md")]],
    )
    sqls = [sql for _, sql in log]
    # The count's last statements: the unfiltered scan, off, its re-run, on.
    assert sqls[-1] == "SET LOCAL enable_indexscan = on", "nothing after the count inherits the exact plan"
    assert sqls[-2] == sqls[-4] and "~*" not in sqls[-2], "the identical unfiltered re-run"
    assert sqls[-3] == "SET LOCAL enable_indexscan = off"
    assert holder["exact_fallback"] is False, "the telemetry is about the arm shown"
    assert result.private_hidden == 1


def test_without_hiding_the_statements_are_upstream_s(scripted, monkeypatch):
    up = scripted

    def statements(names, hide):
        monkeypatch.setattr(settings, "private_folder_names", names)
        result, log, _ = _hybrid_log(up, hide_private=hide, ranked=[[up.kw_row(1, "a.md")]],
                                     semantic=[[], [up.sem_row(2, "b.md")]])
        assert result.private_hidden is None
        return [sql for _, sql in log]

    upstream_statements = statements([], False)
    assert statements([], True) == upstream_statements                   # no private folders
    assert statements(["* (private)"], False) == upstream_statements     # include_private=true


def test_the_hybrid_page_keeps_the_private_line_inside_the_response_cap(monkeypatch):
    rows = [HybridRow(path=f"{i:02d}-" + "p" * 80 + ".md", title="t" * 80, tags=[], score=1 / 61,
                      keyword_rank=1, match="all") for i in range(50)]

    def result(hidden):
        return HybridResult(rows=list(rows), rrf_k=60, keyword_rows=50, keyword_all=50, semantic_rows=0,
                            semantic_chunks=0, snippet_cap=20, private_hidden=hidden)

    monkeypatch.setattr(settings, "max_read_response_chars", 1500, raising=False)
    text, paths = tools._hybrid_response("q", result(2))
    assert len(text) <= 1500
    assert text.endswith("\n\n2 matches in private folders not shown.")
    assert paths and paths == [r.path for r in rows[: len(paths)]]
    # Without hidden notes (none, or no count at all) the page is the renderer's own.
    for hidden in (0, None):
        assert tools._hybrid_response("q", result(hidden)) == tools.render_hybrid_results("q", result(hidden))


# ── the argument is there only with the setting ──────────────────────────────


def test_no_tool_shows_include_private_by_default():
    assert settings.private_folder_names == []
    for tool in server.mcp._tool_manager.list_tools():
        assert "include_private" not in tool.parameters.get("properties", {}), tool.name
        assert "include_private" not in tool.fn_metadata.arg_model.model_fields, tool.name
        assert "include_private" not in (tool.description or ""), tool.name


@pytest.mark.parametrize("name", sorted(PRIVATE_TOOLS | SWITCHED_OFF_PRIVATE_TOOLS))
async def test_passing_it_anyway_is_refused_by_default(name):
    tool = server.mcp._tool_manager.get_tool(name)
    with pytest.raises(ToolError) as exc:
        await tool.run({"include_private": True})
    assert "include_private" in str(exc.value)


async def test_dropping_keeps_the_other_arguments_working():
    mcp = FastMCP("t")
    seen = []

    @mcp.tool()
    async def probe(query: str, limit: int = 5, include_private: bool = False) -> str:
        """Probe."""
        seen.append((query, limit, include_private))
        return "ok"

    server._drop_include_private(mcp)
    server._forbid_unknown_arguments(mcp)
    tool = mcp._tool_manager.get_tool("probe")
    assert set(tool.parameters["properties"]) == {"query", "limit"}
    assert tool.parameters["required"] == ["query"]
    assert await tool.run({"query": "x", "limit": 2}) == "ok"
    assert seen == [("x", 2, False)]
    with pytest.raises(ToolError):
        await tool.run({"query": "x", "include_private": True})


_ENABLED_SCRIPT = textwrap.dedent(
    """
    import json
    from src.mcp_server import server, tools

    def schemas():
        out = {}
        for tool in server.mcp._tool_manager.list_tools():
            prop = tool.parameters.get("properties", {}).get("include_private")
            if prop is not None:
                out[tool.name] = {"prop": prop, "field": "include_private" in tool.fn_metadata.arg_model.model_fields}
        return out

    served = schemas()
    server.switch_on()
    print("SCHEMAS=" + json.dumps({"served": served, "switched_on": schemas(),
                                   "guide": tools._private_folders_guide()}))
    """
)


def _enabled_schemas(tmp_path) -> dict:
    """What a server started with `PRIVATE_FOLDER_NAMES` shows: the served
    tools' schemas, the same with every switched-off tool back on, and the
    guide's section."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT),
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "SECRET_KEY": "test",
        "VAULT_PATH": str(tmp_path),
        "EMBEDDING_ALLOW_PLAINTEXT": "true",
        "PRIVATE_FOLDER_NAMES": '["* (private)"]',
    }
    result = subprocess.run([sys.executable, "-c", _ENABLED_SCRIPT], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    import json
    return json.loads(result.stdout.split("SCHEMAS=", 1)[1])


@pytest.fixture(scope="module")
def enabled(tmp_path_factory):
    return _enabled_schemas(tmp_path_factory.mktemp("enabled"))


def test_with_the_setting_the_seven_served_tools_take_it(enabled):
    assert set(enabled["served"]) == PRIVATE_TOOLS
    assert set(enabled["switched_on"]) == PRIVATE_TOOLS | SWITCHED_OFF_PRIVATE_TOOLS
    for name, got in enabled["switched_on"].items():
        prop = got["prop"]
        assert got["field"] and prop["type"] == "boolean" and prop["default"] is False, name
        description = prop["description"]
        # Says which folders, when to set it, and (searches) the line it replaces.
        assert "`* (private)`" in description, name
        assert "only when the owner asked for something in a private folder" in description, name
        if name in ("keyword_search", "semantic_search", "hybrid_search", "find_related"):
            assert "2 matches in private folders not shown." in description


def test_the_guide_names_exactly_the_served_tools_that_take_it(enabled):
    # Switching hybrid_search on without naming it in the guide's section
    # (or naming it while it is off) fails here.
    import re
    named = set(re.findall(r"`([a-z_]+)`", enabled["guide"]))
    assert named & set(enabled["switched_on"]) == set(enabled["served"])


# ── the guide ────────────────────────────────────────────────────────────────


def test_the_guide_has_a_private_folders_section_only_with_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", [])
    assert tools._private_folders_guide() == ""
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])
    section = tools._private_folders_guide()
    assert section.startswith("\n\n## Private folders (this server)\n\n")
    assert "`* (private)`" in section and "include_private=true" in section
    assert "2 matches in private folders not shown." in section
