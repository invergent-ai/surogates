from types import SimpleNamespace
from uuid import uuid4

from surogates.channels.platforms.slack import SlackPlatform


class _Client:
    def __init__(self, update_raises=False):
        self.update_raises = update_raises
        self.posted = []
        self.updated = []

    async def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        return {"ts": "900.0"}

    async def chat_update(self, **kwargs):
        if self.update_raises:
            raise RuntimeError("message_not_found")
        self.updated.append(kwargs)
        return {"ts": kwargs.get("ts")}


def _platform_with(client):
    platform = SlackPlatform()
    platform._get_client = lambda token: client
    return platform


def _prompt_item(update_ts=None):
    destination = {"channel_id": "C1", "thread_ts": "100.0"}
    if update_ts:
        destination["update_ts"] = update_ts
    return SimpleNamespace(
        session_id=uuid4(),
        destination=destination,
        payload={
            "input_prompt": True,
            "tool_call_id": "tc1",
            "questions": [{"prompt": "Which color?", "choices": [{"label": "blue"}]}],
            "context": "need a choice",
        },
    )


async def test_input_prompt_posts_block_kit_with_answer_button():
    client = _Client()

    result = await _platform_with(client).send(_prompt_item(), creds={"bot_token": "x"})

    assert result.success is True
    assert client.posted and client.updated == []
    kwargs = client.posted[0]
    assert kwargs["channel"] == "C1"
    assert kwargs["thread_ts"] == "100.0"
    assert kwargs["text"] == "I need your input to continue."
    assert kwargs["blocks"][-1]["elements"][0]["action_id"] == "surogates_input_answer"


async def test_input_prompt_edits_placeholder_when_update_ts_present():
    client = _Client()

    result = await _platform_with(client).send(
        _prompt_item(update_ts="200.0"),
        creds={"bot_token": "x"},
    )

    assert result.success is True
    assert client.updated and client.posted == []
    assert client.updated[0]["ts"] == "200.0"
    assert client.updated[0]["blocks"][-1]["elements"][0]["action_id"] == "surogates_input_answer"


async def test_input_prompt_update_failure_falls_back_to_fresh_post():
    client = _Client(update_raises=True)

    result = await _platform_with(client).send(
        _prompt_item(update_ts="200.0"),
        creds={"bot_token": "x"},
    )

    assert result.success is True
    assert client.posted and result.message_id == "900.0"
