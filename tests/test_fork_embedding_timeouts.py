"""Fork: configurable per-request embedding timeouts and OpenAI batch size.

A CPU-only Ollama can take minutes per batch. The timeouts and the OpenAI
batch size are settings so such a deployment can complete, and the timeout
stays a bound on **one request**, never on a note (#127 D5). A read timeout
on the OpenAI provider is not retried: the server is still computing the
abandoned request, and re-sending it multiplied the load.

Fully offline: HTTP is answered by respx, the clock by `wait_for` spies.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from httpx import Response
from pydantic import ValidationError

from src.config import Settings, settings
from src.services import embeddings, index_state
from src.services.embeddings import OllamaProvider, OpenAIProvider


def make(**kw):
    kw.setdefault("secret_key", "0123456789abcdef0123456789abcdef")
    return Settings(_env_file=None, **kw)


@pytest.fixture(autouse=True)
def _fresh_clients(monkeypatch):
    monkeypatch.setattr(embeddings, "_OLLAMA_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(embeddings, "_OPENAI_CLIENT", embeddings._SharedClient())


@pytest.fixture
def ollama(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "embedding_model", "bge-m3")
    monkeypatch.setattr(settings, "ollama_embed_batch_size", 16)
    return settings


@pytest.fixture
def openai(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.example.test/v1")
    return settings


def _openai_answer(request: httpx.Request) -> Response:
    inputs = json.loads(request.read())["input"]
    return Response(
        200,
        json={"data": [{"index": i, "embedding": [0.1]} for i in range(len(inputs))]},
    )


# ── Validation ──────────────────────────────────────────────────────────────


def test_defaults_are_the_upstream_values():
    s = make()
    assert s.ollama_request_timeout_seconds == 30.0
    assert s.openai_request_timeout_seconds == 60.0
    assert s.openai_embed_batch_size == 96


@pytest.mark.parametrize(
    "field", ["ollama_request_timeout_seconds", "openai_request_timeout_seconds"]
)
@pytest.mark.parametrize("value", [0, -1, 3601])
def test_a_timeout_outside_0_to_3600_is_refused(field, value):
    with pytest.raises(ValidationError):
        make(**{field: value})


@pytest.mark.parametrize(
    "field", ["ollama_request_timeout_seconds", "openai_request_timeout_seconds"]
)
def test_a_timeout_is_read_from_the_environment(field, monkeypatch):
    monkeypatch.setenv(field.upper(), "900")
    assert getattr(make(), field) == 900.0


@pytest.mark.parametrize("value", [0, 97])
def test_openai_batch_size_outside_1_to_96_is_refused(value):
    with pytest.raises(ValidationError):
        make(openai_embed_batch_size=value)


def test_openai_batch_size_is_not_in_the_fingerprint(openai, monkeypatch):
    before = index_state.embedding_fingerprint()
    monkeypatch.setattr(settings, "openai_embed_batch_size", 1)
    monkeypatch.setattr(settings, "openai_request_timeout_seconds", 900.0)
    monkeypatch.setattr(settings, "ollama_request_timeout_seconds", 900.0)
    assert index_state.embedding_fingerprint() == before


# ── The Ollama timeout is per request ───────────────────────────────────────


async def test_ollama_wait_for_uses_the_setting_once_per_slice(ollama, monkeypatch):
    monkeypatch.setattr(settings, "ollama_request_timeout_seconds", 600.0)
    monkeypatch.setattr(settings, "ollama_embed_batch_size", 2)
    provider = OllamaProvider()

    async def _request(inputs):
        return [[0.0] for _ in inputs]

    monkeypatch.setattr(provider, "_embed_inputs", _request)
    real_wait_for = asyncio.wait_for
    seen: list[float] = []

    async def _spy(coro, timeout):
        seen.append(timeout)
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr(embeddings.asyncio, "wait_for", _spy)

    out = await provider.embed_batch([f"c{i}" for i in range(5)])

    assert len(out) == 5
    # Three requests (2 + 2 + 1), each under the full setting: the bound is
    # neither divided across the note nor multiplied by it.
    assert seen == [600.0, 600.0, 600.0]


async def test_ollama_http_request_carries_the_setting(ollama, monkeypatch):
    monkeypatch.setattr(settings, "ollama_request_timeout_seconds", 600.0)
    with respx.mock(base_url="http://ollama:11434") as mock:
        route = mock.post("/api/embed").mock(
            return_value=Response(200, json={"embeddings": [[0.1]]})
        )
        await OllamaProvider().embed_one("q")
    assert route.calls[0].request.extensions["timeout"]["read"] == 600.0
    assert embeddings._ollama_client().timeout == httpx.Timeout(600.0)
    await embeddings.close_provider_client()


async def test_a_hung_ollama_request_fails_at_the_setting(ollama, monkeypatch):
    monkeypatch.setattr(settings, "ollama_request_timeout_seconds", 0.05)
    provider = OllamaProvider()

    async def _hangs(_inputs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(provider, "_embed_inputs", _hangs)
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        await provider.embed_batch(["a"])


# ── OpenAI: batch size, timeout, no retry on a read timeout ─────────────────


async def test_openai_batches_at_the_configured_size(openai, monkeypatch):
    monkeypatch.setattr(settings, "openai_embed_batch_size", 2)
    with respx.mock() as mock:
        route = mock.post("https://api.example.test/v1/embeddings").mock(
            side_effect=_openai_answer
        )
        out = await OpenAIProvider().embed_batch(["a", "b", "c", "d", "e"])
    sent = [json.loads(c.request.read())["input"] for c in route.calls]
    assert sent == [["a", "b"], ["c", "d"], ["e"]]
    assert len(out) == 5


async def test_openai_request_carries_the_setting(openai, monkeypatch):
    monkeypatch.setattr(settings, "openai_request_timeout_seconds", 900.0)
    with respx.mock() as mock:
        route = mock.post("https://api.example.test/v1/embeddings").mock(
            side_effect=_openai_answer
        )
        await OpenAIProvider().embed_one("q")
    assert route.calls[0].request.extensions["timeout"]["read"] == 900.0


async def test_openai_read_timeout_is_not_retried(openai, monkeypatch):
    async def _no_sleep(_t):
        return None

    monkeypatch.setattr(embeddings.asyncio, "sleep", _no_sleep)
    with respx.mock() as mock:
        route = mock.post("https://api.example.test/v1/embeddings").mock(
            side_effect=httpx.ReadTimeout("")
        )
        with pytest.raises(httpx.ReadTimeout):
            await OpenAIProvider().embed_one("q")
    assert route.call_count == 1


async def test_openai_connect_error_is_still_retried(openai, monkeypatch):
    async def _no_sleep(_t):
        return None

    monkeypatch.setattr(embeddings.asyncio, "sleep", _no_sleep)
    with respx.mock() as mock:
        route = mock.post("https://api.example.test/v1/embeddings")
        route.side_effect = [httpx.ConnectError("refused"), _openai_answer]
        out = await OpenAIProvider().embed_one("q")
    assert out == [0.1]
    assert route.call_count == 2
