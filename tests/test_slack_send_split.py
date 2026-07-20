"""SlackPlatform.send splits over-limit messages into sequential posts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from surogates.channels.platforms.slack import SlackPlatform


def _item(content: str, *, update_ts: str | None = None, thread_ts: str | None = None):
    destination = {"channel_id": "C1"}
    if update_ts:
        destination["update_ts"] = update_ts
    if thread_ts:
        destination["thread_ts"] = thread_ts
    return SimpleNamespace(
        id=1, session_id="s1", destination=destination, payload={"content": content},
    )


@pytest.fixture
def platform(monkeypatch):
    p = SlackPlatform()
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(side_effect=[{"ts": f"1.{i}"} for i in range(10)]),
        chat_update=AsyncMock(return_value={"ts": "0.9"}),
    )
    monkeypatch.setattr(p, "_get_client", lambda token: client)
    return p, client


async def test_short_message_single_post(platform):
    p, client = platform
    result = await p.send(_item("hello"), creds={"bot_token": "xoxb-1"})
    assert result.success and result.message_id == "1.0"
    assert client.chat_postMessage.await_count == 1


async def test_long_message_splits_into_multiple_posts(platform):
    p, client = platform
    text = ("paragraph one. " * 2000) + "\n\n" + ("paragraph two. " * 2000)
    result = await p.send(_item(text), creds={"bot_token": "xoxb-1"})
    assert result.success
    assert client.chat_postMessage.await_count >= 2
    for call in client.chat_postMessage.await_args_list:
        assert len(call.kwargs["text"]) <= p._MAX_MESSAGE_CHARS
    # last posted ts wins
    assert result.message_id == f"1.{client.chat_postMessage.await_count - 1}"


async def test_update_ts_edits_first_chunk_then_posts_rest(platform):
    p, client = platform
    text = "a" * (p._MAX_MESSAGE_CHARS + 100)
    result = await p.send(
        _item(text, update_ts="0.5"), creds={"bot_token": "xoxb-1"},
    )
    assert result.success
    client.chat_update.assert_awaited_once()
    assert len(client.chat_update.await_args.kwargs["text"]) <= p._MAX_MESSAGE_CHARS
    assert client.chat_postMessage.await_count == 1


async def test_thread_ts_propagates_to_every_chunk(platform):
    p, client = platform
    text = ("x" * p._MAX_MESSAGE_CHARS) + "\n\n" + ("y" * 100)
    await p.send(_item(text, thread_ts="9.9"), creds={"bot_token": "xoxb-1"})
    for call in client.chat_postMessage.await_args_list:
        assert call.kwargs["thread_ts"] == "9.9"


async def test_mid_split_failure_reports_delivered_prefix(platform):
    p, client = platform
    client.chat_postMessage.side_effect = [{"ts": "1.0"}, RuntimeError("boom")]
    text = ("x" * p._MAX_MESSAGE_CHARS) + "\n\n" + ("y" * 100)
    result = await p.send(_item(text), creds={"bot_token": "xoxb-1"})
    assert result.success and result.message_id == "1.0"
