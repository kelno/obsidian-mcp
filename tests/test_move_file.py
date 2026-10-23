"""`move_file`: the raw-file peer of `move_note`.

One non-replacing rename between two `open_mutable` targets, refused for
markdown at either end, for links, directories, hidden paths and an existing
destination, with the same optional `expected_hash` binding as the other raw
file tools. Fully offline: usage logging is stubbed and the vault is a tmp dir.
"""
import hashlib
import json
import os

import pytest

from src.auth.session import current_user_id
from src.mcp_server import tools
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\npixels"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def refusal(result):
    return json.loads(result.rsplit("\n", 1)[-1].removeprefix("MCP-REFUSAL "))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "write_precondition_required", False)
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    vault_service.clear_user_vault_cache()

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)
    permission = current_permission.set("readwrite")
    user = current_user_id.set(None)
    (tmp_path / "Attachments").mkdir()
    (tmp_path / "Attachments" / "pic.png").write_bytes(PNG)
    yield tmp_path
    current_user_id.reset(user)
    current_permission.reset(permission)


def unchanged(vault):
    assert (vault / "Attachments" / "pic.png").read_bytes() == PNG


async def test_moves_a_file_and_creates_parents(vault):
    result = await tools.move_file_impl("Attachments/pic.png", "Archive/2026/pic.png")
    assert result.startswith("Moved Attachments/pic.png to Archive/2026/pic.png")
    assert "MCP-REFUSAL" not in result
    assert (vault / "Archive" / "2026" / "pic.png").read_bytes() == PNG
    assert not (vault / "Attachments" / "pic.png").exists()


async def test_renames_in_place_and_keeps_the_inode(vault):
    before = os.stat(vault / "Attachments" / "pic.png").st_ino
    await tools.move_file_impl("Attachments/pic.png", "Attachments/renamed.png")
    assert os.stat(vault / "Attachments" / "renamed.png").st_ino == before


async def test_an_existing_destination_is_never_replaced(vault):
    (vault / "other.png").write_bytes(b"other")
    result = await tools.move_file_impl("Attachments/pic.png", "other.png")
    assert refusal(result)["code"] == "already_exists"
    assert (vault / "other.png").read_bytes() == b"other"
    unchanged(vault)


@pytest.mark.parametrize(
    "from_path,to_path",
    [
        ("note.md", "moved.png"),
        ("Attachments/pic.png", "pic.md"),
        ("Attachments/pic.png", "pic.MD"),
        ("Attachments/pic.png", "Notes/pic.md/."),
    ],
)
async def test_markdown_at_either_end_points_at_move_note(vault, from_path, to_path):
    (vault / "note.md").write_text("body\n", encoding="utf-8")
    result = await tools.move_file_impl(from_path, to_path)
    if refusal(result)["code"] == "invalid_argument":
        assert "move_note" in result
    assert (vault / "note.md").read_text(encoding="utf-8") == "body\n"
    assert not (vault / "pic.md").exists()
    assert not (vault / "pic.MD").exists()
    unchanged(vault)


async def test_a_missing_source_is_not_found(vault):
    result = await tools.move_file_impl("Attachments/nope.png", "x.png")
    assert refusal(result)["code"] == "not_found"
    assert not (vault / "x.png").exists()


async def test_a_directory_is_refused(vault):
    result = await tools.move_file_impl("Attachments", "Elsewhere")
    assert refusal(result)["code"] == "unsafe_path"
    unchanged(vault)
    assert not (vault / "Elsewhere").exists()


async def test_a_symlinked_source_is_refused(vault):
    (vault / "alias.png").symlink_to(vault / "Attachments" / "pic.png")
    result = await tools.move_file_impl("alias.png", "moved.png")
    assert "MCP-REFUSAL" in result
    assert (vault / "alias.png").is_symlink()
    assert not (vault / "moved.png").exists()
    unchanged(vault)


@pytest.mark.parametrize("to_path", [".hidden/pic.png", "Attachments/.pic.png", "../escape.png"])
async def test_hidden_and_escaping_destinations_are_refused(vault, to_path):
    result = await tools.move_file_impl("Attachments/pic.png", to_path)
    assert refusal(result)["code"] == "validation_failed"
    unchanged(vault)


async def test_a_read_only_key_is_refused(vault):
    token = current_permission.set("read")
    try:
        result = await tools.move_file_impl("Attachments/pic.png", "moved.png")
    finally:
        current_permission.reset(token)
    assert refusal(result)["code"] == "permission_denied"
    unchanged(vault)


@pytest.mark.parametrize(
    "expected,code", [(digest(b"old"), "stale_precondition"), ("ABC", "malformed_precondition")]
)
async def test_a_stale_or_malformed_hash_refuses_the_move(vault, expected, code):
    result = await tools.move_file_impl("Attachments/pic.png", "moved.png", expected_hash=expected)
    event = refusal(result)
    assert event["code"] == code
    if code == "stale_precondition":
        assert event["current_hash"] == digest(PNG)
    assert not (vault / "moved.png").exists()
    unchanged(vault)


async def test_a_matching_hash_moves_and_still_binds_the_destination(vault):
    result = await tools.move_file_impl("Attachments/pic.png", "moved.png", expected_hash=digest(PNG))
    assert result.startswith("Moved")
    metadata = await tools.read_file_impl("moved.png", hash_only=True)
    assert metadata.endswith("content_hash: " + digest(PNG))


async def test_required_mode_refuses_a_move_without_a_hash(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "write_precondition_required", True)
    result = await tools.move_file_impl("Attachments/pic.png", "moved.png")
    assert refusal(result)["code"] == "precondition_required"
    unchanged(vault)
