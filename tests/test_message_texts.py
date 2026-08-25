"""Reply-channel reading, shared by every caller that reads model text.

Providers disagree about which field carries the answer: OpenAI-style
gateways use ``content``, reasoning models leave that empty and put the
answer in ``reasoning_content``, and OpenRouter uses ``reasoning``. The
SDK hands some callers an object and some a plain dict. Four places in
the harness were each reading this their own way; they now share one.
"""

from __future__ import annotations

from typing import Any

from surogates.harness.message_utils import message_texts


def _msg(**fields: Any) -> Any:
    return type("Msg", (), fields)()


def test_content_is_preferred() -> None:
    msg = _msg(content="answer", reasoning_content="thinking", reasoning="r")
    assert list(message_texts(msg)) == ["answer", "thinking", "r"]


def test_empty_content_falls_through_to_the_reasoning_channels() -> None:
    msg = _msg(content="", reasoning_content="answer")
    assert next(iter(message_texts(msg)), None) == "answer"


def test_whitespace_only_channels_are_skipped() -> None:
    msg = _msg(content="   ", reasoning_content="\n\t", reasoning="answer")
    assert list(message_texts(msg)) == ["answer"]


def test_openrouter_reasoning_channel() -> None:
    assert list(message_texts(_msg(content=None, reasoning="answer"))) == ["answer"]


def test_dict_shaped_messages() -> None:
    msg = {"content": None, "reasoning_content": "answer"}
    assert list(message_texts(msg)) == ["answer"]


def test_non_string_channels_are_skipped() -> None:
    # Tool-call replies carry a list content; it is not reply text.
    msg = _msg(content=[{"type": "text", "text": "x"}], reasoning="answer")
    assert list(message_texts(msg)) == ["answer"]


def test_nothing_usable_yields_nothing() -> None:
    assert list(message_texts(_msg(content=None))) == []
    assert list(message_texts(None)) == []
