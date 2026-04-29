"""Embedding-client abstraction for the KB retrieval layer.

Two implementations:

  - :class:`StubEmbeddingClient` — deterministic, hash-based, in-process.
    Same input → same vector across runs. Works offline. Used by the
    default test path so unit + integration suites stay fast.

  - :class:`OpenAICompatibleEmbeddingClient` — POSTs to an
    ``/embeddings`` endpoint that follows the OpenAI request shape:
    ``{"model": <name>, "input": [<string>, ...]}`` returning
    ``{"data": [{"embedding": [...], "index": <int>}, ...]}``. Works
    with HuggingFace text-embeddings-inference (TEI), OpenAI itself,
    vLLM in OpenAI-compat mode, and most self-hosted equivalents.

Production is wired to the HTTP client; opt-in tests can spin up a
real TEI container to exercise the full embedding path. Switching is
a config change, not a code change.
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol, Sequence, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingClient(Protocol):
    """Async embedding client. ``embed()`` returns one vector per input.

    Implementations declare a fixed ``dim`` so the KB schema can match
    its ``kb_chunk.embedding vector(N)`` column.
    """

    @property
    def dim(self) -> int:
        ...

    async def embed(self, inputs: Sequence[str]) -> list[list[float]]:
        ...


# ---------------------------------------------------------------------------
# Stub
# ---------------------------------------------------------------------------


class StubEmbeddingClient:
    """Deterministic, hash-derived embeddings.

    Same input produces the same vector; different inputs produce
    different vectors. The vectors are NOT semantically meaningful —
    cosine similarity between two unrelated chunks is essentially
    random. Tests that exercise retrieval *plumbing* (RRF merge,
    chunk indexing, etc.) work fine. Tests that need
    *semantic* match should use the real client.
    """

    def __init__(self, dim: int = 1024) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, inputs: Sequence[str]) -> list[list[float]]:
        return [self._stable_vector(s) for s in inputs]

    def _stable_vector(self, s: str) -> list[float]:
        """Build a unit-length vector from sha256 of *s* + position seeds.

        We produce ``self._dim`` floats by hashing ``s`` together with
        the position index and converting the leading 8 bytes of each
        digest to a float in ``[-1, 1]``. Then normalise to unit length
        so cosine distance lands in ``[0, 2]`` as expected by pgvector.
        """
        seed = s.encode("utf-8")
        out: list[float] = []
        for i in range(self._dim):
            h = hashlib.sha256(seed + i.to_bytes(4, "big")).digest()
            n = int.from_bytes(h[:8], "big", signed=False)
            # n is uint64; scale to [-1, 1).
            f = (n / (1 << 63)) - 1.0
            out.append(f)
        norm = math.sqrt(sum(x * x for x in out)) or 1.0
        return [x / norm for x in out]


# ---------------------------------------------------------------------------
# OpenAI-compatible HTTP
# ---------------------------------------------------------------------------


class OpenAICompatibleEmbeddingClient:
    """POSTs ``base_url/embeddings`` with an OpenAI-shaped request.

    Confirmed working backends:
      - HuggingFace text-embeddings-inference (TEI) — primary target
        for self-hosted defaults (mxbai-embed-large-v1, BGE).
      - OpenAI's ``/v1/embeddings`` (with API key).
      - vLLM ``--task=embed`` mode.
      - Most LLM-gateway proxies that forward the OpenAI shape.

    The ``model`` arg is forwarded to the backend; servers that accept
    only one model often ignore it. ``dim`` is asserted on the first
    successful response so a misconfigured backend doesn't silently
    write wrong-dimensioned vectors into ``kb_chunk``.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dim: int,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dim
        self._api_key = api_key
        self._timeout = timeout

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, inputs: Sequence[str]) -> list[list[float]]:
        if not inputs:
            return []
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers=headers,
        ) as client:
            response = await client.post(
                f"{self._base_url}/embeddings",
                json={"model": self._model, "input": list(inputs)},
            )
            response.raise_for_status()
            payload = response.json()

        try:
            embeddings = [item["embedding"] for item in payload["data"]]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"unexpected /embeddings response shape: {payload!r}"
            ) from exc

        # Sanity-check dim on every batch — cheap insurance against a
        # backend reload that swapped in a different model.
        for vec in embeddings:
            if len(vec) != self._dim:
                raise RuntimeError(
                    f"embedding dim mismatch: client expected {self._dim}, "
                    f"backend returned {len(vec)} (model={self._model!r}, "
                    f"endpoint={self._base_url!r})"
                )
        return embeddings


# ---------------------------------------------------------------------------
# pgvector serialisation helper
# ---------------------------------------------------------------------------


def vector_literal(vec: Sequence[float]) -> str:
    """Format a list of floats as a pgvector literal: ``'[0.1,0.2,...]'``.

    The pgvector extension accepts either binary or text input; the
    text form ``[x,y,z]`` (no spaces necessary) works across asyncpg
    + SQLAlchemy without dialect-specific bind setup.
    """
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
