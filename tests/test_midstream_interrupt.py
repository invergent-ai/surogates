"""Tests for mid-stream interrupt support in call_llm_streaming_inner.

Validates that when interrupt_check() returns True during streaming,
the HTTP stream is cancelled and a partial response is returned.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.llm_call import call_llm_streaming_inner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session() -> SimpleNamespace:
    """Create a minimal session-like object for tests."""
    return SimpleNamespace(
        id=uuid4(),
        config={"temperature": 0.7},
        model="gpt-4o",
    )


def _make_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    tool_calls: list | None = None,
    role: str | None = None,
    model: str = "gpt-4o",
    usage: Any = None,
) -> SimpleNamespace:
    """Create a fake streaming chunk."""
    delta = SimpleNamespace(
        content=content,
        role=role,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(
        delta=delta,
        finish_reason=finish_reason,
    )
    return SimpleNamespace(
        choices=[choice],
        model=model,
        usage=usage,
    )


async def _async_iter(items):
    """Convert a list to an async iterator."""
    for item in items:
        yield item


class _FakeStream:
    """Fake async streaming response that supports aclose()."""

    def __init__(self, chunks: list):
        self._chunks = chunks
        self._index = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed or self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def aclose(self):
        self.closed = True


class _FakeStreamWithSyncClose:
    """Fake streaming response with sync close() method."""

    def __init__(self, chunks: list):
        self._chunks = chunks
        self._index = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed or self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMidStreamInterrupt:
    async def test_no_interrupt_completes_normally(self) -> None:
        """Without interrupt, all chunks are processed normally."""
        chunks = [
            _make_chunk(content="Hello", role="assistant"),
            _make_chunk(content=" world"),
            _make_chunk(finish_reason="stop"),
        ]
        stream = _FakeStream(chunks)

        llm_client = MagicMock()
        llm_client.chat.completions.create = AsyncMock(return_value=stream)
        store = AsyncMock()

        session = _make_session()
        msg, usage = await call_llm_streaming_inner(
            session=session,
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=lambda: False,
        )

        assert msg["content"] == "Hello world"
        assert usage["finish_reason"] == "stop"

    async def test_interrupt_cancels_stream(self) -> None:
        """Interrupt during streaming stops processing and returns partial content."""
        call_count = 0

        def interrupt_check():
            nonlocal call_count
            call_count += 1
            # Interrupt after the first chunk is processed.
            return call_count > 1

        chunks = [
            _make_chunk(content="Hello", role="assistant"),
            _make_chunk(content=" world"),
            _make_chunk(content=" more"),
            _make_chunk(finish_reason="stop"),
        ]
        stream = _FakeStream(chunks)

        llm_client = MagicMock()
        llm_client.chat.completions.create = AsyncMock(return_value=stream)
        store = AsyncMock()

        session = _make_session()
        msg, usage = await call_llm_streaming_inner(
            session=session,
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=interrupt_check,
        )

        # Only the first chunk's content should be present.
        assert msg["content"] == "Hello"
        assert usage["finish_reason"] == "interrupted"
        assert stream.closed is True

    async def test_interrupt_on_first_chunk(self) -> None:
        """Interrupt before any content is processed."""
        chunks = [
            _make_chunk(content="Hello", role="assistant"),
            _make_chunk(content=" world"),
        ]
        stream = _FakeStream(chunks)

        llm_client = MagicMock()
        llm_client.chat.completions.create = AsyncMock(return_value=stream)
        store = AsyncMock()

        session = _make_session()
        msg, usage = await call_llm_streaming_inner(
            session=session,
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=lambda: True,  # immediate interrupt
        )

        assert msg.get("content", "") == ""
        assert usage["finish_reason"] == "interrupted"

    async def test_none_interrupt_check_proceeds_normally(self) -> None:
        """interrupt_check=None means no interrupt checking."""
        chunks = [
            _make_chunk(content="Hello", role="assistant"),
            _make_chunk(finish_reason="stop"),
        ]
        stream = _FakeStream(chunks)

        llm_client = MagicMock()
        llm_client.chat.completions.create = AsyncMock(return_value=stream)
        store = AsyncMock()

        session = _make_session()
        msg, usage = await call_llm_streaming_inner(
            session=session,
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=None,
        )

        assert msg["content"] == "Hello"
        assert usage["finish_reason"] == "stop"

    async def test_interrupt_with_sync_close(self) -> None:
        """Stream with only sync close() (not aclose()) still works."""
        call_count = 0

        def interrupt_check():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        chunks = [
            _make_chunk(content="partial", role="assistant"),
            _make_chunk(content=" more"),
        ]
        stream = _FakeStreamWithSyncClose(chunks)

        llm_client = MagicMock()
        llm_client.chat.completions.create = AsyncMock(return_value=stream)
        store = AsyncMock()

        session = _make_session()
        msg, usage = await call_llm_streaming_inner(
            session=session,
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
            interrupt_check=interrupt_check,
        )

        assert msg["content"] == "partial"
        assert usage["finish_reason"] == "interrupted"
        assert stream.closed is True

    async def test_prompt_cache_extra_body_injected_for_claude(self) -> None:
        """Verify that cache extra_body is injected for Claude models."""
        chunks = [
            _make_chunk(content="Hi", role="assistant"),
            _make_chunk(finish_reason="stop"),
        ]
        stream = _FakeStream(chunks)

        llm_client = MagicMock()
        create_mock = AsyncMock(return_value=stream)
        llm_client.chat.completions.create = create_mock
        store = AsyncMock()

        session = _make_session()
        await call_llm_streaming_inner(
            session=session,
            create_kwargs={"model": "claude-sonnet-4-20250514", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
        )

        # Verify that extra_body was passed to the create call.
        call_kwargs = create_mock.call_args
        assert "extra_body" in call_kwargs.kwargs or any(
            "extra_body" in str(k) for k in (call_kwargs.kwargs or {})
        )

    async def test_no_cache_extra_body_for_gpt(self) -> None:
        """Verify that no cache extra_body is injected for non-Claude models."""
        chunks = [
            _make_chunk(content="Hi", role="assistant"),
            _make_chunk(finish_reason="stop"),
        ]
        stream = _FakeStream(chunks)

        llm_client = MagicMock()
        create_mock = AsyncMock(return_value=stream)
        llm_client.chat.completions.create = create_mock
        store = AsyncMock()

        session = _make_session()
        await call_llm_streaming_inner(
            session=session,
            create_kwargs={"model": "gpt-4o", "messages": []},
            iteration=1,
            llm_client=llm_client,
            store=store,
        )

        # Verify that extra_body was NOT passed.
        call_kwargs = create_mock.call_args
        passed_kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        # The kwargs are spread as **final_kwargs, so check the positional
        # kwargs dict.  In the mock, the call is create(**final_kwargs, stream=True).
        # So we check the kwargs for "extra_body".
        assert "extra_body" not in passed_kwargs
