"""Tests for surogates.channels.platforms.telegram — parse + verify + identifier_of.

TDD: tests written for the new webhook-based Telegram platform module.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from surogates.channels.platforms.telegram import (
    TelegramPlatform,
    identifier_of,
    parse,
    verify,
)
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

    def test_private_message_ts_is_date_string(self):
        update = _private_message(date=1700000000, text="Hi")
        result = parse(update, bot_username=BOT_USERNAME)
        assert result is not None
        assert result.ts == "1700000000"

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
