"""Tests for surogates.channels.platforms.telegram — parse + verify + identifier_of.

TDD: tests written for the new webhook-based Telegram platform module.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from surogates.channels.platforms.telegram import (
    TelegramPlatform,
    identifier_of,
    parse,
    verify,
)
from surogates.channels.base import SendResult
from surogates.channels.inbound import InboundMessage


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------

BOT_USERNAME = "@my_test_bot"
WEBHOOK_SECRET = "super_secret_token_XYZ"


def _make_request(
    *,
    username: str = BOT_USERNAME,
    secret_header: str | None = WEBHOOK_SECRET,
) -> SimpleNamespace:
    """Build a fake Starlette-like request with path_params and headers."""
    headers: dict[str, str] = {}
    if secret_header is not None:
        headers["X-Telegram-Bot-Api-Secret-Token"] = secret_header
    return SimpleNamespace(
        path_params={"username": username},
        headers=headers,
    )


def _creds(webhook_secret: str | None = WEBHOOK_SECRET) -> dict:
    creds: dict = {}
    if webhook_secret is not None:
        creds["webhook_secret"] = webhook_secret
    return creds


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


def _group_message(
    *,
    chat_id: int = -1001234567890,
    from_id: int = 999,
    username: str | None = "alice",
    text: str = "Hello everyone",
    date: int = 1700000001,
    chat_type: str = "group",
    is_forum: bool = False,
    message_thread_id: int | None = None,
) -> dict:
    """Build a minimal Telegram 'message' update for a group/supergroup chat."""
    message: dict = {
        "message_id": 2,
        "from": {
            "id": from_id,
            "is_bot": False,
            "first_name": "Alice",
        },
        "chat": {
            "id": chat_id,
            "title": "Test Group",
            "type": chat_type,
        },
        "date": date,
        "text": text,
    }
    if username:
        message["from"]["username"] = username
    if is_forum:
        message["chat"]["is_forum"] = True
    if message_thread_id is not None:
        message["message_thread_id"] = message_thread_id
    return {"update_id": 124, "message": message}


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


# ---------------------------------------------------------------------------
# identifier_of
# ---------------------------------------------------------------------------


class TestIdentifierOf:
    def test_reads_username_from_path_params(self):
        request = SimpleNamespace(path_params={"username": BOT_USERNAME})
        assert identifier_of(request, {}) == BOT_USERNAME

    def test_ignores_body_entirely(self):
        request = SimpleNamespace(path_params={"username": BOT_USERNAME})
        assert identifier_of(request, None) == BOT_USERNAME
        assert identifier_of(request, {"some": "data"}) == BOT_USERNAME


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


class TestVerify:
    def test_accepts_matching_secret(self):
        request = _make_request(secret_header=WEBHOOK_SECRET)
        assert verify(request, b"", creds=_creds(WEBHOOK_SECRET)) is True

    def test_rejects_mismatched_secret(self):
        request = _make_request(secret_header="wrong_secret")
        assert verify(request, b"", creds=_creds(WEBHOOK_SECRET)) is False

    def test_rejects_missing_header(self):
        request = _make_request(secret_header=None)
        assert verify(request, b"", creds=_creds(WEBHOOK_SECRET)) is False

    def test_rejects_empty_stored_secret(self):
        """An empty stored secret means webhook auth is not configured — reject."""
        request = _make_request(secret_header=WEBHOOK_SECRET)
        assert verify(request, b"", creds=_creds("")) is False

    def test_rejects_none_stored_secret(self):
        """creds with no webhook_secret key → reject (don't crash)."""
        request = _make_request(secret_header=WEBHOOK_SECRET)
        assert verify(request, b"", creds={}) is False

    def test_rejects_none_webhook_secret_value(self):
        """creds["webhook_secret"] = None → reject without crashing."""
        request = _make_request(secret_header=WEBHOOK_SECRET)
        assert verify(request, b"", creds={"webhook_secret": None}) is False

    def test_case_insensitive_header_lookup(self):
        """Lowercase header key should also be found."""
        request = SimpleNamespace(
            path_params={"username": BOT_USERNAME},
            headers={"x-telegram-bot-api-secret-token": WEBHOOK_SECRET},
        )
        assert verify(request, b"", creds=_creds(WEBHOOK_SECRET)) is True


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


class TestParse:
    # ------------------------------------------------------------------
    # Private message
    # ------------------------------------------------------------------

    def test_private_message_is_dm(self):
        update = _private_message(text="Hi bot")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert isinstance(result, InboundMessage)
        assert result.is_dm is True
        assert result.kind == "text"
        assert result.text == "Hi bot"

    def test_private_message_identifier_is_chat_id(self):
        update = _private_message(chat_id=111, text="Hello")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.identifier == "111"

    def test_private_message_platform_user_id(self):
        update = _private_message(from_id=999, text="Hello")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.platform_user_id == "999"

    def test_private_message_user_name_from_username(self):
        update = _private_message(username="alice", text="Hi")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.user_name == "alice"

    def test_private_message_user_name_falls_back_to_first_name(self):
        update = _private_message(username=None, first_name="Bob", text="Hi")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.user_name == "Bob"

    def test_private_message_user_name_falls_back_to_id(self):
        """No username or first_name → use string user id."""
        update = _private_message(username=None, first_name=None, from_id=777, text="Hi")
        # Patch first_name out of the from dict
        update["message"]["from"].pop("first_name", None)
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.user_name == "777"

    def test_private_message_ts_is_update_id_string(self):
        """ts must be the update_id (globally unique), not the message date.

        FIX 1: Using update_id ensures two messages in the same second get
        different dedup keys.
        """
        update = _private_message(date=1700000000, text="Hi")
        # _private_message sets update_id=123
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.ts == "123"

    def test_private_message_no_thread_key(self):
        update = _private_message(text="Hi")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.thread_key is None

    # ------------------------------------------------------------------
    # Group message
    # ------------------------------------------------------------------

    def test_group_message_not_dm(self):
        update = _group_message(text="Hello group")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.is_dm is False

    def test_supergroup_message_not_dm(self):
        update = _group_message(chat_type="supergroup", text="Hello supergroup")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.is_dm is False

    def test_group_message_no_thread_key(self):
        update = _group_message(text="Hello group")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.thread_key is None

    # ------------------------------------------------------------------
    # Forum thread (supergroup + is_forum + message_thread_id)
    # ------------------------------------------------------------------

    def test_forum_message_has_thread_key(self):
        update = _group_message(
            chat_type="supergroup",
            is_forum=True,
            message_thread_id=42,
            text="Forum thread reply",
        )
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.thread_key == "42"
        assert result.is_dm is False

    def test_non_forum_supergroup_no_thread_key(self):
        update = _group_message(chat_type="supergroup", is_forum=False, text="Regular")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.thread_key is None

    # ------------------------------------------------------------------
    # Mention detection
    # ------------------------------------------------------------------

    def test_is_mention_when_bot_username_in_text(self):
        update = _private_message(text="Hey @my_test_bot, help me")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.is_mention is True

    def test_is_mention_without_at_prefix_in_bot_username_arg(self):
        """bot_username may be passed without leading @."""
        update = _private_message(text="Hey @my_test_bot, help me")
        result = parse(update, bot_username="my_test_bot")
        assert result is not None
        assert result.is_mention is True

    def test_is_mention_case_insensitive(self):
        update = _group_message(text="@MY_TEST_BOT please help")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.is_mention is True

    def test_not_mention_when_bot_not_in_text(self):
        update = _group_message(text="Hello everyone")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.is_mention is False

    # ------------------------------------------------------------------
    # Non-message / unsupported update types → None
    # ------------------------------------------------------------------

    def test_callback_query_returns_none(self):
        update = _callback_query_update()
        assert parse(update, bot_username=BOT_USERNAME) is None

    def test_non_message_update_returns_none(self):
        update = _non_message_update()
        assert parse(update, bot_username=BOT_USERNAME) is None

    def test_empty_body_returns_none(self):
        assert parse({}, bot_username=BOT_USERNAME) is None

    # ------------------------------------------------------------------
    # Message with no text → None
    # ------------------------------------------------------------------

    def test_message_without_text_returns_none(self):
        """A photo-only message with no text (or caption) returns None."""
        update = {
            "update_id": 200,
            "message": {
                "message_id": 10,
                "from": {"id": 1, "is_bot": False, "first_name": "Alice"},
                "chat": {"id": 111, "type": "private"},
                "date": 1700000005,
                "photo": [{"file_id": "abc", "width": 100, "height": 100, "file_size": 1000}],
                # No "text" key
            },
        }
        assert parse(update, bot_username=BOT_USERNAME) is None

    def test_message_with_empty_text_returns_none(self):
        update = _private_message(text="")
        # Overwrite text with empty string
        update["message"]["text"] = ""
        assert parse(update, bot_username=BOT_USERNAME) is None

    # ------------------------------------------------------------------
    # Source dict
    # ------------------------------------------------------------------

    def test_source_contains_platform_telegram(self):
        update = _private_message(text="Hi")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.source.get("platform") == "telegram"

    def test_source_contains_chat_type(self):
        update = _group_message(text="Hi", chat_type="supergroup")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.source.get("chat_type") == "supergroup"

    # ------------------------------------------------------------------
    # Media lists are empty (text-only messages)
    # ------------------------------------------------------------------

    def test_media_urls_empty_for_text_message(self):
        update = _private_message(text="Just text")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.media_urls == []
        assert result.media_types == []

    # ------------------------------------------------------------------
    # Robustness: missing keys
    # ------------------------------------------------------------------

    def test_does_not_crash_on_missing_from_field(self):
        """An update with a message but no 'from' key → None (anonymous post)."""
        update = {
            "update_id": 300,
            "message": {
                "message_id": 20,
                "chat": {"id": 111, "type": "private"},
                "date": 1700000006,
                "text": "Anonymous",
                # No "from" key
            },
        }
        # Should return None gracefully, not raise
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is None

    def test_does_not_crash_on_unexpected_body(self):
        """Completely unexpected body shape → None without exception."""
        assert parse({"update_id": 999, "unknown_type": {}}, bot_username=BOT_USERNAME) is None


# ---------------------------------------------------------------------------
# TelegramPlatform (strategy object)
# ---------------------------------------------------------------------------


class TestTelegramPlatform:
    def test_kind_is_telegram(self):
        platform = TelegramPlatform()
        assert platform.kind == "telegram"

    def test_topology_is_webhook(self):
        platform = TelegramPlatform()
        assert platform.topology == "webhook"

    def test_route_path_template(self):
        platform = TelegramPlatform()
        assert "{username}" in platform.route_path()

    def test_route_path_with_identifier(self):
        platform = TelegramPlatform()
        assert "@mybot" in platform.route_path("@mybot")

    def test_identifier_of_delegates(self):
        platform = TelegramPlatform()
        request = SimpleNamespace(path_params={"username": "@mybot"})
        assert platform.identifier_of(request, {}) == "@mybot"

    def test_verify_delegates(self):
        platform = TelegramPlatform()
        request = _make_request(secret_header=WEBHOOK_SECRET)
        assert platform.verify(request, b"", creds=_creds(WEBHOOK_SECRET)) is True

    @pytest.mark.asyncio
    async def test_parse_async_delegates(self):
        platform = TelegramPlatform()
        update = _private_message(text="Hello async")
        result = await platform.parse(update, creds={"bot_username": BOT_USERNAME})
        assert result is not None
        assert result.text == "Hello async"


# ---------------------------------------------------------------------------
# TelegramPlatform — descriptor shape
# ---------------------------------------------------------------------------


class TestTelegramDescriptor:
    def test_descriptor_is_present(self):
        platform = TelegramPlatform()
        assert hasattr(platform, "descriptor")

    def test_vault_refs_returns_bot_token_and_webhook_secret(self):
        platform = TelegramPlatform()
        refs = platform.descriptor.vault_refs("@my_bot")
        assert "bot_token" in refs
        assert "webhook_secret" in refs

    def test_vault_refs_values_are_strings(self):
        platform = TelegramPlatform()
        refs = platform.descriptor.vault_refs("@my_bot")
        for v in refs.values():
            assert isinstance(v, str)

    def test_config_keys_contains_required_keys(self):
        """config_keys must use 'free_response_channels' (not 'free_response_chats').

        FIX 4: Renamed to match the key read by the pipeline and written by
        the ops provisioner.
        """
        platform = TelegramPlatform()
        keys = platform.descriptor.config_keys
        for expected in (
            "require_mention",
            "free_response_channels",
            "mention_patterns",
            "reply_to_mode",
            "reactions_enabled",
            "per_user_groups",
        ):
            assert expected in keys, f"config_keys missing: {expected!r}"

    def test_webhook_registration_is_api(self):
        platform = TelegramPlatform()
        assert platform.descriptor.webhook_registration == "api"

    def test_register_webhook_is_callable(self):
        platform = TelegramPlatform()
        assert callable(platform.descriptor.register_webhook)


# ---------------------------------------------------------------------------
# TelegramPlatform — registration on import
# ---------------------------------------------------------------------------


class TestTelegramRegistration:
    def test_registered_in_global_registry(self):
        from surogates.channels.registry import registry
        platform = registry.get("telegram")
        assert platform is not None
        assert platform.kind == "telegram"

    def test_double_registration_is_a_noop(self):
        """Importing the module twice (or calling _register() twice) must not raise."""
        from surogates.channels.platforms import telegram as _tg_mod
        from surogates.channels.registry import registry
        # _register is idempotent: calling it again does nothing
        _tg_mod._register()
        assert registry.get("telegram") is not None


# ---------------------------------------------------------------------------
# TelegramPlatform — register_webhook calls setWebhook
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# TelegramPlatform — parse uses cached getMe for bot_username
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_resolves_bot_username_via_get_me():
    """platform.parse calls getMe to resolve bot_username when creds has bot_token."""
    platform = TelegramPlatform()
    update = _private_message(text="hello")
    creds = {"bot_token": BOT_TOKEN, "webhook_secret": "s"}

    with respx.mock() as mock_router:
        mock_router.get(f"{BOT_API_BASE}/getMe").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"id": 123, "username": "my_bot"}}
            )
        )
        result = await platform.parse(update, creds=creds)

    assert result is not None
    assert result.text == "hello"


@pytest.mark.asyncio
async def test_parse_caches_bot_username_across_calls():
    """getMe is called only once for the same bot_token, even across multiple parse calls."""
    platform = TelegramPlatform()
    update = _private_message(text="hello")
    creds = {"bot_token": BOT_TOKEN, "webhook_secret": "s"}

    with respx.mock() as mock_router:
        mock_router.get(f"{BOT_API_BASE}/getMe").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"id": 123, "username": "my_bot"}}
            )
        )
        await platform.parse(update, creds=creds)
        await platform.parse(update, creds=creds)
        await platform.parse(update, creds=creds)

        call_count = len(mock_router.calls)

    assert call_count == 1, f"getMe called {call_count} times; expected exactly 1"


@pytest.mark.asyncio
async def test_parse_without_creds_still_works():
    """parse(body, creds=None) gracefully falls back to empty bot_username."""
    platform = TelegramPlatform()
    update = _private_message(text="hello")
    result = await platform.parse(update, creds=None)
    assert result is not None
    assert result.text == "hello"


@pytest.mark.asyncio
async def test_parse_mention_detected_via_get_me():
    """Mention detection works when bot username comes from getMe."""
    platform = TelegramPlatform()
    update = _private_message(text="@my_bot help me please")
    creds = {"bot_token": BOT_TOKEN}

    with respx.mock() as mock_router:
        mock_router.get(f"{BOT_API_BASE}/getMe").mock(
            return_value=httpx.Response(
                200, json={"ok": True, "result": {"id": 123, "username": "my_bot"}}
            )
        )
        result = await platform.parse(update, creds=creds)

    assert result is not None
    assert result.is_mention is True


# ---------------------------------------------------------------------------
# TelegramPlatform — send via sendMessage
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# TelegramPlatform — handle_non_message_update
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# FIX 1: Telegram dedup key is update_id not date
# ---------------------------------------------------------------------------


class TestTelegramDedupKey:
    """FIX 1: ts must be update_id so two messages with same date get different ts."""

    def test_two_messages_same_date_different_update_id_produce_different_ts(self):
        """Two updates with same date but different update_id → different ts values."""
        update_a = {
            "update_id": 7001,
            "message": {
                "message_id": 1,
                "from": {"id": 100, "is_bot": False, "first_name": "Alice", "username": "alice"},
                "chat": {"id": 111, "type": "private"},
                "date": 1700000000,  # SAME date
                "text": "Message A",
            },
        }
        update_b = {
            "update_id": 7002,  # DIFFERENT update_id
            "message": {
                "message_id": 2,
                "from": {"id": 100, "is_bot": False, "first_name": "Alice", "username": "alice"},
                "chat": {"id": 111, "type": "private"},
                "date": 1700000000,  # SAME date
                "text": "Message B",
            },
        }
        result_a = parse(update_a, bot_username=BOT_USERNAME)
        result_b = parse(update_b, bot_username=BOT_USERNAME)
        assert result_a is not None
        assert result_b is not None
        assert result_a.ts != result_b.ts, (
            f"Two updates with same date but different update_id must produce "
            f"different ts; got a.ts={result_a.ts!r} b.ts={result_b.ts!r}"
        )

    def test_same_update_id_produces_same_ts(self):
        """Repeated delivery of the same update (same update_id) → same ts (dedup catches it)."""
        update = _private_message(text="retry me", date=1700000000)
        # update_id=123 from _private_message fixture
        result_a = parse(update, bot_username=BOT_USERNAME)
        result_b = parse(update, bot_username=BOT_USERNAME)
        assert result_a is not None
        assert result_b is not None
        assert result_a.ts == result_b.ts, (
            "Same update_id must produce same ts so dedup works for retries"
        )

    def test_ts_uses_update_id_not_date(self):
        """ts value must be the update_id, not the message date."""
        update = {
            "update_id": 99999,
            "message": {
                "message_id": 5,
                "from": {"id": 1, "is_bot": False, "first_name": "T", "username": "t"},
                "chat": {"id": 1, "type": "private"},
                "date": 1111111111,
                "text": "hi",
            },
        }
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        # ts must be update_id string, not date string
        assert result.ts == "99999", (
            f"ts should be update_id '99999', not date '1111111111'; got {result.ts!r}"
        )

    def test_ts_fallback_when_update_id_missing(self):
        """When update_id is absent, ts falls back to 'chat_id:message_id'."""
        update = {
            # No update_id key
            "message": {
                "message_id": 42,
                "from": {"id": 7, "is_bot": False, "first_name": "X"},
                "chat": {"id": 123, "type": "private"},
                "date": 1700000000,
                "text": "no update_id",
            },
        }
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        # Fallback: "chat_id:message_id"
        assert result.ts == "123:42", (
            f"Fallback ts should be 'chat_id:message_id' '123:42'; got {result.ts!r}"
        )


# ---------------------------------------------------------------------------
# FIX 4: Telegram descriptor config_keys must use free_response_channels
# ---------------------------------------------------------------------------


class TestTelegramDescriptorFix4:
    """FIX 4: config_keys must list 'free_response_channels', not 'free_response_chats'."""

    def test_config_keys_has_free_response_channels_not_free_response_chats(self):
        """Descriptor must declare 'free_response_channels' to match pipeline."""
        platform = TelegramPlatform()
        keys = platform.descriptor.config_keys
        assert "free_response_channels" in keys, (
            f"config_keys must contain 'free_response_channels'; got {keys!r}"
        )
        assert "free_response_chats" not in keys, (
            f"config_keys must NOT contain 'free_response_chats' (old name); got {keys!r}"
        )


# ---------------------------------------------------------------------------
# FIX 5 (Telegram half): is_bot flag and own-bot filtering
# ---------------------------------------------------------------------------


def _bot_message(
    *,
    chat_id: int = -100111,
    from_id: int = 500,
    bot_username_in_from: str = "other_bot",
    text: str = "I am a bot",
    update_id: int = 8001,
) -> dict:
    """Build a Telegram message update where from.is_bot=True (another bot, not ours)."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "from": {
                "id": from_id,
                "is_bot": True,
                "first_name": "OtherBot",
                "username": bot_username_in_from,
            },
            "chat": {"id": chat_id, "type": "group", "title": "Dev"},
            "date": 1700000100,
            "text": text,
        },
    }


def _own_bot_message(
    *,
    chat_id: int = -100111,
    from_id: int = 42,  # will be "our" bot
    text: str = "I replied",
    update_id: int = 8002,
    bot_username_in_from: str = "my_test_bot",  # matches BOT_USERNAME
) -> dict:
    """A message from the bot itself (our own bot)."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": 11,
            "from": {
                "id": from_id,
                "is_bot": True,
                "first_name": "MyTestBot",
                "username": bot_username_in_from,
            },
            "chat": {"id": chat_id, "type": "group", "title": "Dev"},
            "date": 1700000101,
            "text": text,
        },
    }


class TestTelegramBotFiltering:
    """FIX 5 (Telegram): other-bot messages set is_bot=True; own-bot drops to None."""

    def test_other_bot_message_returns_inbound_message_with_is_bot_true(self):
        """A message from another bot (not our own) → InboundMessage with is_bot=True."""
        update = _bot_message(bot_username_in_from="other_bot", text="bot says hi")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None, (
            "Other bot's message must NOT be hard-dropped at parse; got None"
        )
        assert result.is_bot is True, (
            f"is_bot should be True for a message from another bot; got {result.is_bot!r}"
        )

    def test_own_bot_message_returns_none(self):
        """A message from our own bot (same username as bot_username) → None."""
        update = _own_bot_message(bot_username_in_from="my_test_bot")
        result = parse(update, bot_username="@my_test_bot")
        assert result is None, (
            "Our own bot's message must be dropped at parse (return None); "
            f"got {result!r}"
        )

    def test_human_message_has_is_bot_false(self):
        """A regular human message → InboundMessage with is_bot=False (default)."""
        update = _private_message(text="hello from human")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.is_bot is False, (
            f"Human message must have is_bot=False; got {result.is_bot!r}"
        )

    def test_bot_message_with_empty_bot_username_returns_none(self):
        """from.is_bot=True, bot_username='' (resolution failed) → None (loop safety)."""
        update = _bot_message(bot_username_in_from="other_bot", text="hi from bot")
        result = parse(update, bot_username="")
        assert result is None, (
            "When bot_username is empty (resolution failed), all bot messages must be dropped; "
            f"got {result!r}"
        )

    def test_own_bot_message_case_insensitive_returns_none(self):
        """Own bot's username matched case-insensitively → None."""
        update = _own_bot_message(bot_username_in_from="My_Test_Bot")
        result = parse(update, bot_username="@my_test_bot")
        assert result is None, (
            "Own bot's message (case-insensitive username match) must return None; "
            f"got {result!r}"
        )

    def test_bot_message_different_bot_empty_username_unknown_own_id_returns_none(self):
        """from.is_bot=True, sender='other_bot', bot_username='' → None (safety: drop all bots)."""
        update = _bot_message(bot_username_in_from="other_bot", text="i am some bot")
        result = parse(update, bot_username="")
        assert result is None, (
            "With empty bot_username, even a different-named bot message must be dropped; "
            f"got {result!r}"
        )
