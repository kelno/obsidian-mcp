"""`index_status` against a real PostgreSQL: every embedding state counted once.

The counts are SQL over `notes_metadata`, `note_embeddings`, `ledger_entries`
and `indexer_runs`, so they are checked here on rows set up in each state:
embedded (one only in part), pending (never embedded, changed since, in a
private folder), excluded by pattern, and empty; a folder, private folders,
the ledger, and the rate from the pass history.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.auth.session import current_user_id
from src.config import settings
from src.mcp_server import tools
from src.mcp_server.auth import current_permission
from src.services import indexer, ledger_embed
from src.services import vault as vault_service
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)

# (path, content_hash, embedded_content_hash, vectors, chunks_truncated)
NOTES = [
    ("Home.md", "h0", "h0", 1, False),  # embedded; carries the area tag for the ledger entry
    ("a.md", "h1", "h1", 2, False),  # embedded
    ("b.md", "h2", "h2", 1, True),  # embedded, beginning only
    ("c.md", "h3", None, 0, False),  # pending: never embedded
    ("d.md", "h4-new", "h4-old", 1, False),  # pending: changed since (old vectors kept)
    ("_x/e.md", "h5", "h5", 0, False),  # excluded by pattern
    ("f.md", "h6", "h6", 0, False),  # no text
    ("Secret (private)/g.md", "h7", None, 0, False),  # pending, private
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("index_status", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for target in (settings, indexer.settings, tools.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    monkeypatch.setattr(settings, "embedding_exclude_patterns", ["_x/*"])
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])
    for module in (indexer, src.database, tools, ledger_embed):
        monkeypatch.setattr(module, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(ledger_embed, "enqueue", lambda entry_id: True)  # left pending on purpose
    vault_service.clear_user_vault_cache()

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)
    now = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL ledger.allow_delete = 'on'"))
        for table in ("ledger_revisions", "ledger_entry_topics", "ledger_entry_links", "ledger_embeddings",
                      "ledger_entries", "ledger_topics", "ledger_ref_counters",
                      "note_embeddings", "notes_metadata", "indexer_runs"):
            await session.execute(text(f"DELETE FROM {table}"))
        for path, h, eh, vectors, truncated in NOTES:
            frontmatter = '{"area_tag": "area/home"}' if path == "Home.md" else "{}"
            note_id = (await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, tags, frontmatter, content_hash, "
                "embedded_content_hash, chunks_truncated) VALUES (NULL, :p, 't', '{}', CAST(:fm AS jsonb), :h, :eh, :t) "
                "RETURNING id"), {"p": path, "fm": frontmatter, "h": h, "eh": eh, "t": truncated})).scalar()
            for i in range(vectors):
                await session.execute(text(
                    "INSERT INTO note_embeddings (note_id, chunk_index, chunk_text, embedding) "
                    "VALUES (:id, :i, 'x', :v)"), {"id": note_id, "i": i, "v": str([0.5] * DIM)})
        # Six notes embedded ten minutes ago count; fifty three hours ago don't.
        for finished, embedded in ((now - timedelta(minutes=10), 6), (now - timedelta(hours=3), 50)):
            await session.execute(text(
                "INSERT INTO indexer_runs (started_at, finished_at, trigger, user_id, notes_embedded) "
                "VALUES (:s, :f, 'scheduled', NULL, :n)"), {"s": finished - timedelta(minutes=1), "f": finished,
                                                             "n": embedded})
        await session.commit()
    permission = current_permission.set("readwrite")
    user = current_user_id.set(None)
    added = await tools.ledger_add_impl("quirk", "Paint dries slowly", area="home", details={"certainty": "tested"})
    assert added.error is None, added.error
    yield root
    current_user_id.reset(user)
    current_permission.reset(permission)


async def test_counts_every_state_and_the_rate(vault):
    result = await tools.index_status_impl()
    assert result.error is None, result.error
    v = result.vault
    assert (v.notes, v.embedded, v.pending, v.excluded, v.no_text, v.beginning_only) == (8, 3, 3, 1, 1, 1)
    assert v.embedded + v.pending + v.excluded + v.no_text == v.notes
    p = result.private_folders
    assert (p.notes, p.pending, p.embedded) == (1, 1, 0)
    assert (result.ledger.entries, result.ledger.pending) == (1, 1)
    assert result.progress.embedded_last_hour == 6
    assert result.progress.estimated_minutes_left == 30  # 3 pending at 6 an hour
    assert result.progress.last_embedded_at is not None
    assert result.progress.paused is False and result.progress.embedding_now is False
    assert result.folder is None and result.folder_counts is None
    assert result.checked_at is not None
    assert "8 notes indexed: 3 embedded, 3 pending, 2 never embedded" in result.summary
    assert "about 30 min to go" in result.summary and "1 of 1 entries pending" in result.summary


async def test_a_folder_gets_its_own_counts(vault):
    result = await tools.index_status_impl(folder="/Secret (private)/")
    assert result.folder == "Secret (private)/"
    assert (result.folder_counts.notes, result.folder_counts.pending) == (1, 1)
    assert result.private_folders is None  # the folder is the question
    assert result.vault.notes == 8

    # A prefix match is on whole folder names: "_x" is not "_xy".
    nowhere = await tools.index_status_impl(folder="_xy")
    assert nowhere.folder_counts.notes == 0
    assert "No indexed note under _xy/" in nowhere.notes[0]


async def test_nothing_embedded_lately_has_no_estimate_and_says_why(vault, sessionmaker):
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM indexer_runs"))
        await session.commit()
    result = await tools.index_status_impl()
    assert result.progress.embedded_last_hour == 0
    assert result.progress.estimated_minutes_left is None and result.progress.last_embedded_at is None
    assert "Nothing was embedded in the last hour." in result.summary
    assert any("none was embedded in the last hour" in n for n in result.notes)
