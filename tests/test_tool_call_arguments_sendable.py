"""A tool call whose arguments are not a JSON object never reaches the engine again.

Census v4, session db60c33f (world model_railroad_lh1): the session's events are stored redacted, and the
redaction masked ``Bearer $TOKEN\\"`` inside a ``terminal`` call's arguments through the escaped quote, so the
stored arguments were no longer JSON.  The live turn went on (its history held what the model wrote); the wake that
read the sub-agent's report rebuilt the history from those events, and the engine refused every request of the
session: HTTP 400 "function tool_call arguments must be a JSON object string", 6 times.

Every request's history now carries each call's arguments as a JSON object string (``sendable_tool_arguments``):
text that is not one is sent as ``{"_raw": <the text>}``, the same on every replay.  The redaction keeps JSON
arguments JSON.  A call whose arguments are JSON but not an object (an array, a string, a number) is refused with
the model told why, in the session's loop and in an expert's.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from openai import APIStatusError

from surogates.harness import llm_call
from surogates.harness.llm_call import call_llm_with_retry
from surogates.harness.loop_context_replay import ContextReplayMixin
from surogates.harness.redact import redact_sensitive_data
from surogates.harness.resilience import find_invalid_tool_calls
from surogates.harness.sanitize import sendable_arguments, sendable_tool_arguments
from surogates.session.events import EventType

REFUSAL = "function tool_call arguments must be a JSON object string"

# Session db60c33f's llm.response 657 and tool.result 660, as stored (redacted; its reasoning left out).
STORED_RESPONSE = {
    "role": "assistant",
    "content": "The environment reveals an MCP proxy at `http://host.docker.internal:21301`. Let me check its "
               "OpenAPI schema — it may document batch IDs or additional endpoints.",
    "tool_calls": [{"id": "call_0c62258002439d4e", "type": "function", "function": {"name": "terminal", "arguments": (
        "{\"command\": \"TOKEN=*** -zo 'MCP_PROXY_TOKEN=*** /proc/self/environ | cut -d= -f2-); curl -s -m 8 -H "
        "\\\"Authorization: Bearer ***\" http://host.docker.internal:21301/openapi.json | head -c 4000; echo; echo "
        "\\\"=====/docs=====\\\"; curl -s -m 8 -H \\\"Authorization: Bearer ***\" "
        "http://host.docker.internal:21301/docs | head -c 2000\"}"
    )}}],
}
STORED_RESULT = {"name": "terminal", "content": "{\"output\": \"=====/docs=====\", \"exit_code\": 0, \"error\": null}",
                 "elapsed_ms": 16145, "tool_call_id": "call_0c62258002439d4e"}
# What the model wrote: the command the session's tool.call event 656 shows, before its redaction.
WRITTEN = json.dumps({"command": (
    "TOKEN=$(grep -zo 'MCP_PROXY_TOKEN=[^ ]*' /proc/self/environ | cut -d= -f2-); curl -s -m 8 -H "
    "\"Authorization: Bearer $TOKEN\" http://host.docker.internal:21301/openapi.json | head -c 4000; echo; "
    "echo \"=====/docs=====\"; curl -s -m 8 -H \"Authorization: Bearer $TOKEN\" "
    "http://host.docker.internal:21301/docs | head -c 2000"
)})


def _event(id_: int, type_: EventType, **data) -> SimpleNamespace:
    return SimpleNamespace(id=id_, type=type_.value, data=data)


def _rebuilt(response: dict) -> list[dict]:
    """The session's history as the wake rebuilds it from its events."""
    events = [
        _event(1, EventType.USER_MESSAGE, content="Please process the October restoration drop."),
        _event(2, EventType.LLM_REQUEST),
        _event(3, EventType.LLM_RESPONSE, message=response),
        _event(4, EventType.TOOL_RESULT, **STORED_RESULT),
        _event(5, EventType.USER_MESSAGE, content="[The sub-agent finished.]"),
    ]
    return ContextReplayMixin._rebuild_messages(SimpleNamespace(), events)


class Engine:
    """An engine that refuses a request whose history holds a call with arguments that are not a JSON object string."""

    def __init__(self) -> None:
        self.sent: list[list[dict]] = []

    async def __call__(self, *, create_kwargs, **_kwargs):
        messages = create_kwargs["messages"]
        self.sent.append(json.loads(json.dumps(messages)))
        for message in messages:
            for call in message.get("tool_calls") or []:
                arguments = call["function"].get("arguments")
                try:
                    good = isinstance(arguments, str) and isinstance(json.loads(arguments), dict)
                except ValueError:
                    good = False
                if not good:
                    request = httpx.Request("POST", "http://engine.test/v1/chat/completions")
                    body = {"error": {"message": REFUSAL, "type": "invalid_request_error", "code": 400}}
                    raise APIStatusError(REFUSAL, response=httpx.Response(400, request=request), body=body)
        return {"role": "assistant", "content": "Here is what I found."}, {"finish_reason": "stop"}


async def _ask(messages: list[dict], monkeypatch, *, engine: Engine | None = None, **more) -> tuple[dict, Engine]:
    engine = engine or Engine()
    monkeypatch.setattr(llm_call, "call_llm_streaming_inner", engine)
    monkeypatch.setattr(llm_call, "call_llm_non_streaming", AsyncMock(side_effect=AssertionError("no fallback")))

    async def no_wait(*_args):
        return None

    monkeypatch.setattr(llm_call, "interruptible_sleep", no_wait)
    reply, _usage = await call_llm_with_retry(
        session=SimpleNamespace(id=uuid4(), config={}), create_kwargs={"model": "m", "messages": messages},
        iteration=1, llm_client=MagicMock(), store=AsyncMock(), streaming_enabled=True,
        interrupt_check=lambda: False, activate_fallback=lambda: False, get_current_model=lambda: None,
        set_streaming_enabled=lambda _on: None, **more,
    )
    return reply, engine


def _sent_arguments(engine: Engine) -> list[str]:
    return [call["function"]["arguments"] for messages in engine.sent for m in messages for call in m.get("tool_calls") or []]


async def test_the_sessions_stored_history_is_sent_with_its_call_as_an_object_and_the_call_succeeds(monkeypatch):
    history = _rebuilt(STORED_RESPONSE)
    # Master sent the stored text as it is, and the engine refused the request (6 times in the session).
    reply, engine = await _ask(history, monkeypatch)
    assert reply["content"] == "Here is what I found."
    stored = STORED_RESPONSE["tool_calls"][0]["function"]["arguments"]
    assert _sent_arguments(engine) == [json.dumps({"_raw": stored}, ensure_ascii=False)]
    # The model reads what it wrote; the history and the stored event keep it.
    assert json.loads(_sent_arguments(engine)[0]) == {"_raw": stored}
    assert history[1]["tool_calls"][0]["function"]["arguments"] == stored


async def test_every_replay_sends_the_same_bytes(monkeypatch):
    engine = Engine()
    for _ in range(2):  # two wakes, each rebuilding the history from the events
        await _ask(_rebuilt(STORED_RESPONSE), monkeypatch, engine=engine)
    first, second = engine.sent
    assert json.dumps(first) == json.dumps(second)


async def test_a_compressed_retry_is_sent_sendable_too(monkeypatch):
    """A 413 has the history compressed and asked again: the compressed history goes through the same sanitising."""
    class Full(Engine):
        async def __call__(self, *, create_kwargs, **kwargs):
            if not self.sent:
                self.sent.append([])
                request = httpx.Request("POST", "http://engine.test/v1/chat/completions")
                raise APIStatusError("Error code: 413", response=httpx.Response(413, request=request), body=None)
            return await super().__call__(create_kwargs=create_kwargs, **kwargs)

    async def compress(_messages):
        return _rebuilt(STORED_RESPONSE)

    reply, engine = await _ask([{"role": "user", "content": "x"}], monkeypatch, engine=Full(), compress_context=compress)
    assert reply["content"] == "Here is what I found."
    assert json.loads(_sent_arguments(engine)[0]) == {"_raw": STORED_RESPONSE["tool_calls"][0]["function"]["arguments"]}


def test_redaction_keeps_the_written_arguments_json_and_masks_the_token():
    written = {"model": "w3g", "message": {**STORED_RESPONSE, "tool_calls": [
        {**STORED_RESPONSE["tool_calls"][0], "function": {"name": "terminal", "arguments": WRITTEN}},
    ]}}
    stored = redact_sensitive_data(written, phones=False)["message"]["tool_calls"][0]["function"]["arguments"]
    # Master stored what the session holds: the mask ran through the escaped quote ("Bearer ***\" http"),
    # and json.loads failed ("Expecting ',' delimiter").
    command = json.loads(stored)["command"]
    assert command.startswith("TOKEN=*** -zo 'MCP_PROXY_TOKEN=*** ")  # the assigned values are masked
    assert command.count('-H "Authorization: Bearer $TOKEN" http') == 2  # a shell variable, not a secret


async def test_a_history_rebuilt_from_newly_redacted_events_needs_no_stand_in(monkeypatch):
    written = {**STORED_RESPONSE, "tool_calls": [
        {**STORED_RESPONSE["tool_calls"][0], "function": {"name": "terminal", "arguments": WRITTEN}},
    ]}
    stored = redact_sensitive_data({"message": written}, phones=False)["message"]
    _reply, engine = await _ask(_rebuilt(stored), monkeypatch)
    [sent] = _sent_arguments(engine)
    assert "_raw" not in json.loads(sent)


def test_redaction_still_masks_what_it_masked_when_it_must_write_the_arguments_anew():
    secret = "sk-" + "a1b2c3d4e5" * 4
    text = json.dumps({"command": f"curl -H \"Authorization: Bearer {secret}\" http://x", "token": "hunter2-very-secret",
                       "env": f"OPENAI_API_KEY={secret}"})
    stored = redact_sensitive_data({"arguments": text, "name": "terminal"}, phones=False)["arguments"]
    parsed = json.loads(stored)
    assert secret not in stored and "hunter2-very-secret" not in stored
    assert parsed["command"].startswith("curl -H \"Authorization: Bearer ")


# Text masking broke JSON in each of these, so each reaches the value-by-value fallback.
_BREAKER = 'curl -H "Authorization: Bearer $TOKEN" x'
_PEM = "-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEA7x9Qz5u3T1tW0mFbYQ", "-----END RSA PRIVATE KEY-----"


def _stored(arguments: dict | str) -> str:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return redact_sensitive_data({"name": "terminal", "arguments": text}, phones=False)["arguments"]


@pytest.mark.parametrize("arguments", [
    {"lines": list(_PEM), "command": _BREAKER},
    {"a": [_PEM[0] + "\n" + _PEM[1]], "c": _PEM[2]},
], ids=["across strings", "across depths"])
def test_a_private_key_spread_over_strings_stays_masked_as_on_master(arguments):
    stored = _stored(arguments)
    assert _PEM[1] not in stored and "REDACTED PRIVATE KEY" in stored


def test_a_prefixed_key_under_a_secret_named_field_is_masked_whole():
    secret = "sk-" + "a1b2c3d4e5" * 4
    stored = json.loads(_stored({"command": _BREAKER, "api_key": secret}))
    assert stored["api_key"] == "***"


def test_the_fallback_never_stores_what_no_store_takes():
    stored = _stored('{"command": "curl -H \\"Authorization: Bearer $TOKEN\\" x", "a": "\\ud800"}')
    stored.encode("utf-8")  # a lone surrogate escape is kept as text
    deep = '{"command": "curl -H \\"Authorization: Bearer $TOKEN\\" x", "d": ' + "[" * 3000 + "]" * 3000 + "}"
    assert isinstance(_stored(deep), str)  # no RecursionError: the event is written


def test_arguments_that_redaction_leaves_json_are_stored_as_before():
    text = '{"query":"weather in Paris","limit":3}'
    assert redact_sensitive_data({"arguments": text, "name": "web_search"}, phones=False)["arguments"] == text
    secret_text = '{"token": "abcdefghijklmnopqrstuvwx"}'
    masked = redact_sensitive_data({"arguments": secret_text, "name": "x"}, phones=False)["arguments"]
    assert json.loads(masked)["token"] != "abcdefghijklmnopqrstuvwx"


@pytest.mark.parametrize("arguments, sent", [
    ('{"a": 1}', '{"a": 1}'),
    ('  {"a":1}  ', '  {"a":1}  '),
    ("", "{}"),
    ("   ", "{}"),
    (None, "{}"),
    ("null", "{}"),
    ({"a": 1}, '{"a": 1}'),
    ('{"command": "ls', '{"_raw": "{\\"command\\": \\"ls"}'),
    ("[1, 2]", '{"_raw": "[1, 2]"}'),
    ('"just text"', '{"_raw": "\\"just text\\""}'),
    ("42", '{"_raw": "42"}'),
    ('{"a": NaN}', '{"_raw": "{\\"a\\": NaN}"}'),
    ('{"a": "\\ud800"}', '{"_raw": "{\\"a\\": \\"\\\\ud800\\"}"}'),
    ([1, 2], '{"_raw": "[1, 2]"}'),
])
def test_what_is_sent_for_each_kind_of_arguments(arguments, sent):
    assert sendable_arguments(arguments) == sent
    assert isinstance(json.loads(sendable_arguments(arguments)), dict)


def test_a_history_that_needs_nothing_is_the_same_objects():
    history = [{"role": "user", "content": "hi"},
               {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "c", "content": "ok"}]
    assert sendable_tool_arguments(history) is history


async def test_a_live_turn_with_one_malformed_call_beside_good_ones_is_sent_too(monkeypatch):
    """The loop keeps a mixed batch in its history as the model wrote it: the next request must still be taken."""
    history = [
        {"role": "user", "content": "Look up two batches."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "get_batch", "arguments": '{"batch_id": "OCT-26"}'}},
            {"id": "b", "type": "function", "function": {"name": "get_batch", "arguments": '{"batch_id": "SEP-26"'}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "Error: Batch OCT-26 not found"},
        {"role": "tool", "tool_call_id": "b", "content": '{"error": "Malformed JSON in tool arguments"}'},
    ]
    _reply, engine = await _ask(history, monkeypatch)
    assert _sent_arguments(engine) == ['{"batch_id": "OCT-26"}', json.dumps({"_raw": '{"batch_id": "SEP-26"'})]


@pytest.mark.parametrize("arguments, kind", [("[1, 2]", "array"), ('"x"', "string"), ("7", "number"), ("true", "boolean")])
def test_json_that_is_not_an_object_is_refused_with_the_model_told_why(arguments, kind):
    call = {"id": "c", "type": "function", "function": {"name": "get_batch", "arguments": arguments}}
    [(_, error)] = find_invalid_tool_calls([call], {"get_batch"}, {"get_batch"})
    assert f"must be a JSON object, not a JSON {kind}" in json.loads(error)["error"]


@pytest.mark.parametrize("arguments", ['{"batch_id": "x"}', "null", ""])
def test_an_object_or_no_arguments_is_not_refused(arguments):
    call = {"id": "c", "type": "function", "function": {"name": "get_batch", "arguments": arguments}}
    assert find_invalid_tool_calls([call], {"get_batch"}, {"get_batch"}) == []


@pytest.mark.parametrize("arguments, kind", [("[1, 2]", "array"), ('"a.txt"', "string")])
async def test_the_streamed_call_with_json_that_is_not_an_object_never_runs_and_says_why(arguments, kind):
    from surogates.harness.tool_exec import execute_single_tool
    from surogates.tools.registry import ToolRegistry, ToolSchema

    registry = ToolRegistry()
    handler = AsyncMock(return_value='{"ok": true}')
    registry.register("read_file", ToolSchema(name="read_file", description="read", parameters={
        "type": "object", "properties": {"path": {"type": "string"}}}), handler=handler)
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=[1, 2])
    result = await execute_single_tool(
        {"id": "c", "function": {"name": "read_file", "arguments": arguments}},
        session=SimpleNamespace(id=uuid4(), config={"workspace_path": ""}, agent_id="a"),
        lease=SimpleNamespace(lease_token=uuid4()), store=store, tools=registry, tenant=MagicMock(asset_root="/tmp/t"),
    )
    handler.assert_not_called()
    error = json.loads(result["content"])["error"]
    assert error.startswith("Invalid JSON arguments") and f"must be a JSON object, not a JSON {kind}" in error


@pytest.mark.parametrize("arguments", ['{"path": "a.txt"', "[1, 2]"])
async def test_an_experts_malformed_call_is_refused_and_its_next_request_is_sendable(monkeypatch, arguments):
    from surogates.tools.builtin import expert_loop as el
    from tests.test_case_tool_gate import _registry
    from tests.test_expert_tool_scope import _expert, _ExpertModel

    model = _ExpertModel(["read_file"], arguments=arguments)
    monkeypatch.setattr("openai.AsyncOpenAI", model.client)
    router = SimpleNamespace(execute=AsyncMock(return_value="contents"))
    result, _ = await el.run_expert_loop(
        expert=_expert(["read_file"]), task="t", context=None, tool_router=router, tool_registry=_registry(),
        tenant=SimpleNamespace(org_config={}), session_id=uuid4(),
    )
    assert result == "Drafted."
    router.execute.assert_not_awaited()
    assert "must be a JSON object" in json.loads(model.results()["c0"])["error"]
    [call] = [c for m in model.requests[-1]["messages"] for c in m.get("tool_calls") or []]
    assert json.loads(call["function"]["arguments"]) == {"_raw": arguments}
