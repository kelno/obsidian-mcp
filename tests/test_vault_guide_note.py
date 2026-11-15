"""`get_vault_guide` serves the vault note `VAULT_GUIDE_NOTE` names.

A vault whose root `CLAUDE.md` holds only Claude Code imports keeps its
conventions in another note; the guide must serve that one, or claude.ai
agents get an import line. The default stays `CLAUDE.md`, with upstream's
"no guide" text unchanged. Offline: usage logging is stubbed and the vault
is a per-test tmp dir.
"""

import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.config import Settings  # noqa: E402

# Upstream's message for a vault without `CLAUDE.md`, pinned so the default
# path keeps serving it word for word.
UPSTREAM_NO_CLAUDE_MD = (
    "# Vault-Specific Conventions\n"
    "\n"
    "No `CLAUDE.md` found at the vault root. To teach the agent about your\n"
    "folder structure, file-naming conventions, tag taxonomy, required\n"
    "frontmatter fields, or task-management syntax, create a `CLAUDE.md`\n"
    "file at the root of your vault. The agent will pick it up automatically\n"
    "on the next call.\n"
    "\n"
    "Suggested sections:\n"
    "\n"
    "- **Folder structure** — what lives where, and where new notes belong.\n"
    "- **Naming conventions** — how filenames are formatted.\n"
    "- **Frontmatter** — required and conventional YAML fields.\n"
    "- **Tag taxonomy** — top-level tags and their meaning.\n"
    "- **Task syntax** — any GTD/Dataview/checklist conventions in use.\n"
)


@pytest.fixture(autouse=True)
def _no_usage_log(monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(tools, "_log_usage", _noop)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "vault_guide_note", "CLAUDE.md")
    return tmp_path


def _vault_section(guide: str) -> str:
    return guide.rsplit("\n\n---\n\n", 1)[1]


async def test_default_serves_the_root_claude_md(vault):
    (vault / "CLAUDE.md").write_text("Root conventions.\n", encoding="utf-8")
    guide = await tools.get_vault_guide_impl()
    assert _vault_section(guide) == "# Vault-Specific Conventions\n\nRoot conventions.\n"


async def test_default_without_claude_md_is_upstreams_message(vault):
    guide = await tools.get_vault_guide_impl()
    assert _vault_section(guide) == UPSTREAM_NO_CLAUDE_MD


async def test_a_configured_note_is_served_instead_of_claude_md(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_guide_note", "_/agent/Vault conventions.md")
    (vault / "CLAUDE.md").write_text("@_/agent/Common-rules.md\n", encoding="utf-8")
    (vault / "_" / "agent").mkdir(parents=True)
    (vault / "_" / "agent" / "Vault conventions.md").write_text(
        "---\ndescription: x\n---\nFolder rules.\n", encoding="utf-8")
    guide = await tools.get_vault_guide_impl()
    section = _vault_section(guide)
    assert "Folder rules." in section
    assert "Common-rules" not in section
    # The primer names the note it now comes from.
    assert "(loaded from `_/agent/Vault conventions.md` in the vault)" in guide
    assert "`CLAUDE.md` at the vault root" not in guide


async def test_the_primer_names_its_source_once():
    """`_primer_for` replaces a fixed phrase: if the primer is reworded, this
    fails instead of the guide quietly naming the wrong note."""
    assert tools._VAULT_GUIDE_PRIMER.count(tools._PRIMER_SOURCE) == 1


async def test_a_missing_configured_note_names_its_path(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_guide_note", "_/agent/Vault conventions.md")
    (vault / "CLAUDE.md").write_text("Root conventions.\n", encoding="utf-8")
    section = _vault_section(await tools.get_vault_guide_impl())
    assert "No `_/agent/Vault conventions.md` found in the vault." in section
    assert "create a `_/agent/Vault conventions.md`\nfile in your vault." in section
    # The root note is not served in its place: a silent fallback would hide
    # a wrong setting.
    assert "Root conventions." not in section


def _settings(**kw):
    return Settings(_env_file=None, secret_key="test", **kw)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_blank_means_claude_md(value):
    assert _settings(vault_guide_note=value).vault_guide_note == "CLAUDE.md"


@pytest.mark.parametrize("value", ["CLAUDE.md", "_/agent/Vault conventions.md", " Guide.MD "])
def test_vault_relative_notes_are_accepted(value):
    assert _settings(vault_guide_note=value).vault_guide_note == value.strip()


@pytest.mark.parametrize("value", [
    "/CLAUDE.md", "C:/vault/CLAUDE.md", "../CLAUDE.md", "a/../CLAUDE.md", "./CLAUDE.md",
    ".obsidian/guide.md", "a//b.md", "_\\agent\\guide.md", "CLAUDE.txt", "agent/",
])
def test_anything_else_stops_the_server(value):
    with pytest.raises(ValidationError, match="VAULT_GUIDE_NOTE"):
        _settings(vault_guide_note=value)
