"""Query embeddings go first: the indexer's next document slice waits while a
query embedding is pending, within a bound (fork, 2026-11-19).

See indexing-and-embeddings.md, "Queries go first". Offline: the providers'
request functions are replaced, so nothing reaches a network.
"""
import asyncio
import time

import pytest

from src.config import settings
from src.services import embeddings
from src.services.embeddings import OllamaProvider, OpenAIProvider, TeiProvider


@pytest.fixture
def ollama(monkeypatch):
    """The real `get_embedding` and `OllamaProvider.embed_batch` over a fake
    `/api/embed`: a query (`["q..."]`) blocks until its event is set; every
    other request is a document slice, recorded in the order it was sent."""
    monkeypatch.setattr(settings, "embedding_provider", "ollama")
    monkeypatch.setattr(settings, "ollama_embed_batch_size", 2)
    monkeypatch.setattr(settings, "embedding_query_instruction", "")
    embeddings.get_provider.cache_clear()

    state = type("State", (), {})()
    state.events = []
    state.slices = []
    state.query_gates = {}
    state.slice_gate = None

    async def fake_embed_inputs(self, inputs):
        if len(inputs) == 1 and inputs[0].startswith("q"):
            state.events.append(("query sent", inputs[0]))
            gate = state.query_gates.get(inputs[0])
            if gate is not None:
                await gate.wait()
            state.events.append(("query done", inputs[0]))
            return [[1.0, 0.0]]
        state.events.append(("slice sent", list(inputs)))
        state.slices.append(list(inputs))
        if state.slice_gate is not None and len(state.slices) == 1:
            await state.slice_gate.wait()
        return [[0.0, float(i)] for i, _ in enumerate(inputs)]

    monkeypatch.setattr(OllamaProvider, "_embed_inputs", fake_embed_inputs)
    return state


async def _until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


async def test_a_slice_waits_while_a_query_is_pending(ollama):
    ollama.query_gates["q1"] = asyncio.Event()
    query = asyncio.create_task(embeddings.get_embedding("q1", kind="query"))
    await _until(lambda: ("query sent", "q1") in ollama.events)
    assert embeddings._QUERY_PRIORITY.pending == 1

    batch = asyncio.create_task(
        embeddings.get_embeddings_batch(["a", "b", "c"], kind="document")
    )
    await asyncio.sleep(0.1)
    assert ollama.slices == [], "no slice is sent while the query is pending"

    ollama.query_gates["q1"].set()
    assert await query == [1.0, 0.0]
    vectors = await batch
    assert ollama.slices == [["a", "b"], ["c"]]
    assert len(vectors) == 3
    assert embeddings._QUERY_PRIORITY.pending == 0


async def test_the_slices_are_the_same_with_and_without_a_query(ollama):
    """The gate changes when a slice is sent, never what is in it."""
    texts = [f"t{i}" for i in range(5)]
    alone = await embeddings.get_embeddings_batch(texts, kind="document")
    slices_alone = list(ollama.slices)
    ollama.slices.clear()

    ollama.query_gates["q2"] = asyncio.Event()
    query = asyncio.create_task(embeddings.get_embedding("q2", kind="query"))
    await _until(lambda: ("query sent", "q2") in ollama.events)
    batch = asyncio.create_task(embeddings.get_embeddings_batch(texts, kind="document"))
    await asyncio.sleep(0.05)
    ollama.query_gates["q2"].set()
    await query
    assert await batch == alone
    assert ollama.slices == slices_alone == [["t0", "t1"], ["t2", "t3"], ["t4"]]


async def test_a_slice_in_flight_is_untouched_and_the_next_one_waits(ollama):
    ollama.slice_gate = asyncio.Event()
    batch = asyncio.create_task(
        embeddings.get_embeddings_batch(["a", "b", "c"], kind="document")
    )
    await _until(lambda: len(ollama.slices) == 1)

    ollama.query_gates["q3"] = asyncio.Event()
    query = asyncio.create_task(embeddings.get_embedding("q3", kind="query"))
    await _until(lambda: ("query sent", "q3") in ollama.events)

    ollama.slice_gate.set()  # the first slice answers
    await asyncio.sleep(0.1)
    assert ollama.slices == [["a", "b"]], "the next slice waits for the query"

    ollama.query_gates["q3"].set()
    await query
    vectors = await batch
    assert ollama.slices == [["a", "b"], ["c"]]
    assert vectors == [[0.0, 0.0], [0.0, 1.0], [0.0, 0.0]]
    order = [e[0] for e in ollama.events]
    assert order.index("query done") < len(order) - 1
    assert ollama.events[-1] == ("slice sent", ["c"])


async def test_the_wait_is_bounded(ollama, monkeypatch):
    """A query that never ends holds each slice for the bound, no longer."""
    monkeypatch.setattr(embeddings, "QUERY_PRIORITY_MAX_WAIT_SECONDS", 0.05)
    ollama.query_gates["q4"] = asyncio.Event()
    query = asyncio.create_task(embeddings.get_embedding("q4", kind="query"))
    await _until(lambda: ("query sent", "q4") in ollama.events)

    start = time.monotonic()
    vectors = await asyncio.wait_for(
        embeddings.get_embeddings_batch(["a", "b", "c"], kind="document"), timeout=2
    )
    elapsed = time.monotonic() - start
    assert ollama.slices == [["a", "b"], ["c"]]
    assert len(vectors) == 3
    assert elapsed >= 0.1  # two slices, each waited the bound
    assert not query.done()

    ollama.query_gates["q4"].set()
    await query


async def test_the_wait_is_not_request_time(ollama, monkeypatch):
    """Ollama's per-slice `wait_for` covers the request only: a wait longer
    than `OLLAMA_REQUEST_TIMEOUT_SECONDS` does not time the slice out."""
    monkeypatch.setattr(settings, "ollama_request_timeout_seconds", 0.05)
    ollama.query_gates["q5"] = asyncio.Event()
    query = asyncio.create_task(embeddings.get_embedding("q5", kind="query"))
    await _until(lambda: ("query sent", "q5") in ollama.events)
    batch = asyncio.create_task(embeddings.get_embeddings_batch(["a"], kind="document"))
    await asyncio.sleep(0.2)
    ollama.query_gates["q5"].set()
    await query
    assert await batch == [[0.0, 0.0]]


async def test_a_cancelled_or_failed_query_releases_the_gate(ollama, monkeypatch):
    ollama.query_gates["q6"] = asyncio.Event()
    query = asyncio.create_task(embeddings.get_embedding("q6", kind="query"))
    await _until(lambda: ("query sent", "q6") in ollama.events)
    query.cancel()
    with pytest.raises(asyncio.CancelledError):
        await query
    assert embeddings._QUERY_PRIORITY.pending == 0

    async def failing(self, inputs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(OllamaProvider, "_embed_inputs", failing)
    with pytest.raises(RuntimeError):
        await embeddings.get_embedding("q7", kind="query")
    assert embeddings._QUERY_PRIORITY.pending == 0
    # Nothing pending: a slice goes at once.
    start = time.monotonic()
    await embeddings._let_queries_go_first("document")
    assert time.monotonic() - start < 0.05


async def test_only_queries_count_and_only_documents_wait(ollama):
    # A document embedded one at a time is not a pending query.
    await embeddings.get_embedding("doc", kind="document")
    assert embeddings._QUERY_PRIORITY.pending == 0

    ollama.query_gates["q8"] = asyncio.Event()
    query = asyncio.create_task(embeddings.get_embedding("q8", kind="query"))
    await _until(lambda: ("query sent", "q8") in ollama.events)
    # A batch of kind "query" does not wait behind a query.
    start = time.monotonic()
    await embeddings._let_queries_go_first("query")
    assert time.monotonic() - start < 0.05
    ollama.query_gates["q8"].set()
    await query


async def test_two_overlapping_queries_hold_the_slice_until_both_end(ollama):
    ollama.query_gates["qa"] = asyncio.Event()
    ollama.query_gates["qb"] = asyncio.Event()
    qa = asyncio.create_task(embeddings.get_embedding("qa", kind="query"))
    qb = asyncio.create_task(embeddings.get_embedding("qb", kind="query"))
    await _until(lambda: ("query sent", "qb") in ollama.events)
    assert embeddings._QUERY_PRIORITY.pending == 2
    batch = asyncio.create_task(embeddings.get_embeddings_batch(["a"], kind="document"))
    ollama.query_gates["qa"].set()
    await qa
    await asyncio.sleep(0.05)
    assert ollama.slices == []
    ollama.query_gates["qb"].set()
    await qb
    await batch
    assert ollama.slices == [["a"]]


@pytest.mark.parametrize("provider_cls,method,size_setting", [
    (OpenAIProvider, "_post", "openai_embed_batch_size"),
    (TeiProvider, "_post_slice", "tei_embed_batch_size"),
    (OllamaProvider, "_embed_inputs", "ollama_embed_batch_size"),
])
async def test_every_provider_lets_queries_go_first_before_each_slice(
    provider_cls, method, size_setting, monkeypatch
):
    monkeypatch.setattr(settings, size_setting, 2)
    calls = []

    async def gate(kind):
        calls.append(("gate", kind))

    async def post(self, inputs):
        calls.append(("slice", list(inputs)))
        return [[0.0] for _ in inputs]

    async def verified():
        return None

    monkeypatch.setattr(embeddings, "_let_queries_go_first", gate)
    monkeypatch.setattr(embeddings, "_require_tei_verified", verified)
    monkeypatch.setattr(provider_cls, method, post)
    out = await provider_cls().embed_batch(["a", "b", "c", "d", "e"], kind="document")
    assert len(out) == 5
    assert calls == [
        ("gate", "document"), ("slice", ["a", "b"]),
        ("gate", "document"), ("slice", ["c", "d"]),
        ("gate", "document"), ("slice", ["e"]),
    ]


async def test_state_from_another_loop_is_dropped():
    """A count left by a loop that ended (pytest runs one per test) does not
    hold the next loop's slices."""
    priority = embeddings._QueryPriority()
    priority.query_started()  # left pending on this test's loop

    def other_loop():
        async def body():
            assert priority.pending == 0
            start = time.monotonic()
            await priority.wait_until_idle(1.0)
            return time.monotonic() - start

        return asyncio.run(body())

    waited = await asyncio.to_thread(other_loop)
    assert waited < 0.05
