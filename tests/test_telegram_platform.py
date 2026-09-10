"""Telegram platform webhook registration, delivery and callback handling."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx

from surogates.channels.platforms.telegram import (
    TelegramPlatform,
)


BOT_USERNAME = "@my_test_bot"
WEBHOOK_SECRET = "super_secret_token_XYZ"


def _private_message(
    *,
    chat_id: int = 111,
    from_id: int = 999,
    username: str | None = "alice",
    first_name: str | None = "Alice",
    text: str = "Hello bot",
    date: int = 1700000000,
    message_thread_id: int | None = None,
) -> dict:
    """Build a minimal Telegram 'message' update for a private chat."""
    message: dict = {
        "message_id": 1,
        "from": {
            "id": from_id,
            "is_bot": False,
            "first_name": first_name or "User",
        },
        "chat": {
            "id": chat_id,
            "type": "private",
        },
        "date": date,
        "text": text,
    }
    if username:
        message["from"]["username"] = username
    if message_thread_id is not None:
        message["message_thread_id"] = message_thread_id
    return {"update_id": 123, "message": message}


def _callback_query_update() -> dict:
    """Build a minimal callback_query update (no 'message' key at top level)."""
    return {
        "update_id": 125,
        "callback_query": {
            "id": "abc123",
            "from": {"id": 999, "is_bot": False, "first_name": "Alice"},
            "data": "button_pressed",
            "chat_instance": "xyz",
        },
    }


def _non_message_update() -> dict:
    """An update with no 'message' key (e.g. channel_post, edited_message)."""
    return {
        "update_id": 126,
        "channel_post": {
            "message_id": 3,
            "chat": {"id": -100999, "type": "channel", "title": "News"},
            "date": 1700000002,
            "text": "Channel post",
        },
    }


BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
BOT_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


@pytest.mark.asyncio
async def test_register_webhook_calls_set_webhook():
    """register_webhook POSTs setWebhook with url, secret_token, allowed_updates."""
    import json as _json

    platform = TelegramPlatform()
    webhook_url = "https://example.com/telegram/@my_bot"
    creds = {"bot_token": BOT_TOKEN, "webhook_secret": "mysecret"}
    captured: dict = {}

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.post(f"{BOT_API_BASE}/setWebhook").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        await platform.descriptor.register_webhook("@my_bot", webhook_url, creds)
        assert len(mock_router.calls) == 1
        captured["body"] = _json.loads(mock_router.calls[0].request.content)

    body = captured["body"]
    assert body["url"] == webhook_url
    assert body["secret_token"] == "mysecret"
    assert "message" in body["allowed_updates"]
    assert "callback_query" in body["allowed_updates"]


@pytest.mark.asyncio
async def test_register_webhook_raises_on_non_ok():
    """register_webhook raises (or at minimum logs) when Telegram returns ok=false."""
    platform = TelegramPlatform()
    creds = {"bot_token": BOT_TOKEN, "webhook_secret": "s"}

    with respx.mock() as mock_router:
        mock_router.post(f"{BOT_API_BASE}/setWebhook").mock(
            return_value=httpx.Response(200, json={"ok": False, "description": "Unauthorized"})
        )
        with pytest.raises(Exception):
            await platform.descriptor.register_webhook("@bot", "https://x.com/bot", creds)


@pytest.mark.asyncio
async def test_parse_uses_identifier_not_get_me():
    """platform.parse uses the identifier kwarg for bot_username — getMe is NOT called."""
    platform = TelegramPlatform()
    update = _private_message(text="hello")
    creds = {"bot_token": BOT_TOKEN, "webhook_secret": "s"}

    with respx.mock(assert_all_called=False) as mock_router:
        # Register a getMe route; if it is called the test fails via the
        # assertion below (call count must be zero).
        mock_router.get(f"{BOT_API_BASE}/getMe").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"id": 123, "username": "my_bot"}}
            )
        )
        result = await platform.parse(update, creds=creds, identifier="@my_bot")
        get_me_calls = len(mock_router.calls)

    assert result is not None
    assert result.text == "hello"
    assert get_me_calls == 0, f"getMe was called {get_me_calls} time(s); expected 0"


@pytest.mark.asyncio
async def test_parse_identifier_used_for_mention_detection():
    """Mention detection works when bot username comes from the identifier kwarg."""
    platform = TelegramPlatform()
    update = _private_message(text="@my_bot help me please")
    creds = {"bot_token": BOT_TOKEN}

    with respx.mock(assert_all_called=False) as mock_router:
        mock_router.get(f"{BOT_API_BASE}/getMe").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"id": 123, "username": "my_bot"}}
            )
        )
        result = await platform.parse(update, creds=creds, identifier="@my_bot")
        get_me_calls = len(mock_router.calls)

    assert result is not None
    assert result.is_mention is True
    assert get_me_calls == 0, f"getMe was called {get_me_calls} time(s); expected 0"


@pytest.mark.asyncio
async def test_send_posts_send_message_to_correct_chat():
    """send POSTs sendMessage with chat_id and text."""
    platform = TelegramPlatform()
    item = SimpleNamespace(
        destination={"chat_id": 111},
        payload={"content": "Hello there"},
    )
    creds = {"bot_token": BOT_TOKEN}

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.post(f"{BOT_API_BASE}/sendMessage").mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 99, "date": 1700000000}},
            )
        )
        result = await platform.send(item, creds=creds)

    assert result.success is True
    assert result.message_id == "99"
    assert result.error is None


@pytest.mark.asyncio
async def test_send_includes_message_thread_id():
    """send includes message_thread_id in the sendMessage call when present (Telegram forum topics)."""
    import json as _json

    platform = TelegramPlatform()
    item = SimpleNamespace(
        destination={"chat_id": 111, "message_thread_id": 42},
        payload={"content": "A thread reply"},
    )
    creds = {"bot_token": BOT_TOKEN}
    captured: dict = {}

    with respx.mock() as mock_router:
        mock_router.post(f"{BOT_API_BASE}/sendMessage").mock(
            return_value=httpx.Response(
                200,
                json={"ok": True, "result": {"message_id": 100, "date": 1700000001}},
            )
        )
        result = await platform.send(item, creds=creds)
        captured["body"] = _json.loads(mock_router.calls[0].request.content)

    assert result.success is True
    assert captured["body"].get("message_thread_id") == 42


@pytest.mark.asyncio
async def test_send_ok_false_returns_send_result_failure():
    """When Telegram returns ok=false, send returns SendResult(success=False, error=...)."""
    platform = TelegramPlatform()
    item = SimpleNamespace(
        destination={"chat_id": 999},
        payload={"content": "oops"},
    )
    creds = {"bot_token": BOT_TOKEN}

    with respx.mock() as mock_router:
        mock_router.post(f"{BOT_API_BASE}/sendMessage").mock(
            return_value=httpx.Response(
                200,
                json={"ok": False, "description": "Chat not found"},
            )
        )
        result = await platform.send(item, creds=creds)

    assert result.success is False
    assert result.error is not None


@pytest.mark.asyncio
async def test_send_http_error_returns_send_result_failure():
    """An HTTP-level error (e.g. 5xx) → SendResult(success=False); no exception raised."""
    platform = TelegramPlatform()
    item = SimpleNamespace(
        destination={"chat_id": 777},
        payload={"content": "hi"},
    )
    creds = {"bot_token": BOT_TOKEN}

    with respx.mock() as mock_router:
        mock_router.post(f"{BOT_API_BASE}/sendMessage").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        result = await platform.send(item, creds=creds)

    assert result.success is False
    assert result.error is not None


@pytest.mark.asyncio
async def test_handle_non_message_update_callback_query_returns_true():
    """callback_query body → answerCallbackQuery is called; returns True."""
    platform = TelegramPlatform()
    creds = {"bot_token": BOT_TOKEN}
    body = _callback_query_update()

    with respx.mock() as mock_router:
        mock_router.post(f"{BOT_API_BASE}/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        result = await platform.handle_non_message_update(
            body, routing=None, creds=creds, deps=None
        )
        ack_call_count = len(mock_router.calls)

    assert result is True
    assert ack_call_count == 1


@pytest.mark.asyncio
async def test_handle_non_message_update_callback_query_acks_correct_id():
    """answerCallbackQuery is called with the callback_query id."""
    import json as _json

    platform = TelegramPlatform()
    creds = {"bot_token": BOT_TOKEN}
    body = _callback_query_update()  # has callback_query.id == "abc123"
    captured: dict = {}

    with respx.mock() as mock_router:
        mock_router.post(f"{BOT_API_BASE}/answerCallbackQuery").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )
        await platform.handle_non_message_update(
            body, routing=None, creds=creds, deps=None
        )
        captured["body"] = _json.loads(mock_router.calls[0].request.content)

    assert captured["body"].get("callback_query_id") == "abc123"


@pytest.mark.asyncio
async def test_handle_non_message_update_non_callback_returns_false():
    """A non-callback_query update (e.g. channel_post) → returns False (fall through)."""
    platform = TelegramPlatform()
    creds = {"bot_token": BOT_TOKEN}
    body = _non_message_update()

    result = await platform.handle_non_message_update(
        body, routing=None, creds=creds, deps=None
    )

    assert result is False


@pytest.mark.asyncio
async def test_handle_non_message_update_message_update_returns_false():
    """A regular message update → returns False (let pipeline handle it)."""
    platform = TelegramPlatform()
    creds = {"bot_token": BOT_TOKEN}
    body = _private_message(text="hello")

    result = await platform.handle_non_message_update(
        body, routing=None, creds=creds, deps=None
    )

    assert result is False


class TestTelegramSendPrivate:
    """send_private DMs the sender (the user id is their private chat)."""

    @pytest.mark.asyncio
    async def test_dms_the_sender(self):
        posted = {}

        class _FakeHttp:
            async def post(self, url, json):
                posted.update(url=url, json=json)

                class _R:
                    def json(self_inner):
                        return {"ok": True, "result": {"message_id": 1}}

                return _R()

        plat = TelegramPlatform()
        plat._http = _FakeHttp()
        ok = await plat.send_private(
            {"bot_token": "T"}, sender_id="555", chat_id="-100", is_dm=False, text="link me",
        )
        assert ok is True
        assert posted["json"]["chat_id"] == "555"
        assert "link me" in posted["json"]["text"]
        assert "sendMessage" in posted["url"]

    @pytest.mark.asyncio
    async def test_returns_false_on_api_error(self):
        class _FakeHttp:
            async def post(self, url, json):
                class _R:
                    def json(self_inner):
                        return {"ok": False, "description": "blocked"}

                return _R()

        plat = TelegramPlatform()
        plat._http = _FakeHttp()
        ok = await plat.send_private(
            {"bot_token": "T"}, sender_id="555", chat_id="-100", is_dm=False, text="x",
        )
        assert ok is False
