"""What the agent typed into a page never reaches a stored or shown copy of its tool call."""

from __future__ import annotations

import json

from surogates.harness.redact import mask_tool_arguments, redact_sensitive_data, typed_texts

PASSWORD = "Waalhaven-2026"
BULLETS = "•" * len(PASSWORD)


def test_a_tool_call_events_arguments_carry_the_length_never_the_text() -> None:
    stored = redact_sensitive_data({
        "tool_call_id": "c1", "name": "browser_type", "arguments": {"text": PASSWORD, "ref": "@e5"},
    })
    assert stored["arguments"] == {"text": BULLETS, "ref": "@e5"}


def test_an_llm_responses_tool_calls_are_masked_in_their_json_string() -> None:
    stored = redact_sensitive_data({"message": {"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {
            "name": "browser_type", "arguments": json.dumps({"ref": "@e5", "text": PASSWORD}),
        }},
        {"id": "c2", "type": "function", "function": {
            "name": "browser_navigate", "arguments": json.dumps({"url": "https://acme.test/"}),
        }},
    ]}})
    calls = stored["message"]["tool_calls"]
    assert PASSWORD not in json.dumps(stored)
    assert json.loads(calls[0]["function"]["arguments"]) == {"ref": "@e5", "text": BULLETS}
    assert json.loads(calls[1]["function"]["arguments"]) == {"url": "https://acme.test/"}


def test_compacted_messages_are_masked_at_any_depth() -> None:
    stored = redact_sensitive_data({"compacted_messages": [{"role": "assistant", "tool_calls": [
        {"id": "c1", "function": {"name": "browser_type", "arguments": json.dumps({"text": PASSWORD})}},
    ]}]})
    assert PASSWORD not in json.dumps(stored)


def test_arguments_cut_off_mid_string_still_lose_the_text() -> None:
    cut = '{"ref": "@e5", "text": "Waalhav'
    assert mask_tool_arguments("browser_type", cut) == '{"ref": "@e5", "text": "' + "•" * 7


def test_masking_is_stable_so_a_stored_copy_masked_again_is_unchanged() -> None:
    once = mask_tool_arguments("browser_type", {"text": PASSWORD})
    assert mask_tool_arguments("browser_type", once) == once


def test_other_tools_and_a_secret_ref_are_left_alone() -> None:
    assert mask_tool_arguments("terminal", {"text": PASSWORD}) == {"text": PASSWORD}
    assert mask_tool_arguments("browser_type", {"secret_ref": "vault://browser_acme"}) == {
        "secret_ref": "vault://browser_acme",
    }


def test_typed_texts_finds_each_typing_calls_text_longest_first() -> None:
    calls = [
        {"id": "a", "function": {"name": "browser_type", "arguments": json.dumps({"text": "ana"})}},
        {"id": "b", "function": {"name": "browser_type", "arguments": json.dumps({"text": PASSWORD})}},
        {"id": "c", "function": {"name": "browser_type", "arguments": json.dumps({"secret_ref": "vault://browser_x"})}},
        {"id": "d", "function": {"name": "web_search", "arguments": json.dumps({"text": "nope"})}},
    ]
    assert typed_texts(calls) == [PASSWORD, "ana"]


def test_an_event_stored_before_masking_is_masked_when_read() -> None:
    """Every reader (event list, live stream, harness) gets events as Event
    models, so a legacy row's plaintext is masked once, there."""
    from types import SimpleNamespace
    from uuid import uuid4

    from surogates.session.models import Event

    legacy = SimpleNamespace(
        id=1, session_id=uuid4(), org_id=None, user_id=None, type="llm.response",
        trace_id=None, span_id=None, created_at=None,
        data={"message": {"tool_calls": [{"id": "c1", "function": {
            "name": "browser_type", "arguments": json.dumps({"ref": "@e5", "text": PASSWORD}),
        }}]}},
    )
    read = Event.model_validate(legacy)
    assert PASSWORD not in json.dumps(read.data)
    assert BULLETS in read.data["message"]["tool_calls"][0]["function"]["arguments"]
