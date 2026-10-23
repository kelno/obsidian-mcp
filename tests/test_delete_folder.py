"""`delete_folder`: remove an empty folder, and nothing else.

One `rmdir` through the parent descriptor `open_mutable` anchored, so the
kernel refuses a folder with any entry in it, hidden ones included. Refused for
files, the vault root, hidden paths, symlinks and traversal. Fully offline:
usage logging is stubbed and the vault is a tmp dir.
"""
import json

import pytest

from src.auth.session import current_user_id
from src.mcp_server import tools
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service

pytestmark = pytest.mark.asyncio


def refusal(result):
    return json.loads(result.rsplit("\n", 1)[-1].removeprefix("MCP-REFUSAL "))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    vault_service.clear_user_vault_cache()

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)
    permission = current_permission.set("readwrite")
    user = current_user_id.set(None)
    (tmp_path / "Projects" / "Old").mkdir(parents=True)
    yield tmp_path
    current_user_id.reset(user)
    current_permission.reset(permission)


async def test_removes_an_empty_folder(vault):
    result = await tools.delete_folder_impl("Projects/Old")
    assert result == "Deleted empty folder Projects/Old"
    assert not (vault / "Projects" / "Old").exists()
    assert (vault / "Projects").is_dir()
    assert not (vault / ".trash").exists()


async def test_a_trailing_slash_is_accepted(vault):
    result = await tools.delete_folder_impl("Projects/Old/")
    assert result.startswith("Deleted empty folder")
    assert not (vault / "Projects" / "Old").exists()


@pytest.mark.parametrize("entry", ["note.md", "pic.png", ".DS_Store", "Sub"])
async def test_a_folder_with_content_is_never_removed(vault, entry):
    folder = vault / "Projects" / "Old"
    if entry == "Sub":
        (folder / entry).mkdir()
    else:
        (folder / entry).write_bytes(b"x")
    result = await tools.delete_folder_impl("Projects/Old")
    assert refusal(result)["code"] == "folder_not_empty"
    assert "delete_note" in result and "delete_file" in result
    assert (folder / entry).exists()


async def test_a_missing_folder_is_not_found_and_creates_nothing(vault):
    result = await tools.delete_folder_impl("Nowhere/Gone")
    assert refusal(result)["code"] == "not_found"
    assert not (vault / "Nowhere").exists()


@pytest.mark.parametrize("name", ["note.md", "pic.png"])
async def test_a_file_is_refused_and_points_at_the_file_tools(vault, name):
    (vault / name).write_bytes(b"x")
    result = await tools.delete_folder_impl(name)
    assert refusal(result)["code"] == "invalid_argument"
    assert "delete_note" in result
    assert (vault / name).read_bytes() == b"x"


async def test_a_symlinked_folder_is_refused(vault):
    (vault / "alias").symlink_to(vault / "Projects" / "Old")
    result = await tools.delete_folder_impl("alias")
    assert refusal(result)["code"] == "validation_failed"
    assert (vault / "alias").is_symlink()
    assert (vault / "Projects" / "Old").is_dir()


@pytest.mark.parametrize("path", ["", ".", "/", ".obsidian", "Projects/.hidden", "../escape"])
async def test_the_root_hidden_and_escaping_paths_are_refused(vault, path):
    (vault / ".obsidian").mkdir()
    (vault / "Projects" / ".hidden").mkdir()
    result = await tools.delete_folder_impl(path)
    assert refusal(result)["code"] == "validation_failed"
    assert (vault / ".obsidian").is_dir()
    assert (vault / "Projects" / ".hidden").is_dir()


async def test_a_read_only_key_is_refused(vault):
    token = current_permission.set("read")
    try:
        result = await tools.delete_folder_impl("Projects/Old")
    finally:
        current_permission.reset(token)
    assert refusal(result)["code"] == "permission_denied"
    assert (vault / "Projects" / "Old").is_dir()
