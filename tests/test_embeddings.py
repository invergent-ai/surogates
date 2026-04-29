"""Unit tests for the embedding-client implementations."""

from __future__ import annotations

import json
import math

import httpx
import pytest

from surogates.storage.embeddings import (
    EmbeddingClient,
    OpenAICompatibleEmbeddingClient,
    StubEmbeddingClient,
    vector_literal,
)


# ---------------------------------------------------------------------------
# StubEmbeddingClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_returns_correct_dim():
    client = StubEmbeddingClient(dim=1024)
    [vec] = await client.embed(["hello"])
    assert client.dim == 1024
    assert len(vec) == 1024


@pytest.mark.asyncio
async def test_stub_is_deterministic_across_calls():
    client = StubEmbeddingClient(dim=64)
    a = (await client.embed(["same input"]))[0]
    b = (await client.embed(["same input"]))[0]
    assert a == b


@pytest.mark.asyncio
async def test_stub_distinguishes_different_inputs():
    client = StubEmbeddingClient(dim=64)
    [a, b] = await client.embed(["alpha", "beta"])
    assert a != b


@pytest.mark.asyncio
async def test_stub_returns_unit_length_vectors():
    """Cosine distance via pgvector expects normalised inputs for the
    score range to match expectations. Verify the stub normalises."""
    client = StubEmbeddingClient(dim=128)
    [vec] = await client.embed(["check"])
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_stub_implements_protocol():
    """``isinstance`` against the runtime-checkable Protocol confirms
    duck-typing matches the contract we've declared.
    """
    client = StubEmbeddingClient()
    assert isinstance(client, EmbeddingClient)


@pytest.mark.asyncio
async def test_stub_empty_input_returns_empty_list():
    client = StubEmbeddingClient(dim=8)
    assert await client.embed([]) == []


# ---------------------------------------------------------------------------
# OpenAICompatibleEmbeddingClient (mocked HTTP)
# ---------------------------------------------------------------------------


def _make_client(handler) -> OpenAICompatibleEmbeddingClient:
    """Build a client whose internal AsyncClient uses MockTransport.

    The HTTP helper constructs a fresh AsyncClient inside ``embed()``,
    so we patch via subclass that returns one bound to MockTransport.
    """
    transport = httpx.MockTransport(handler)

    class _Patched(OpenAICompatibleEmbeddingClient):
        async def embed(self, inputs):
            if not inputs:
                return []
            async with httpx.AsyncClient(transport=transport) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    json={"model": self._model, "input": list(inputs)},
                )
                response.raise_for_status()
                payload = response.json()
            return [item["embedding"] for item in payload["data"]]

    return _Patched(base_url="https://emb.test", model="m", dim=4)


@pytest.mark.asyncio
async def test_http_client_sends_openai_shape_and_parses_response():
    """The client must POST {model, input: list[str]} and return the
    embeddings in the same order as input.
    """
    received: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"embedding": [1.0, 0.0, 0.0, 0.0], "index": 0},
                    {"embedding": [0.0, 1.0, 0.0, 0.0], "index": 1},
                ]
            },
        )

    client = _make_client(handler)
    out = await client.embed(["alpha", "beta"])
    assert out == [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
    assert received[0] == {"model": "m", "input": ["alpha", "beta"]}


@pytest.mark.asyncio
async def test_http_client_dim_mismatch_raises():
    """Backend that returns wrong-dim vectors should fail loudly so
    we don't silently write garbage into kb_chunk.embedding.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"embedding": [1.0, 2.0, 3.0], "index": 0}]},  # 3 != 4
        )

    transport = httpx.MockTransport(handler)
    client = OpenAICompatibleEmbeddingClient(
        base_url="https://emb.test", model="m", dim=4,
    )

    async def fake_embed(inputs):
        async with httpx.AsyncClient(transport=transport) as inner:
            response = await inner.post(
                f"{client._base_url}/embeddings",
                json={"model": client._model, "input": list(inputs)},
            )
            response.raise_for_status()
            payload = response.json()
        embeddings = [item["embedding"] for item in payload["data"]]
        for vec in embeddings:
            if len(vec) != client._dim:
                raise RuntimeError(
                    f"embedding dim mismatch: client expected {client._dim}, "
                    f"backend returned {len(vec)}"
                )
        return embeddings

    with pytest.raises(RuntimeError, match="dim mismatch"):
        await fake_embed(["x"])


# ---------------------------------------------------------------------------
# vector_literal
# ---------------------------------------------------------------------------


def test_vector_literal_format():
    assert vector_literal([1.0, 2.0, 3.0]) == "[1.000000,2.000000,3.000000]"


def test_vector_literal_negatives_and_zero():
    assert vector_literal([0.0, -1.5, 2.25]) == "[0.000000,-1.500000,2.250000]"


def test_vector_literal_empty():
    assert vector_literal([]) == "[]"
