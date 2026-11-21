"""A prompt over the window is compacted and asked again, not a failed session.

11-21 (coding-route reads, 262,144-token window): two sessions FAILED after 53 and 87 minutes of work with the engine's
"prepared prompt has 262176 tokens, exceeding Engine max_context 262144" (code context_length_exceeded): no
detector knew the wording, so the overflow was taken for a non-retryable client error. Now the wording is a context
overflow, the session compacts and asks again, and a provider that names the window already assumed keeps it (the
old step-down halved 262,144 to 128,000 although the window was right).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
from openai import APIStatusError

from surogates.harness import llm_call
from surogates.harness.context import ContextCompressor
from surogates.harness.error_classify import classify_harness_error
from surogates.harness.llm_call import call_llm_with_retry
from surogates.harness.model_metadata import parse_context_limit_from_error

ENGINE_TEXT = ("Error code: 400 - {'error': {'code': 'context_length_exceeded', 'message': 'prepared prompt has 262176 "
               "tokens, exceeding Engine max_context 262144', 'param': 'messages', 'type': 'invalid_request_error'}}")


def _overflow() -> APIStatusError:
    request = httpx.Request("POST", "http://engine.test/v1/chat/completions")
    body = {"error": {"code": "context_length_exceeded", "message": "prepared prompt has 262176 tokens, exceeding "
                      "Engine max_context 262144", "param": "messages", "type": "invalid_request_error"}}
    return APIStatusError(ENGINE_TEXT, response=httpx.Response(400, request=request), body=body)


def test_the_engines_wording_is_a_context_overflow():
    assert parse_context_limit_from_error(ENGINE_TEXT) == 262_144
    info = classify_harness_error(_overflow())
    assert info.category == "context_overflow"


BARE_TEXT = "Error code: 400 - unknown parameter max_context_tokens"


def _bare() -> APIStatusError:
    request = httpx.Request("POST", "http://engine.test/v1/chat/completions")
    return APIStatusError(BARE_TEXT, response=httpx.Response(400, request=request),
                          body={"error": {"message": "unknown parameter max_context_tokens"}})


def test_a_bare_max_context_word_is_not_an_overflow():
    assert classify_harness_error(_bare()).category != "context_overflow"
    # The UI classifier's own phrase list (an error that is not an API status error).
    assert classify_harness_error(Exception(BARE_TEXT)).category != "context_overflow"
    assert classify_harness_error(Exception(ENGINE_TEXT)).category == "context_overflow"


async def _ask(monkeypatch, compressor=None, error=None):
    sent: list = []

    async def engine(*, create_kwargs, **kwargs):
        sent.append(create_kwargs["messages"])
        if len(sent) == 1:
            raise error if error is not None else _overflow()
        return {"role": "assistant", "content": "done"}, {"prompt_tokens": 10, "completion_tokens": 1}

    compressed = [{"role": "user", "content": "summary of the work so far"}]
    compress = AsyncMock(return_value=compressed)
    monkeypatch.setattr(llm_call, "call_llm_streaming_inner", engine)
    monkeypatch.setattr(llm_call, "call_llm_non_streaming", AsyncMock(side_effect=AssertionError("no fallback")))
    monkeypatch.setattr(llm_call, "interruptible_sleep", AsyncMock(return_value=None))
    try:
        reply, _usage = await call_llm_with_retry(
            session=SimpleNamespace(id=uuid4(), config={}),
            create_kwargs={"model": "m", "messages": [{"role": "user", "content": "x" * 1000}]},
            iteration=1, llm_client=MagicMock(), store=AsyncMock(), streaming_enabled=True,
            interrupt_check=lambda: False, activate_fallback=lambda: False, get_current_model=lambda: None,
            set_streaming_enabled=lambda _on: None, compress_context=compress, context_compressor=compressor,
        )
    except Exception as exc:  # the caller's answer to a non-retryable error
        return exc, compress, sent
    return reply, compress, sent


async def test_the_overflow_is_compacted_and_asked_again(monkeypatch):
    reply, compress, sent = await _ask(monkeypatch)
    assert reply["content"] == "done"
    compress.assert_awaited_once()
    assert sent[1][-1]["content"] == "summary of the work so far"


async def test_the_named_window_already_assumed_is_kept(monkeypatch):
    compressor = ContextCompressor("engine-model", context_window=262_144, quiet_mode=True)
    reply, compress, _sent = await _ask(monkeypatch, compressor)
    assert reply["content"] == "done"
    compress.assert_awaited_once()
    assert compressor.context_length == 262_144


async def test_a_smaller_named_window_is_adopted(monkeypatch):
    compressor = ContextCompressor("engine-model", context_window=300_000, quiet_mode=True)
    await _ask(monkeypatch, compressor)
    assert compressor.context_length == 262_144


async def test_a_bare_max_context_word_is_not_compacted(monkeypatch):
    result, compress, sent = await _ask(monkeypatch, error=_bare())
    compress.assert_not_awaited()
    assert len(sent) == 1 and not (isinstance(result, dict) and result.get("content") == "done")


async def test_a_larger_named_window_is_not_a_reason_to_halve_it(monkeypatch):
    compressor = ContextCompressor("engine-model", context_window=200_000, quiet_mode=True)
    await _ask(monkeypatch, compressor)
    assert compressor.context_length == 200_000
