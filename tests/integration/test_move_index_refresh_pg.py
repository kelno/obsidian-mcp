"""`move_note(rewrite_links=True)` brings the link index up to date first.

The defect, found live on 2026-11-09: the move finds the notes to rewrite in
`note_links`, which only an index pass writes. A note that linked to the moved
one but was written since the last pass had no row, so the move left its link
at the old path and reported "rewrote 0 link(s)" as a success.

Driven against a real PostgreSQL: the vault is indexed once, a new linking
note is then written straight to disk (no pass sees it), and the move must
still rewrite it — because the move ran the index stage itself.
"""

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.mcp_server.auth import current_permission
from src.services import embeddings as embeddings_service
from src.services import indexer, vault_fs
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("move_index_refresh", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    for target in (settings, tools.settings, indexer.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        await session.commit()
    permission = current_permission.set("readwrite")
    vault_fs.reset_filesystem_probe_cache()
    try:
        yield root
    finally:
        current_permission.reset(permission)
        vault_fs.reset_filesystem_probe_cache()


async def test_a_link_written_since_the_last_pass_is_rewritten(vault):
    (vault / "Foo.md").write_text("body\n", encoding="utf-8")
    (vault / "Old.md").write_text("see [[Foo]]\n", encoding="utf-8")
    await indexer.index_vault(user_id=None)

    # Written after the pass: no `note_links` row exists for it yet.
    (vault / "New.md").write_text(
        "[[Foo|Foo]] and [[Foo|my label]] and [Foo](Foo.md)\n", encoding="utf-8"
    )

    result = await tools.move_note_impl("Foo.md", "Bar.md", rewrite_links=True)

    assert "rewrote 4 link(s) across 2 note(s)" in result, result
    assert (vault / "Old.md").read_text(encoding="utf-8") == "see [[Bar]]\n"
    assert (vault / "New.md").read_text(encoding="utf-8") == (
        "[[Bar|Bar]] and [[Bar|my label]] and [Bar](Bar.md)\n"
    )


async def test_a_note_created_since_the_last_pass_can_be_moved_with_its_backlinks(vault):
    """Both ends new: the moved note itself has no row before the refresh."""
    await indexer.index_vault(user_id=None)
    (vault / "Fresh.md").write_text("body\n", encoding="utf-8")
    (vault / "Linker.md").write_text("see [[Fresh]]\n", encoding="utf-8")

    result = await tools.move_note_impl("Fresh.md", "Sub/Fresh2.md", rewrite_links=True)

    assert "rewrote 1 link(s) across 1 note(s)" in result, result
    assert (vault / "Linker.md").read_text(encoding="utf-8") == "see [[Fresh2]]\n"


async def test_a_note_the_refresh_cannot_read_still_counts_as_a_backlink(vault, monkeypatch):
    """A note the refresh fails to read does not refuse the move (a dangling
    symlink would otherwise block every rewrite move for good): its row keeps
    the links an earlier pass extracted, so it is still found and rewritten."""
    (vault / "Foo.md").write_text("body\n", encoding="utf-8")
    (vault / "Flaky.md").write_text("see [[Foo]]\n", encoding="utf-8")
    await indexer.index_vault(user_id=None)
    (vault / "Flaky.md").write_text("see [[Foo]] again\n", encoding="utf-8")

    real_read = indexer._read_and_hash

    def failing_read(parent_fd, name, rel):
        if rel == "Flaky.md":
            raise OSError(5, "Input/output error")
        return real_read(parent_fd, name, rel)

    monkeypatch.setattr(indexer, "_read_and_hash", failing_read)

    result = await tools.move_note_impl("Foo.md", "Bar.md", rewrite_links=True)

    assert "Move aborted" not in result, result
    assert (vault / "Bar.md").exists()
    assert (vault / "Flaky.md").read_text(encoding="utf-8") == "see [[Bar]] again\n"


async def test_a_move_without_rewrite_runs_no_refresh(vault, monkeypatch):
    (vault / "Foo.md").write_text("body\n", encoding="utf-8")

    async def must_not_run(*args, **kwargs):
        raise AssertionError("the refresh ran for a move without rewrite_links")

    monkeypatch.setattr(indexer, "refresh_index_for_move", must_not_run)

    result = await tools.move_note_impl("Foo.md", "Bar.md")

    assert "Moved" in result or "moved" in result, result
    assert (vault / "Bar.md").exists()
