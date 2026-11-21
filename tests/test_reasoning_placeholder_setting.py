"""What a reasoning-less tool-call turn carries when the history is re-sent: ``llm.reasoning_placeholder``.

Default ``placeholder`` keeps "(no reasoning recorded)", which a thinking model that refuses a tool call without
reasoning (DeepSeek) needs. A model that reads it as reasoning can copy it as its own: Inkling-2-Mini did in 5-19 % of
its calls on 11-21, every copy right after a reasoning-less turn, its reasoning shrinking once the placeholder was in
the history. ``empty`` sends an empty field, ``omit`` none (vLLM takes a reasoning-less turn).
"""

from __future__ import annotations

import surogates.harness.loop as loop
from surogates.config import LLMSettings
from surogates.harness.loop import REASONING_PLACEHOLDER, _to_api_messages

REASONED = {"role": "assistant", "content": "ok", "reasoning": "thinking"}
BARE_CALL = {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function",
                                                                  "function": {"name": "f", "arguments": "{}"}}]}


def test_the_default_is_the_placeholder_as_before():
    api = _to_api_messages([REASONED, BARE_CALL], "placeholder")
    assert api[0]["reasoning_content"] == "thinking"
    assert api[1]["reasoning_content"] == REASONING_PLACEHOLDER
    assert LLMSettings().reasoning_placeholder == "placeholder"


def test_empty_sends_an_empty_field():
    api = _to_api_messages([REASONED, BARE_CALL], "empty")
    assert api[1]["reasoning_content"] == ""
    assert api[0]["reasoning_content"] == "thinking"


def test_omit_sends_no_field_and_keeps_real_reasoning():
    api = _to_api_messages([REASONED, BARE_CALL], "omit")
    assert "reasoning_content" not in api[1]
    assert api[0]["reasoning_content"] == "thinking"


def test_the_deployment_setting_is_what_a_request_uses(monkeypatch):
    monkeypatch.setattr(loop, "_reasoning_fill", lambda: "omit")
    assert "reasoning_content" not in _to_api_messages([REASONED, BARE_CALL])[1]
    monkeypatch.setattr(loop, "_reasoning_fill", lambda: "placeholder")
    assert _to_api_messages([REASONED, BARE_CALL])[1]["reasoning_content"] == REASONING_PLACEHOLDER


def test_a_history_without_reasoning_gets_none_whatever_the_setting():
    for fill in ("placeholder", "empty", "omit"):
        assert "reasoning_content" not in _to_api_messages([BARE_CALL], fill)[0]


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("SUROGATES_LLM_REASONING_PLACEHOLDER", "omit")
    assert LLMSettings().reasoning_placeholder == "omit"
