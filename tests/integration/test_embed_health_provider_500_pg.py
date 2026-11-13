"""The indexer's health must catch embeddings failing over a long time.

Owner's report, 2026-11-13: the embedding server answered HTTP 500 (it pointed
at a PC that was down), two notes stayed pending, and `index_status` still
said `health.status` "ok", `embedding_failing` false. These tests drive the
real index and embed stages on a real PostgreSQL, with the embedding provider
answering 500 at the HTTP level, and read `indexer_health.snapshot()` the way
`/health` and `index_status` do.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.services import embeddings as embeddings_service
from src.services import indexer, indexer_health
from src.services import vault as vault_service
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = 8


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("embed_health_500", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


class _Ollama:
    """An `/api/embed` endpoint answering 500 while `down`, vectors otherwise."""

    def __init__(self):
        self.down = True
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.down:
            return httpx.Response(500, text="Internal Server Error")
        import json

        inputs = json.loads(request.content)["input"]
        return httpx.Response(
            200, json={"embeddings": [[1.0] + [0.0] * (DIM - 1) for _ in inputs]}
        )


@pytest_asyncio.fixture(loop_scope="module")
async def world(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(settings, "embedding_provider", "ollama", raising=False)
    monkeypatch.setattr(settings, "embedding_dimensions", DIM, raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    ollama = _Ollama()
    client = httpx.AsyncClient(transport=httpx.MockTransport(ollama.handler))
    monkeypatch.setattr(embeddings_service, "_ollama_client", lambda: client)

    indexer._last_full_hash.clear()
    indexer.clear_embed_stop_state()
    vault_service.clear_user_vault_cache()
    async with sessionmaker() as session:
        for table in ("note_links", "note_embeddings", "notes_metadata",
                      "indexer_runs", "indexer_state", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()

    for name in ("Take a spore print.md", "Champignons.md"):
        (root / name).write_text(f"# {name}\n\nSome text about mushrooms.\n")
    assert await indexer._index_stage_once(None)
    indexer_health.reset()

    yield ollama

    await client.aclose()
    indexer.clear_embed_stop_state()
    indexer_health.reset()


async def _pending(sessionmaker) -> int:
    async with sessionmaker() as session:
        return (await session.execute(text(
            "SELECT count(*) FROM notes_metadata WHERE embedded_content_hash "
            "IS NULL OR embedded_content_hash != content_hash"
        ))).scalar()


async def test_a_provider_answering_500_degrades_health(world, sessionmaker):
    """The repro: every embed stage meets a 500 and the notes stay pending."""
    for _ in range(settings.indexer_degraded_after_failures):
        assert await indexer._embed_stage_once(None)
    assert world.calls >= settings.indexer_degraded_after_failures
    assert await _pending(sessionmaker) == 2

    snap = indexer_health.snapshot()
    assert snap["embedding_failing_scopes"] == 1, snap
    assert snap["status"] == "degraded"

    # The provider comes back: the next stage embeds, and health is ok again.
    world.down = False
    assert await indexer._embed_stage_once(None)
    assert await _pending(sessionmaker) == 0
    snap = indexer_health.snapshot()
    assert snap["embedding_failing_scopes"] == 0
    assert snap["embedding_stuck_scopes"] == 0
    assert snap["status"] == "ok", snap


async def test_pending_notes_that_never_embed_degrade_health_whatever_the_cause(
    world, sessionmaker, monkeypatch
):
    """Stages that return cleanly but embed nothing, with notes pending, are
    not progress: after `EMBED_STUCK_AFTER_SECONDS` health says so, even with
    no failure counted (here, every stage skipped by a stale fingerprint)."""
    async def skipped(_session, _suffix):
        return False

    real_check, real_now = indexer._embedding_generation_current, indexer_health._now
    monkeypatch.setattr(indexer, "_embedding_generation_current", skipped)
    now = indexer_health._now()
    monkeypatch.setattr(indexer_health, "_now", lambda: now)
    for _ in range(5):
        assert await indexer._embed_stage_once(None)
    snap = indexer_health.snapshot()
    assert snap["embedding_failing_scopes"] == 0
    assert snap["embedding_stuck_scopes"] == 0, "flagged before the threshold"

    later = now + timedelta(seconds=settings.embed_stuck_after_seconds)
    monkeypatch.setattr(indexer_health, "_now", lambda: later)
    assert await indexer._embed_stage_once(None)
    snap = indexer_health.snapshot()
    assert snap["embedding_stuck_scopes"] == 1, snap
    assert snap["seconds_embedding_stuck"] >= settings.embed_stuck_after_seconds
    assert snap["status"] == "degraded"

    # Fixed: the next stage embeds them, and the flag clears at once.
    monkeypatch.setattr(indexer, "_embedding_generation_current", real_check)
    monkeypatch.setattr(indexer_health, "_now", real_now)
    world.down = False
    assert await indexer._embed_stage_once(None)
    assert await _pending(sessionmaker) == 0
    assert indexer_health.snapshot()["embedding_stuck_scopes"] == 0
