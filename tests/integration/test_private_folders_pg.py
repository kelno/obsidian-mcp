"""Private folders on the three searches, against a real database (fork, P98).

Off by default `include_private` leaves notes under a private folder out
inside the query, so the limit still fills, and the result ends with how many
would have been listed ("2 matches in private folders not shown."); with it,
they come back like any other. Each count is checked against the unfiltered
run of the same search: the private notes ranked ahead of the last result
shown, or every one when the results do not fill the limit.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""

import math

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.mcp_server.tools as tools
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import private_folders
from src.services.embeddings import semantic_search
from src.services.fts import index_tsvector_sql
from src.services.search import full_text_search

import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)


def _unit(*leading: float) -> list[float]:
    vec = list(leading) + [0.0] * (DIM - len(leading))
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


QUERY = NEAR = _unit(1.0)
MIDDLE = _unit(0.8, 0.6)
FAR = _unit(0.0, 1.0)

# path, body, vector. Two private notes (one nested, another case), and a
# note whose *file name* looks private, which is not.
NOTES = [
    ("Notes/a.md", "apple banana", NEAR),
    ("Diary (private)/b.md", "apple apple secret", NEAR),
    ("Deep/People (Private)/c.md", "apple cherry", MIDDLE),
    ("Notes/Plans (private).md", "apple", MIDDLE),
    ("Notes/d.md", "banana", FAR),
]
PRIVATE = {"Diary (private)/b.md", "Deep/People (Private)/c.md"}


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("private", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    fragment, cfg_params = index_tsvector_sql(tags_bind=None)
    async with maker() as session:
        for path, body, vec in NOTES:
            note_id = (await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, tags, frontmatter, content_hash, "
                f"embedded_content_hash, content_tsvector) VALUES (NULL, :p, :t, '{{}}', '{{}}', :h, :h, {fragment}) "
                "RETURNING id"), {"p": path, "t": path.rsplit("/", 1)[-1], "h": path, "content": body,
                                  "title": path.rsplit("/", 1)[-1], "aliases": "", **cfg_params},
            )).scalar_one()
            session.add(NoteEmbedding(note_id=note_id, chunk_index=0, chunk_text=body, embedding=vec))
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def wired(sessionmaker, monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])
    monkeypatch.setattr(tools, "async_session", sessionmaker)

    async def noop(*args, **kwargs):
        return None

    async def fake_get_embedding(text_: str):
        return QUERY

    monkeypatch.setattr(tools, "_log_usage", noop)
    monkeypatch.setattr("src.services.embeddings.get_embedding", fake_get_embedding)
    return sessionmaker


def _expected_hidden(unfiltered: list[str], limit: int) -> int:
    """From the unfiltered order: private notes ahead of the last public one shown."""
    public = [p for p in unfiltered if p not in PRIVATE]
    if len(public) < limit:
        return sum(1 for p in unfiltered if p in PRIVATE)
    last = unfiltered.index(public[limit - 1])
    return min(sum(1 for p in unfiltered[:last] if p in PRIVATE), limit)


# ── keyword_search ───────────────────────────────────────────────────────────


async def test_keyword_search_leaves_private_notes_out_and_counts_them(wired):
    out = await tools.search_notes_impl("apple")
    assert "Diary" not in out and "People" not in out
    assert "`Notes/a.md`" in out and "`Notes/Plans (private).md`" in out
    assert out.endswith("\n\n2 matches in private folders not shown.")

    opened = await tools.search_notes_impl("apple", include_private=True)
    for path in PRIVATE:
        assert f"`{path}`" in opened
    assert "not shown" not in opened


async def test_keyword_search_with_only_private_matches(wired):
    out = await tools.search_notes_impl("secret")
    assert out == "No results for 'secret'\n\n1 match in private folders not shown."


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_keyword_count_matches_the_unfiltered_order(wired, limit):
    async with wired() as session:
        unfiltered = [r["path"] for r in await full_text_search(session, "apple", limit=50)]
    async with wired() as session:
        counts: dict = {}
        shown = await full_text_search(session, "apple", limit=limit, hide_private=True, counts=counts)
    assert [r["path"] for r in shown] == [p for p in unfiltered if p not in PRIVATE][:limit]
    assert counts["private_hidden"] == _expected_hidden(unfiltered, limit)


async def test_keyword_search_without_the_setting_is_unchanged(wired, monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", [])
    out = await tools.search_notes_impl("apple")
    for path in PRIVATE:
        assert f"`{path}`" in out
    assert "not shown" not in out


# ── semantic_search ──────────────────────────────────────────────────────────


async def test_semantic_search_leaves_private_notes_out_and_counts_them(wired):
    out = await tools.semantic_search_impl("anything", limit=10)
    assert "Diary" not in out and "People" not in out and "secret" not in out
    assert out.endswith("\n\n2 matches in private folders not shown.")
    opened = await tools.semantic_search_impl("anything", limit=10, include_private=True)
    assert "`Diary (private)/b.md`" in opened and "not shown" not in opened


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_semantic_count_matches_the_unfiltered_order(wired, limit):
    async with wired() as session:
        unfiltered = [r["path"] for r in await semantic_search(session, "q", limit=50)]
    async with wired() as session:
        counts: dict = {}
        shown = await semantic_search(session, "q", limit=limit, hide_private=True, counts=counts)
    assert [r["path"] for r in shown] == [p for p in unfiltered if p not in PRIVATE][:limit]
    assert counts["private_hidden"] == _expected_hidden(unfiltered, limit)


# ── find_related ─────────────────────────────────────────────────────────────


async def test_find_related_leaves_private_notes_out_and_counts_them(wired):
    out = await tools.find_related_impl("Notes/a.md", limit=10)
    assert "Diary" not in out and "People" not in out
    assert "`Notes/Plans (private).md`" in out
    assert out.endswith("\n\n2 matches in private folders not shown.")
    # Limit 1: Plans (distance 0.2) is listed; b.md (0) was nearer, c.md ties
    # at 0.2 and so would not have displaced it.
    one = await tools.find_related_impl("Notes/a.md", limit=1)
    assert "`Notes/Plans (private).md`" in one and one.endswith("\n\n1 match in private folders not shown.")
    opened = await tools.find_related_impl("Notes/a.md", limit=10, include_private=True)
    assert "`Diary (private)/b.md`" in opened and "not shown" not in opened


async def test_find_related_from_a_private_note_still_works(wired):
    # Naming a private note is asking for it: the source is not filtered,
    # only what the result lists.
    out = await tools.find_related_impl("Diary (private)/b.md", limit=10)
    assert "`Notes/a.md`" in out and "People" not in out
    assert out.endswith("\n\n1 match in private folders not shown.")


async def test_the_sql_predicate_agrees_with_is_private(wired):
    async with wired() as session:
        rows = (await session.execute(
            text("SELECT file_path FROM notes_metadata WHERE file_path ~* :r"),
            {"r": private_folders.path_regex()})).scalars().all()
    assert set(rows) == PRIVATE == {p for p, _, _ in NOTES if private_folders.is_private(p)}
