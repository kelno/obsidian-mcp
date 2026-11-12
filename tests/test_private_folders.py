"""Private folders (fork, P98): one definition, one argument, off by default.

`src/services/private_folders.py` decides what is private from
`PRIVATE_FOLDER_NAMES`; `include_private` opens it on the searches, the task
list and the ledger's citing lines. With the setting empty (every
deployment's default) nothing is private and no tool shows the argument:
`_drop_include_private` in `src/mcp_server/server.py` removes it, through the
same SDK internals `_forbid_unknown_arguments` uses, so this module pins them
too. The searches themselves run against a real database in
`tests/integration/test_private_folders_pg.py`.
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
from src.services import private_folders  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

#: Every tool that takes `include_private` when private folders are configured.
PRIVATE_TOOLS = {"keyword_search", "semantic_search", "find_related", "list_tasks", "ledger_get", "ledger_update"}


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


# ── the argument is there only with the setting ──────────────────────────────


def test_no_tool_shows_include_private_by_default():
    assert settings.private_folder_names == []
    for tool in server.mcp._tool_manager.list_tools():
        assert "include_private" not in tool.parameters.get("properties", {}), tool.name
        assert "include_private" not in tool.fn_metadata.arg_model.model_fields, tool.name
        assert "include_private" not in (tool.description or ""), tool.name


@pytest.mark.parametrize("name", sorted(PRIVATE_TOOLS))
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
    from src.mcp_server.server import mcp
    out = {}
    for tool in mcp._tool_manager.list_tools():
        prop = tool.parameters.get("properties", {}).get("include_private")
        if prop is not None:
            out[tool.name] = {"prop": prop, "field": "include_private" in tool.fn_metadata.arg_model.model_fields}
    print("SCHEMAS=" + json.dumps(out))
    """
)


def test_with_the_setting_the_six_tools_take_it(tmp_path):
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
    schemas = json.loads(result.stdout.split("SCHEMAS=", 1)[1])
    assert set(schemas) == PRIVATE_TOOLS
    for name, got in schemas.items():
        prop = got["prop"]
        assert got["field"] and prop["type"] == "boolean" and prop["default"] is False, name
        description = prop["description"]
        # Says which folders, when to set it, and (searches) the line it replaces.
        assert "`* (private)`" in description, name
        assert "only when the owner asked for something in a private folder" in description, name
        if name in ("keyword_search", "semantic_search", "find_related"):
            assert "2 matches in private folders not shown." in description


# ── the guide ────────────────────────────────────────────────────────────────


def test_the_guide_has_a_private_folders_section_only_with_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", [])
    assert tools._private_folders_guide() == ""
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])
    section = tools._private_folders_guide()
    assert section.startswith("\n\n## Private folders (this server)\n\n")
    assert "`* (private)`" in section and "include_private=true" in section
    assert "2 matches in private folders not shown." in section
