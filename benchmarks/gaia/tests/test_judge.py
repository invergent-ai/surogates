import httpx
import pytest
import respx

from gaia_bench.judge import JudgeError, make_openai_complete

BASE = "https://llm.example/v1"
SCHEMA = {
    "name": "verdict",
    "strict": True,
    "schema": {"type": "object", "properties": {"root_cause": {"type": "string"}}},
}
MSGS = [{"role": "user", "content": "why did it fail?"}]


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def make():
    return make_openai_complete(BASE, "key", "claude-sonnet-5")


@respx.mock
async def test_parses_structured_json_reply():
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=reply('{"root_cause": "search_no_results"}')
    )
    out = await make()(MSGS, SCHEMA)
    assert out["root_cause"] == "search_no_results"


@respx.mock
async def test_sends_schema_as_response_format_and_model():
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=reply('{"root_cause": "x"}')
    )
    await make()(MSGS, SCHEMA)
    body = route.calls.last.request.read().decode()
    assert '"json_schema"' in body
    assert "claude-sonnet-5" in body
    assert route.calls.last.request.headers["authorization"] == "Bearer key"


@respx.mock
async def test_tolerates_fenced_json():
    # Not every OpenAI-compatible proxy honours response_format; several
    # wrap the object in a markdown fence instead.
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=reply('```json\n{"root_cause": "reasoning_error"}\n```')
    )
    out = await make()(MSGS, SCHEMA)
    assert out["root_cause"] == "reasoning_error"


@respx.mock
async def test_tolerates_prose_around_the_object():
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=reply('Here is my verdict: {"root_cause": "gave_up_early"} done')
    )
    out = await make()(MSGS, SCHEMA)
    assert out["root_cause"] == "gave_up_early"


@respx.mock
async def test_raises_on_unparseable_reply():
    respx.post(f"{BASE}/chat/completions").mock(return_value=reply("no json here"))
    with pytest.raises(JudgeError, match="could not parse"):
        await make()(MSGS, SCHEMA)


@respx.mock
async def test_raises_on_empty_content():
    # The opus-5 thinking-burn signature: all tokens spent reasoning,
    # nothing visible returned.
    respx.post(f"{BASE}/chat/completions").mock(return_value=reply(""))
    with pytest.raises(JudgeError):
        await make()(MSGS, SCHEMA)


@respx.mock
async def test_raises_on_http_error():
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(429, text="rate limited")
    )
    with pytest.raises(JudgeError, match="429"):
        await make()(MSGS, SCHEMA)
