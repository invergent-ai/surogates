"""Characterization tests for upstream-gateway error sentinels leaking to users.

Context
-------
Prod routes agent LLM traffic through a third-party gateway (yunwu.ai).
When that gateway's own upstream returns no content, it does **not** return
an error status -- it fabricates a normal-looking chat completion whose
``content`` is a hardcoded Chinese error string and attaches a bogus
tool_call with ``finish_reason='tool_calls'``:

    ⚠️ 上游模型未返回任何内容。可能原因：触发了安全策略、上游限流、
    或模型对当前输入直接结束。请重试或简化输入后再试。

Because it arrives as ordinary streamed ``content`` (plus a tool call), it
sails past every existing guard and is streamed live to the end user as an
``llm.delta`` event and persisted as the assistant message.

These tests pin the behaviour we want:
  * the sentinel is NEVER emitted to the user as a visible delta, and
  * it is NOT persisted as assistant content, and the bogus tool call is
    dropped, so the turn can be retried / failed over,
while LEGITIMATE content (including a legit "⚠️" warning) is untouched.

The first group is expected to FAIL against current code -- that failure is
the gap. The "must stay green" group protects functionality: whatever simple
fix we add must not eat real content.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.llm_call import call_llm_streaming_inner
from surogates.harness.stream_scrubbers import (
    StreamingContextScrubber,
    StreamingThinkScrubber,
)
from surogates.session.events import EventType

# NB: pyproject sets ``asyncio_mode = "auto"`` -- async tests are detected
# automatically, so no module-level asyncio mark (it would wrongly tag the
# one synchronous scrubber test below).


# The exact string the yunwu.ai gateway injects (observed in prod session
# 74e8245d-... on 2026-07-06).
SENTINEL = (
    "⚠️ 上游模型未返回任何内容。可能原因：触发了安全策略、上游限流、"
    "或模型对当前输入直接结束。请重试或简化输入后再试。"
)


# ---------------------------------------------------------------------------
# Fakes (mirrors tests/test_midstream_interrupt.py)
# ---------------------------------------------------------------------------


def _make_session() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), config={"temperature": 0.7}, model="gpt-4o")


def _text_chunk(
    *,
    content: str | None = None,
    finish_reason: str | None = None,
    role: str | None = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, role=role, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        model="gpt-4o",
        usage=None,
    )


def _tool_chunk(
    *,
    name: str,
    tool_id: str = "toolu_bdrk_bogus",
    finish_reason: str | None = None,
) -> SimpleNamespace:
    tool_delta = SimpleNamespace(
        index=0,
        id=tool_id,
        function=SimpleNamespace(name=name, arguments="{}"),
    )
    delta = SimpleNamespace(content=None, role=None, tool_calls=[tool_delta])
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        model="gpt-4o",
        usage=None,
    )


class _FakeStream:
    def __init__(self, chunks: list[Any]):
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


async def _run_stream(chunks: list[Any]) -> tuple[dict[str, Any], dict[str, Any], AsyncMock]:
    llm_client = MagicMock()
    llm_client.chat.completions.create = AsyncMock(return_value=_FakeStream(chunks))
    store = AsyncMock()
    msg, usage = await call_llm_streaming_inner(
        session=_make_session(),
        create_kwargs={"model": "gpt-4o", "messages": []},
        iteration=1,
        llm_client=llm_client,
        store=store,
        interrupt_check=lambda: False,
    )
    return msg, usage, store


def _emitted_delta_texts(store: AsyncMock) -> list[str]:
    """All visible content strings streamed to the user as llm.delta events."""
    texts: list[str] = []
    for call in store.emit_event.call_args_list:
        args = call.args
        if len(args) < 3:
            continue
        event_type = args[1]
        if event_type not in (EventType.LLM_DELTA, EventType.LLM_DELTA.value):
            continue
        payload = args[2]
        if isinstance(payload, dict) and payload.get("content"):
            texts.append(payload["content"])
    return texts


# ---------------------------------------------------------------------------
# THE GAP -- expected to FAIL against current code
# ---------------------------------------------------------------------------


class TestSentinelMustNeverReachTheUser:
    async def test_sentinel_not_streamed_as_delta_single_chunk(self) -> None:
        """The Chinese error must never be emitted as a visible delta."""
        chunks = [
            _text_chunk(content=SENTINEL, role="assistant"),
            _tool_chunk(name="browser_get_state"),
            _text_chunk(finish_reason="tool_calls"),
        ]
        _msg, _usage, store = await _run_stream(chunks)

        streamed = "".join(_emitted_delta_texts(store))
        assert SENTINEL not in streamed, (
            "gateway error string was streamed live to the user"
        )
        assert "上游模型" not in streamed, "raw CJK gateway error leaked to the user"

    async def test_sentinel_not_streamed_when_split_across_chunks(self) -> None:
        """Splitting the sentinel across deltas must not defeat suppression."""
        # A naive "== SENTINEL" check on a single delta would miss this.
        chunks = [
            _text_chunk(content="⚠️ 上游模型未返回", role="assistant"),
            _text_chunk(content="任何内容。可能原因：触发了安全策略、上游限流、"),
            _text_chunk(content="或模型对当前输入直接结束。请重试或简化输入后再试。"),
            _tool_chunk(name="browser_screenshot"),
            _text_chunk(finish_reason="tool_calls"),
        ]
        _msg, _usage, store = await _run_stream(chunks)

        streamed = "".join(_emitted_delta_texts(store))
        assert "上游模型" not in streamed, "split gateway error leaked to the user"

    async def test_sentinel_dropped_from_final_message(self) -> None:
        """The sentinel must not persist as assistant content, and the bogus
        tool call must be dropped so the turn can retry / fail over."""
        chunks = [
            _text_chunk(content=SENTINEL, role="assistant"),
            _tool_chunk(name="browser_get_state"),
            _text_chunk(finish_reason="tool_calls"),
        ]
        msg, usage, _store = await _run_stream(chunks)

        assert SENTINEL not in (msg.get("content") or ""), (
            "gateway error persisted as assistant content"
        )
        assert not msg.get("tool_calls"), "bogus tool call from error turn was kept"
        assert usage.get("upstream_error_sentinel") is True, (
            "turn not flagged as an upstream sentinel for retry/failover"
        )


# ---------------------------------------------------------------------------
# GUARDRAILS -- must STAY green (fix must not affect real functionality)
# ---------------------------------------------------------------------------


class TestLegitimateContentUnaffected:
    async def test_normal_content_still_streamed(self) -> None:
        chunks = [
            _text_chunk(content="Here is ", role="assistant"),
            _text_chunk(content="your answer."),
            _text_chunk(finish_reason="stop"),
        ]
        msg, _usage, store = await _run_stream(chunks)

        assert msg["content"] == "Here is your answer."
        assert "".join(_emitted_delta_texts(store)) == "Here is your answer."

    async def test_legit_warning_emoji_prefix_not_suppressed(self) -> None:
        """A real ⚠️ warning that merely SHARES the leading emoji must survive."""
        legit = "⚠️ Note: the disk is almost full, consider cleanup."
        chunks = [
            _text_chunk(content=legit, role="assistant"),
            _text_chunk(finish_reason="stop"),
        ]
        msg, _usage, store = await _run_stream(chunks)

        assert msg["content"] == legit
        assert "".join(_emitted_delta_texts(store)) == legit

    async def test_legit_content_with_tool_call_preserved(self) -> None:
        chunks = [
            _text_chunk(content="Let me check that.", role="assistant"),
            _tool_chunk(name="web_search", finish_reason="tool_calls"),
        ]
        msg, _usage, _store = await _run_stream(chunks)

        assert msg["content"] == "Let me check that."
        assert msg.get("tool_calls"), "legit tool call was dropped"


# ---------------------------------------------------------------------------
# WHY it leaks -- the existing scrubbers do not recognise the sentinel
# ---------------------------------------------------------------------------


def test_existing_scrubbers_pass_the_sentinel_through_unchanged() -> None:
    """Documents the gap: neither existing scrubber is the fix point.

    The streaming pipeline is
    ``context_scrubber.feed(think_scrubber.feed(text))`` -- both let the
    gateway error through verbatim, which is why it reaches the user.
    """
    think = StreamingThinkScrubber()
    context = StreamingContextScrubber()
    visible = context.feed(think.feed(SENTINEL)) + context.feed(think.flush()) + context.flush()
    assert visible == SENTINEL  # unchanged -> nothing strips it today


# ---------------------------------------------------------------------------
# Non-streaming path -- the sentinel must never be persisted or returned
# ---------------------------------------------------------------------------


def _non_streaming_response(content: str, *, tool_calls: list[Any] | None = None):
    message = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(
        message=message,
        finish_reason="tool_calls" if tool_calls else "stop",
    )
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    return SimpleNamespace(choices=[choice], usage=usage, model="claude-opus-4-8")


async def _run_non_streaming(response: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    from surogates.harness.llm_call import call_llm_non_streaming

    llm_client = MagicMock()
    llm_client.base_url = "https://yunwu.ai/v1"
    llm_client.chat.completions.create = AsyncMock(return_value=response)
    return await call_llm_non_streaming(
        session=_make_session(),
        create_kwargs={"model": "claude-opus-4-8", "messages": []},
        iteration=1,
        llm_client=llm_client,
        store=None,
    )


class TestNonStreamingSentinelGuard:
    async def test_sentinel_blanked_and_flagged(self) -> None:
        response = _non_streaming_response(
            SENTINEL,
            tool_calls=[
                {
                    "id": "toolu_bdrk_bogus",
                    "type": "function",
                    "function": {"name": "browser_get_state", "arguments": "{}"},
                }
            ],
        )
        msg, usage = await _run_non_streaming(response)

        assert msg.get("content", "") == ""
        assert not msg.get("tool_calls")
        assert usage.get("upstream_error_sentinel") is True

    async def test_legit_content_untouched(self) -> None:
        msg, usage = await _run_non_streaming(_non_streaming_response("All good."))

        assert msg["content"] == "All good."
        assert "upstream_error_sentinel" not in usage

    async def test_structured_list_content_sentinel_blanked(self) -> None:
        # Content may arrive as a list of blocks (multimodal/structured). The
        # guard must flatten it (no AttributeError) and still detect the error.
        response = _non_streaming_response(
            [{"type": "text", "text": SENTINEL}],
        )
        msg, usage = await _run_non_streaming(response)

        assert msg.get("content", "") == ""
        assert usage.get("upstream_error_sentinel") is True

    async def test_structured_list_content_legit_untouched(self) -> None:
        content = [{"type": "text", "text": "All good."}]
        response = _non_streaming_response(content)
        msg, usage = await _run_non_streaming(response)

        assert msg["content"] == content
        assert "upstream_error_sentinel" not in usage


# ---------------------------------------------------------------------------
# Retry / failover routing in call_llm_with_retry
# ---------------------------------------------------------------------------


async def _run_with_retry(
    *,
    stream_side_effect: Any,
    activate_fallback: Any,
    get_current_model: Any,
    interrupt_check: Any = None,
    on_stream_retry: Any = None,
):
    from unittest.mock import patch

    from surogates.harness.llm_call import call_llm_with_retry

    sleep_mock = AsyncMock()
    with (
        patch(
            "surogates.harness.llm_call.call_llm_streaming",
            AsyncMock(side_effect=stream_side_effect),
        ) as streaming_mock,
        # Patch the interruptible backoff so the retry path uses no real delay.
        patch("surogates.harness.llm_call.interruptible_sleep", sleep_mock),
    ):
        result = await call_llm_with_retry(
            session=_make_session(),
            create_kwargs={"model": "claude-opus-4-8", "messages": []},
            iteration=1,
            llm_client=MagicMock(),
            store=AsyncMock(),
            streaming_enabled=True,
            interrupt_check=interrupt_check or (lambda: False),
            activate_fallback=activate_fallback,
            get_current_model=get_current_model,
            set_streaming_enabled=lambda _enabled: None,
            on_stream_retry=on_stream_retry,
        )
    return result, streaming_mock, sleep_mock


_SENTINEL_RESULT = ({"role": "assistant", "content": ""}, {"upstream_error_sentinel": True})
_GOOD_RESULT = ({"role": "assistant", "content": "recovered"}, {})


class TestRetryRoutingOnSentinel:
    async def test_fails_over_to_backup_provider(self) -> None:
        """A sentinel with a fallback available switches provider and retries."""
        activate_fallback = MagicMock(return_value=True)
        get_current_model = MagicMock(return_value="backup-model")

        (msg, _usage), streaming_mock, _sleep = await _run_with_retry(
            stream_side_effect=[_SENTINEL_RESULT, _GOOD_RESULT],
            activate_fallback=activate_fallback,
            get_current_model=get_current_model,
        )

        assert msg["content"] == "recovered"
        assert activate_fallback.called
        assert streaming_mock.await_count == 2

    async def test_retries_in_place_then_raises_when_no_fallback(self) -> None:
        """No fallback: retry up to the cap, then surface an empty-response
        error (never the Chinese sentinel)."""
        activate_fallback = MagicMock(return_value=False)
        get_current_model = MagicMock(return_value=None)

        with pytest.raises(ValueError, match="empty response"):
            await _run_with_retry(
                stream_side_effect=[_SENTINEL_RESULT] * 5,
                activate_fallback=activate_fallback,
                get_current_model=get_current_model,
            )

    async def test_recovers_in_place_without_fallback(self) -> None:
        """No fallback but the gateway clears on the next attempt -> success,
        transparent to the user."""
        activate_fallback = MagicMock(return_value=False)
        get_current_model = MagicMock(return_value=None)

        (msg, _usage), streaming_mock, _sleep = await _run_with_retry(
            stream_side_effect=[_SENTINEL_RESULT, _GOOD_RESULT],
            activate_fallback=activate_fallback,
            get_current_model=get_current_model,
        )

        assert msg["content"] == "recovered"
        assert streaming_mock.await_count == 2

    async def test_in_place_backoff_is_interruptible(self) -> None:
        """The retry backoff must use the interruptible sleep wired to
        interrupt_check, so a user Stop is honoured mid-backoff."""
        activate_fallback = MagicMock(return_value=False)
        get_current_model = MagicMock(return_value=None)
        interrupt_check = lambda: False  # noqa: E731

        (msg, _usage), _streaming, sleep_mock = await _run_with_retry(
            stream_side_effect=[_SENTINEL_RESULT, _GOOD_RESULT],
            activate_fallback=activate_fallback,
            get_current_model=get_current_model,
            interrupt_check=interrupt_check,
        )

        assert msg["content"] == "recovered"
        assert sleep_mock.await_count == 1
        # Second positional arg is the interrupt callback (not a bare sleep).
        _seconds, passed_interrupt = sleep_mock.await_args.args
        assert passed_interrupt is interrupt_check

    async def test_sentinel_retry_discards_streaming_executor(self) -> None:
        """A fabricated turn may dispatch a bogus tool into the executor; the
        retry must discard it via on_stream_retry before re-issuing."""
        activate_fallback = MagicMock(return_value=False)
        get_current_model = MagicMock(return_value=None)
        fresh_cb = MagicMock()
        on_stream_retry = MagicMock(return_value=fresh_cb)

        (msg, _usage), _streaming, _sleep = await _run_with_retry(
            stream_side_effect=[_SENTINEL_RESULT, _GOOD_RESULT],
            activate_fallback=activate_fallback,
            get_current_model=get_current_model,
            on_stream_retry=on_stream_retry,
        )

        assert msg["content"] == "recovered"
        assert on_stream_retry.called, "executor was not discarded on sentinel retry"


# ---------------------------------------------------------------------------
# Safety: what the user actually sees in the worst case (all retries fail)
# ---------------------------------------------------------------------------


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


class TestTerminalErrorIsSafe:
    async def test_exhausted_retries_surface_clean_english_error(self) -> None:
        """When every attempt is a sentinel and there is no fallback, the error
        that reaches the user is a fixed English classification -- never the
        raw Chinese string."""
        from surogates.harness.error_classify import classify_harness_error

        activate_fallback = MagicMock(return_value=False)
        get_current_model = MagicMock(return_value=None)

        raised: Exception | None = None
        try:
            await _run_with_retry(
                stream_side_effect=[_SENTINEL_RESULT] * 5,
                activate_fallback=activate_fallback,
                get_current_model=get_current_model,
            )
        except Exception as exc:  # noqa: BLE001 - asserting on the surfaced error
            raised = exc

        assert raised is not None
        # The exception message itself carries no gateway text.
        assert not _has_cjk(str(raised)), "raw CJK leaked into the raised error"

        # This is exactly what the loop feeds to the user-facing crash event.
        info = classify_harness_error(raised)
        assert info.category == "invalid_response"
        assert info.title == "The model returned an empty or malformed response."
        assert not _has_cjk(info.title)
        assert not _has_cjk(info.detail)
        assert info.retryable is True

    def test_sentinel_itself_would_be_flagged_cjk(self) -> None:
        """Sanity check the CJK detector actually fires on the gateway string."""
        assert _has_cjk(SENTINEL) is True
