"""`hybrid_search` with private notes left out: the keyword arm ranked once
for both the page and the count gives exactly what two runs gave (fork, P98).

Until this change, step 4 ran the keyword arm filtered and the count ran it
again unfiltered (`search.keyword_arm_rows` twice). `keyword_arm_rows_split`
now reads both from one ranking statement per block. The promise is that the
caller sees nothing different: the same page, the same order, the same
`private_hidden`, for every input. `_old_hybrid_search` below is the service
as it stood before (b4c2cfb), kept as the oracle, its telemetry and test hook
left out; every case runs both on a seeded vault and compares the results
field by field.

The vault is built so that the two keyword arms differ in every way they can:
private notes at a third of the corpus and among rank ties, a conjunctive
block that fills only unfiltered (the filtered arm then runs the any-word
fill and the unfiltered one does not), fills of different lengths in both,
private notes at the page boundary, and a folder where only private notes
hold vectors (the filtered semantic scan comes back empty and re-runs exact).
The arm depth is patched down so the fills and the cuts happen on a small
corpus. A last test asserts each of those cases was met.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""

import hashlib
import math
import random

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.models.db import NoteEmbedding
from src.services import embeddings, hybrid, private_folders, search, timing
from src.services.fts import index_tsvector_sql
from src.services.hybrid import HybridResult, HybridRow, count_private_hidden, fuse

import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)


def _unit(values: list[float]) -> list[float]:
    vec = list(values) + [0.0] * (DIM - len(values))
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _vector_for(seed: str) -> list[float]:
    rng = random.Random(hashlib.sha256(seed.encode()).hexdigest())
    return _unit([rng.uniform(-1, 1) for _ in range(6)])


WORDS = ["apple", "banana", "cherry", "date", "elder", "fig", "grape"]
FILLER = ["stone", "river", "cloud", "paper", "lamp"]
FOLDERS = ["Notes/", "Work/", "Notes/Sub/", "Diary (private)/", "Deep/People (Private)/", "Work/Old (private)/"]


def _corpus() -> list[tuple[str, str, int, bool]]:
    """`(path, body, chunks, excerpt)`: chunks is how many vectors the note
    holds (0 for none)."""
    rng = random.Random(20261119)
    notes = []
    for i in range(140):
        folder = rng.choice(FOLDERS)
        words = [rng.choice(WORDS[: rng.randint(2, len(WORDS))]) for _ in range(rng.randint(1, 6))]
        words += rng.sample(FILLER, rng.randint(0, 3))
        rng.shuffle(words)
        chunks = rng.choice([0, 1, 1, 2, 3])
        notes.append((f"{folder}n{i:03d}.md", " ".join(words), chunks, i % 2 == 0))
    # Rank ties (the same body, a title that does not match): by code point
    # the private paths sort among the public ones differently than by a
    # linguistic collation, so the database's order decides both arms.
    for name in ["alpha", "Beta (private)/b", "beta", "Diary (private)/x", "delta", "echo",
                 "Echo (private)/e", "foxtrot", "golf (private)/g", "hotel"]:
        notes.append((f"Tie/{name}.md", "kiwi lemon", 1, True))
    # Only private notes hold vectors here: the filtered scan finds nothing
    # and re-runs exact; the unfiltered one finds them.
    for name in ["a", "b", "c"]:
        notes.append((f"Only/{name}.md", "melon", 0, True))
    for name in ["Secret (private)/s", "Secret (private)/t"]:
        notes.append((f"Only/{name}.md", "melon", 2, True))
    return notes


QUERIES = [
    "apple", "banana", "grape", "fig", "apple banana", "cherry date", "apple fig grape",
    "elder banana cherry", "\"apple banana\"", "kiwi", "kiwi lemon", "lemon apple", "melon",
    "melon stone", "nothinghere", "the",
]


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("hybridsplit", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def maker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    fragment, cfg_params = index_tsvector_sql(tags_bind=None)
    async with maker() as session:
        for path, body, chunks, excerpt in _corpus():
            title = path.rsplit("/", 1)[-1][:-3]
            note_id = (await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, tags, frontmatter, content_hash, "
                "embedded_content_hash, body_excerpt, content_tsvector) VALUES "
                f"(NULL, :p, :t, '{{}}', '{{}}', :h, :h, :excerpt, {fragment}) RETURNING id"),
                {"p": path, "t": title, "h": path, "excerpt": body if excerpt else None,
                 "content": body, "title": title, "aliases": "", **cfg_params},
            )).scalar_one()
            for c in range(chunks):
                session.add(NoteEmbedding(note_id=note_id, chunk_index=c, chunk_text=f"{body} #{c}",
                                          embedding=_vector_for(f"{path}#{c}")))
        await session.commit()
        await session.execute(text("ANALYZE"))
    yield maker
    await engine.dispose()


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(settings, "private_folder_names", ["* (private)"])

    async def fake_get_embedding(text_: str, **_kw):
        return _vector_for(f"query:{text_}")

    monkeypatch.setattr(embeddings, "get_embedding", fake_get_embedding)


# ── The oracle: `hybrid_search` and `_private_hidden_count` as of b4c2cfb ────


async def _old_private_hidden_count(session, query, query_embedding, page, *, filters, k, limit):
    k_all = [r["path"] for r in (
        await search.keyword_arm_rows(session, query, limit=hybrid.HYBRID_ARM_DEPTH, **filters)).rows]
    s_all: list[str] = []
    if query_embedding is not None:
        s_arm = await embeddings.semantic_arm_rows(
            session, query_embedding, overfetch=hybrid.HYBRID_ARM_DEPTH, **filters)
        s_all = [r["path"] for r in s_arm.rows]
        if s_arm.exact_fallback:
            await session.execute(text("SET LOCAL enable_indexscan = on"))
    private = {path for path in (*k_all, *s_all) if private_folders.is_private(path)}
    return count_private_hidden(page, k_all, s_all, private, k=k, limit=limit)


async def _old_hybrid_search(session, query, *, limit=10, folder=None, tags=None, frontmatter=None,
                             user_id=None, rrf_k=None, rerank_candidates=False, hide_private=False):
    limit = max(1, min(limit, hybrid.HYBRID_MAX_LIMIT))
    depth = limit
    if rerank_candidates:
        from src.services.rerank import RERANK_MAX_CANDIDATES

        depth = max(limit, RERANK_MAX_CANDIDATES)
    k = int(rrf_k) if rrf_k is not None else settings.hybrid_rrf_k
    filters = dict(folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id)
    hiding = hide_private and private_folders.enabled()

    query_embedding = await embeddings.get_embedding(query, kind="query")
    unavailable = None

    await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))
    await session.execute(text("SET LOCAL hnsw.ef_search = 80"))
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))

    k_arm = await search.keyword_arm_rows(
        session, query, limit=hybrid.HYBRID_ARM_DEPTH, with_excerpt_flag=True, **filters,
        **({"private": "exclude"} if hiding else {}),
    )

    s_arm = await embeddings.semantic_arm_rows(
        session, query_embedding, overfetch=hybrid.HYBRID_ARM_DEPTH,
        with_rerank_text=rerank_candidates, **filters,
        **({"hide_private": True} if hiding else {}),
    )
    s_rows, s_chunks, exact_fallback = s_arm.rows, s_arm.chunk_rows, s_arm.exact_fallback
    if exact_fallback:
        await session.execute(text("SET LOCAL enable_indexscan = on"))

    k_by_path = {r["path"]: r for r in k_arm.rows}
    s_by_path = {r["path"]: r for r in s_rows}
    fused = fuse([r["path"] for r in k_arm.rows], [r["path"] for r in s_rows], k)
    page: list[HybridRow] = []
    for path, score, k_rank, s_rank in fused[:depth]:
        kr, sr = k_by_path.get(path), s_by_path.get(path)
        src = kr if kr is not None else sr
        row = HybridRow(path=path, title=src["title"], tags=src["tags"], score=score,
                        keyword_rank=k_rank, semantic_rank=s_rank)
        if kr is not None:
            row.match = kr["match"]
            row.no_excerpt = bool(kr.get("no_excerpt"))
        if sr is not None:
            row.similarity = sr["similarity"]
            row.chunk = sr["chunk"]
            row.stale = bool(sr["stale"])
            row.embedding_truncated = bool(sr["embedding_truncated"])
            row.section = sr.get("section")
            if rerank_candidates:
                row.rerank_text = sr.get("rerank_text")
                row.rerank_chunks = list(sr.get("rerank_chunks") or [])
                row.rerank_stamp = sr.get("rerank_stamp")
        page.append(row)

    cap = settings.keyword_snippet_rows
    if cap > 0:
        snippet_rows = [k_by_path[r.path] for r in page if r.path in k_by_path][:cap]
        if snippet_rows:
            await search.keyword_snippets(session, snippet_rows, k_arm)
            for kd in snippet_rows:
                row = next(r for r in page if r.path == kd["path"])
                row.snippet = kd.get("snippet")
                row.head = kd.get("head")
                row.matched_in = kd.get("matched_in") or None

    rechunk_pending = await embeddings.rechunk_pending_count(session, user_id)
    if rerank_candidates:
        await hybrid._fill_excerpts(session, page, user_id=user_id)
    private_hidden = None
    if hiding:
        private_hidden = await _old_private_hidden_count(
            session, query, query_embedding, [path for path, *_ in fused[:limit]],
            filters=filters, k=k, limit=limit,
        )

    overlap = len(k_by_path.keys() & s_by_path.keys())
    keyword_any = sum(1 for r in k_arm.rows if r["match"] == "any")
    return HybridResult(
        rows=page, arms=hybrid.ARMS_BOTH, semantic_unavailable=unavailable, rrf_k=k,
        keyword_rows=len(k_arm.rows), keyword_all=len(k_arm.rows) - keyword_any,
        keyword_any=keyword_any, semantic_rows=len(s_rows), semantic_chunks=s_chunks,
        overlap=overlap, snippet_cap=cap, rechunk_pending=rechunk_pending,
        top_similarity=max((r["similarity"] for r in s_rows), default=None),
        private_hidden=private_hidden,
    )


# ── What the cases met, asserted at the end ──────────────────────────────────

SEEN: dict[str, int] = {
    "fill_filtered_only": 0,   # the unfiltered conjunctive block filled, the filtered one did not
    "fill_both_lengths_differ": 0,   # both arms filled, with different remaining limits
    "boundary": 0,   # a full page with some, not all, private notes counted
    "exact_fallback": 0,   # the filtered semantic scan re-ran exact
    "hidden_nonzero": 0,
}


async def _old_arms(maker, query, depth, folder):
    async with maker() as session:
        filtered = await search.keyword_arm_rows(
            session, query, limit=depth, folder=folder, with_excerpt_flag=True, private="exclude")
    async with maker() as session:
        unfiltered = await search.keyword_arm_rows(session, query, limit=depth, folder=folder)
    return filtered, unfiltered


@pytest.mark.parametrize("depth", [1, 2, 3, 5, 8, 50])
@pytest.mark.parametrize("folder", [None, "Tie/", "Only/", "Notes/"])
async def test_the_split_keyword_arm_is_the_two_runs(maker, wired, depth, folder):
    for query in QUERIES:
        old_filtered, old_unfiltered = await _old_arms(maker, query, depth, folder)
        async with maker() as session:
            arm, unfiltered = await search.keyword_arm_rows_split(
                session, query, limit=depth, folder=folder, with_excerpt_flag=True)
        case = (query, depth, folder)
        assert arm.rows == old_filtered.rows, case
        assert unfiltered == [r["path"] for r in old_unfiltered.rows], case
        assert arm.fallback == old_filtered.fallback, case
        assert arm.plan == old_filtered.plan, case
        assert (arm.any_query is None) == (old_filtered.any_query is None), case
        if old_filtered.fallback == "ran" and old_unfiltered.fallback == "not_needed":
            SEEN["fill_filtered_only"] += 1
        if old_filtered.fallback == old_unfiltered.fallback == "ran":
            n_f = sum(1 for r in old_filtered.rows if r["match"] == "all")
            n_u = sum(1 for r in old_unfiltered.rows if r["match"] == "all")
            if n_f != n_u:
                SEEN["fill_both_lengths_differ"] += 1


@pytest.mark.parametrize("depth", [2, 5, 50])
@pytest.mark.parametrize("folder", [None, "Tie/", "Only/"])
@pytest.mark.parametrize("rerank", [False, True])
async def test_hybrid_search_is_the_old_service(maker, wired, monkeypatch, depth, folder, rerank):
    monkeypatch.setattr(hybrid, "HYBRID_ARM_DEPTH", depth)
    for query in QUERIES:
        for limit in (1, 2, 3, 5, 10, 50):
            async with maker() as session:
                old = await _old_hybrid_search(session, query, limit=limit, folder=folder,
                                               rerank_candidates=rerank, hide_private=True)
            token = timing.begin()
            try:
                async with maker() as session:
                    new = await hybrid.hybrid_search(session, query, limit=limit, folder=folder,
                                                     rerank_candidates=rerank, hide_private=True)
                holder = dict(timing.current())
            finally:
                timing.clear(token)
            case = (query, depth, folder, rerank, limit)
            assert [r.path for r in new.rows] == [r.path for r in old.rows], case
            assert new.private_hidden == old.private_hidden, case
            assert new == old, case
            if holder.get("exact_fallback"):
                SEEN["exact_fallback"] += 1
            if new.private_hidden:
                SEEN["hidden_nonzero"] += 1
                async with maker() as session:
                    everything = await hybrid.hybrid_search(session, query, limit=50, folder=folder)
                private_total = sum(1 for r in everything.rows if private_folders.is_private(r.path))
                if len(new.rows) >= limit and new.private_hidden < min(private_total, limit):
                    SEEN["boundary"] += 1


async def test_without_hiding_both_paths_agree_too(maker, wired, monkeypatch):
    monkeypatch.setattr(hybrid, "HYBRID_ARM_DEPTH", 5)
    for query in QUERIES:
        async with maker() as session:
            old = await _old_hybrid_search(session, query, limit=10)
        async with maker() as session:
            new = await hybrid.hybrid_search(session, query, limit=10)
        assert new == old and new.private_hidden is None, query


async def test_every_case_was_met():
    # Runs after the cases above in this module (pytest keeps file order;
    # the module is one xdist group).
    for name, count in SEEN.items():
        assert count > 0, f"the corpus no longer exercises {name}: {SEEN}"
