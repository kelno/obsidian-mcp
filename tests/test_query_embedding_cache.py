"""The in-process cache of query embeddings (fork, 2026-11-19).

See indexing-and-embeddings.md, "The query-embedding cache". A hit returns the
vector the provider returned for the same provider input, as a new list; the
key covers everything that decides the vector; documents, errors and the
warm-ups never touch it.
"""
import asyncio
import math
import struct

import pytest

from src import main
from src.config import settings
from src.services import embeddings, refusals


class _Provider:
    """One fixed provider object, as `get_provider()`'s singleton is. Answers
    from `vectors` (by provider input) or a default derived from the input,
    and records every request."""

    def __init__(self):
        self.calls = []
        self.vectors = {}
        self.fail_with = None
        self.gate = None

    async def embed_one(self, text, *, kind):
        self.calls.append((kind, text))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        if text in self.vectors:
            return list(self.vectors[text])
        return [float(len(text)), 0.25, -0.5]

    async def embed_batch(self, texts, *, kind):
        return [await self.embed_one(t, kind=kind) for t in texts]


@pytest.fixture
def provider(monkeypatch):
    p = _Provider()
    monkeypatch.setattr(embeddings, "get_provider", lambda: p)
    monkeypatch.setattr(settings, "embedding_provider", "ollama")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "embedding_dimensions", 3)
    monkeypatch.setattr(settings, "embedding_query_instruction", "")
    return p


async def test_a_repeated_query_is_answered_from_the_cache(provider):
    first = await embeddings.get_embedding("find this", kind="query")
    second = await embeddings.get_embedding("find this", kind="query")
    assert first == second == [9.0, 0.25, -0.5]
    assert provider.calls == [("query", "find this")]


async def test_a_hit_is_bit_for_bit_the_providers_vector(provider):
    odd = [0.1, -0.0, 1e-300, 5e-324, 1.7976931348623157e308, -0.123456789012345678]
    provider.vectors["q"] = odd
    fresh = await embeddings.get_embedding("q", kind="query")
    hit = await embeddings.get_embedding("q", kind="query")
    pack = lambda v: struct.pack(f"{len(v)}d", *v)  # noqa: E731
    assert pack(hit) == pack(fresh) == pack(odd)
    assert math.copysign(1.0, hit[1]) == -1.0
    assert all(type(x) is float for x in hit)


async def test_a_vector_with_integer_elements_comes_back_as_it_was(provider):
    """A provider's JSON can carry `0` rather than `0.0`; a hit returns the
    same objects, not floats in their place."""
    provider.vectors["q"] = [0, 0.5, -1]
    await embeddings.get_embedding("q", kind="query")
    hit = await embeddings.get_embedding("q", kind="query")
    assert hit == [0, 0.5, -1]
    assert [type(x) for x in hit] == [int, float, int]


async def test_no_caller_can_change_a_cached_vector(provider):
    first = await embeddings.get_embedding("q", kind="query")
    first[0] = 999.0
    second = await embeddings.get_embedding("q", kind="query")
    assert second == [1.0, 0.25, -0.5]
    second.append(7.0)
    second[1] = -1.0
    third = await embeddings.get_embedding("q", kind="query")
    assert third == [1.0, 0.25, -0.5]
    assert third is not second
    assert len(provider.calls) == 1


async def test_the_key_is_the_provider_input_after_the_instruction(provider, monkeypatch):
    await embeddings.get_embedding("abc", kind="query")
    monkeypatch.setattr(settings, "embedding_query_instruction", "Instruct: ")
    await embeddings.get_embedding("abc", kind="query")
    assert provider.calls == [("query", "abc"), ("query", "Instruct: abc")]
    # The same bytes reach the provider either way, so one entry answers both.
    monkeypatch.setattr(settings, "embedding_query_instruction", "Instruct")
    await embeddings.get_embedding(": abc", kind="query")
    assert len(provider.calls) == 2


@pytest.mark.parametrize("setting,value", [
    ("embedding_model", "qwen3-embedding:0.6b"),
    ("ollama_url", "http://other:11434"),
    ("embedding_dimensions", 4),
])
async def test_anything_that_decides_the_vector_is_in_the_key(provider, monkeypatch, setting, value):
    await embeddings.get_embedding("q", kind="query")
    monkeypatch.setattr(settings, setting, value)
    await embeddings.get_embedding("q", kind="query")
    assert len(provider.calls) == 2


async def test_the_provider_and_its_model_are_in_the_key(provider, monkeypatch):
    await embeddings.get_embedding("q", kind="query")
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "openai_embedding_model", "text-embedding-3-small")
    await embeddings.get_embedding("q", kind="query")
    monkeypatch.setattr(settings, "openai_embedding_model", "text-embedding-3-large")
    await embeddings.get_embedding("q", kind="query")
    assert len(provider.calls) == 3

    other = _Provider()
    monkeypatch.setattr(embeddings, "get_provider", lambda: other)
    await embeddings.get_embedding("q", kind="query")
    assert len(other.calls) == 1


async def test_documents_never_use_the_cache(provider):
    await embeddings.get_embedding("text", kind="document")
    await embeddings.get_embedding("text", kind="document")
    await embeddings.get_embeddings_batch(["text"], kind="document")
    assert provider.calls == [("document", "text")] * 3
    # Nor does a document fill it for a query of the same text.
    await embeddings.get_embedding("text", kind="query")
    assert provider.calls[-1] == ("query", "text")
    assert len(embeddings._QUERY_EMBEDDING_CACHE) == 1


@pytest.mark.parametrize("error", [
    RuntimeError("provider down"),
    refusals.ProviderInputTooLarge("too long", provider="ollama"),
])
async def test_errors_are_never_cached(provider, error):
    provider.fail_with = error
    with pytest.raises(type(error)):
        await embeddings.get_embedding("q", kind="query")
    assert len(embeddings._QUERY_EMBEDDING_CACHE) == 0
    with pytest.raises(type(error)):
        await embeddings.get_embedding("q", kind="query")
    assert len(provider.calls) == 2
    provider.fail_with = None
    assert await embeddings.get_embedding("q", kind="query") == [1.0, 0.25, -0.5]
    assert len(provider.calls) == 3


async def test_a_cancelled_or_timed_out_query_caches_nothing(provider):
    provider.gate = asyncio.Event()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(embeddings.get_embedding("q", kind="query"), 0.05)
    assert len(embeddings._QUERY_EMBEDDING_CACHE) == 0
    assert embeddings._QUERY_PRIORITY.pending == 0
    provider.gate.set()
    await embeddings.get_embedding("q", kind="query")
    assert len(provider.calls) == 2


async def test_a_hit_is_not_a_pending_query(provider):
    await embeddings.get_embedding("q", kind="query")
    provider.gate = asyncio.Event()  # a provider call would now block
    assert await asyncio.wait_for(embeddings.get_embedding("q", kind="query"), 1) == [1.0, 0.25, -0.5]
    assert embeddings._QUERY_PRIORITY.pending == 0


async def test_the_cache_is_bounded_least_recently_used_first(provider, monkeypatch):
    monkeypatch.setattr(embeddings, "_QUERY_EMBEDDING_CACHE", embeddings._QueryEmbeddingCache(2))
    await embeddings.get_embedding("a", kind="query")
    await embeddings.get_embedding("b", kind="query")
    await embeddings.get_embedding("a", kind="query")  # hit: "a" is now the most recent
    await embeddings.get_embedding("c", kind="query")  # evicts "b"
    assert len(embeddings._QUERY_EMBEDDING_CACHE) == 2
    await embeddings.get_embedding("a", kind="query")
    await embeddings.get_embedding("c", kind="query")
    assert [t for _, t in provider.calls] == ["a", "b", "c"]
    await embeddings.get_embedding("b", kind="query")
    assert [t for _, t in provider.calls] == ["a", "b", "c", "b"]


def test_the_default_bound():
    assert embeddings.QUERY_EMBEDDING_CACHE_ENTRIES == 256
    assert embeddings._QUERY_EMBEDDING_CACHE.max_entries == 256


async def test_use_cache_false_always_asks_and_stores_nothing(provider):
    await embeddings.get_embedding("warmup", kind="query", use_cache=False)
    await embeddings.get_embedding("warmup", kind="query", use_cache=False)
    assert len(provider.calls) == 2
    assert len(embeddings._QUERY_EMBEDDING_CACHE) == 0
    # A cached entry does not answer it either.
    await embeddings.get_embedding("warmup", kind="query")
    await embeddings.get_embedding("warmup", kind="query", use_cache=False)
    assert len(provider.calls) == 4


async def test_the_startup_warm_up_reaches_the_provider_every_time(provider):
    await main._warm_embedding_model()
    await main._warm_embedding_model()
    assert provider.calls == [("query", "warmup"), ("query", "warmup")]


async def test_an_unhashable_stand_in_provider_is_not_cached(monkeypatch):
    from types import SimpleNamespace

    calls = []

    async def embed_one(text, *, kind):
        calls.append(text)
        return [0.0]

    stand_in = SimpleNamespace(embed_one=embed_one)
    monkeypatch.setattr(embeddings, "get_provider", lambda: stand_in)
    await embeddings.get_embedding("q", kind="query")
    await embeddings.get_embedding("q", kind="query")
    assert calls == ["q", "q"]


async def test_the_ledger_query_vector_goes_through_the_cache(provider):
    from src.services import ledger_embed

    first = await ledger_embed.query_vector("what failed")
    second = await ledger_embed.query_vector("what failed")
    assert first == second
    assert provider.calls == [("query", "what failed")]
    # And a search of the same words shares the entry.
    await embeddings.get_embedding("what failed", kind="query")
    assert len(provider.calls) == 1


async def test_each_user_has_their_own_entries(provider):
    """A hit is fast enough to tell apart from a miss, so on a multi-user
    deployment one tenant's search never answers another's (review, 2026-11-19)."""
    from src.auth.session import current_user_id

    token = current_user_id.set(1)
    try:
        await embeddings.get_embedding("q", kind="query")
        await embeddings.get_embedding("q", kind="query")
    finally:
        current_user_id.reset(token)
    token = current_user_id.set(2)
    try:
        await embeddings.get_embedding("q", kind="query")
    finally:
        current_user_id.reset(token)
    assert len(provider.calls) == 2


async def test_a_long_query_costs_its_vector_in_the_cache_not_its_text(provider):
    """The key holds the input's SHA-256: a 1 MiB query adds no 1 MiB key."""
    long = "x" * (1 << 20)
    await embeddings.get_embedding(long, kind="query")
    key = embeddings._query_cache_key(embeddings.get_provider(), long, "query")
    assert not any(isinstance(part, str) and len(part) > 1000 for part in key)
    await embeddings.get_embedding(long, kind="query")
    assert len(provider.calls) == 1
