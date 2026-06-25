"""Telegram webhook channel platform strategy.

Exposes three stateless module-level functions used by the dispatcher and the
registered :class:`TelegramPlatform` object that implements
:class:`~surogates.channels.registry.ChannelPlatform`.

Module-level functions
----------------------
identifier_of(request, body) -> str
    Reads the bot username from the URL path parameter ``username``.  The
    path is the authoritative identifier because Telegram webhook updates
    carry no bot identity in the body.

verify(request, raw_body, *, creds) -> bool
    Validates the Telegram webhook request by comparing the
    ``X-Telegram-Bot-Api-Secret-Token`` header against the stored
    ``webhook_secret`` credential using a constant-time comparison.  A
    missing/empty stored secret or missing/mismatched header → ``False``.

parse(body, *, bot_username) -> InboundMessage | None
    Converts a raw Telegram update dict (JSON body as parsed by FastAPI)
    into an :class:`~surogates.channels.inbound.InboundMessage`.

    Only ``message`` updates are handled here.  ``callback_query`` and
    other update types return ``None`` (handled elsewhere by the framework).
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from surogates.channels.inbound import InboundMessage

__all__ = [
    "TelegramPlatform",
    "identifier_of",
    "verify",
    "parse",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# identifier_of
# ---------------------------------------------------------------------------


def identifier_of(request: Any, body: Any) -> str:
    """Return the bot username from the URL path parameter.

    The path parameter ``username`` is the authoritative identifier because
    Telegram updates carry no bot identity in the body.  This is safe to call
    before the body is parsed.

    Parameters
    ----------
    request:
        Starlette-like request object exposing ``path_params["username"]``.
    body:
        Parsed request body — intentionally ignored.
    """
    return request.path_params["username"]


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify(
    request: Any,
    raw_body: bytes,
    *,
    creds: dict,
) -> bool:
    """Validate a Telegram webhook request via the secret-token header.

    Telegram sends the ``X-Telegram-Bot-Api-Secret-Token`` header on every
    webhook POST when the webhook was registered with a ``secret_token``.
    There is no body signature — the header itself is the auth.

    Parameters
    ----------
    request:
        Starlette-like request object exposing ``headers``.
    raw_body:
        Raw request body bytes — not used for Telegram (no body signature).
    creds:
        Credential dict; must contain ``webhook_secret``.

    Returns
    -------
    True
        Header matches the stored secret.
    False
        Missing/empty stored secret, missing header, or header mismatch.
    """
    stored_secret: str = creds.get("webhook_secret") or ""
    if not stored_secret:
        logger.debug("verify: no webhook_secret configured — rejecting")
        return False

    headers = request.headers
    header_value: str | None = None
    if hasattr(headers, "get"):
        header_value = headers.get("X-Telegram-Bot-Api-Secret-Token") or headers.get(
            "x-telegram-bot-api-secret-token"
        )

    if not header_value:
        logger.debug("verify: X-Telegram-Bot-Api-Secret-Token header missing")
        return False

    return hmac.compare_digest(stored_secret, header_value)


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def parse(body: dict, *, bot_username: str) -> InboundMessage | None:
    """Convert a raw Telegram update dict to an :class:`InboundMessage`.

    Only ``message`` updates are handled; ``callback_query`` and other
    update types return ``None`` so the caller can route them elsewhere.

    Parameters
    ----------
    body:
        Raw Telegram update dict (JSON-decoded webhook body).
    bot_username:
        The bot's username (with or without leading ``@``).  Used to detect
        @-mentions in the message text.

    Returns
    -------
    InboundMessage
        When the update contains a processable text message.
    None
        For non-message updates, messages with no usable content, or on any
        missing-key error.
    """
    try:
        return _parse(body, bot_username=bot_username)
    except Exception:
        logger.debug("parse: unexpected error parsing Telegram update", exc_info=True)
        return None


def _parse(body: dict, *, bot_username: str) -> InboundMessage | None:
    """Internal parser — may raise; wrapped by :func:`parse`."""
    # Only handle message updates.
    message = body.get("message")
    if message is None:
        return None

    # ------------------------------------------------------------------
    # Chat type → is_dm
    # ------------------------------------------------------------------
    chat = message.get("chat", {})
    chat_type = chat.get("type", "")

    if chat_type == "private":
        is_dm = True
    else:
        is_dm = False

    # ------------------------------------------------------------------
    # Thread key — forum supergroups use message_thread_id.
    # ------------------------------------------------------------------
    thread_key: str | None = None
    message_thread_id = message.get("message_thread_id")
    if message_thread_id is not None:
        thread_key = str(message_thread_id)

    # ------------------------------------------------------------------
    # Sender
    # ------------------------------------------------------------------
    from_user = message.get("from", {})
    from_id = from_user.get("id")
    if from_id is None:
        # Anonymous channel posts have no "from"; skip.
        return None

    platform_user_id = str(from_id)
    user_name = (
        from_user.get("username")
        or from_user.get("first_name")
        or platform_user_id
    )

    # ------------------------------------------------------------------
    # Text and content guard
    # ------------------------------------------------------------------
    text: str = message.get("text", "")
    if not text:
        # No usable text content — nothing to do.
        return None

    # ------------------------------------------------------------------
    # Mention detection — case-insensitive @bot_username in text.
    # ------------------------------------------------------------------
    clean_username = bot_username.lstrip("@")
    is_mention = f"@{clean_username}".lower() in text.lower()

    # ------------------------------------------------------------------
    # Identifiers
    # ------------------------------------------------------------------
    identifier = str(chat.get("id", ""))
    ts = str(message.get("date", ""))

    # ------------------------------------------------------------------
    # Source metadata
    # ------------------------------------------------------------------
    is_forum = bool(chat.get("is_forum", False))
    source: dict = {
        "platform": "telegram",
        "chat_type": chat_type,
        "chat_id": identifier,
        "is_forum": is_forum,
    }
    if message_thread_id is not None:
        source["message_thread_id"] = message_thread_id

    return InboundMessage(
        kind="text",
        identifier=identifier,
        thread_key=thread_key,
        platform_user_id=platform_user_id,
        user_name=user_name,
        text=text,
        media_urls=[],
        media_types=[],
        is_dm=is_dm,
        is_mention=is_mention,
        ts=ts,
        source=source,
    )


# ---------------------------------------------------------------------------
# TelegramPlatform — ChannelPlatform implementation (stub; full strategy next)
# ---------------------------------------------------------------------------


class TelegramPlatform:
    """Webhook-based Telegram channel platform strategy.

    Implements :class:`~surogates.channels.registry.ChannelPlatform`.

    Each Telegram bot is identified by its username, which is the
    ``{username}`` path parameter in the webhook URL.  Credentials
    (``bot_token`` and ``webhook_secret``) are resolved by the dispatcher
    and passed to every method that requires them.
    """

    kind = "telegram"
    topology = "webhook"

    def route_path(self, identifier: str | None = None) -> str:
        """Return the FastAPI path for this platform."""
        if identifier is None:
            return "/telegram/{username}"
        return f"/telegram/{identifier}"

    def identifier_of(self, request: Any, body: Any) -> str:
        return identifier_of(request, body)

    def verify(self, request: Any, raw_body: bytes, *, creds: dict) -> bool:
        return verify(request, raw_body, creds=creds)

    async def parse(
        self, body: Any, *, creds: dict | None = None
    ) -> InboundMessage | None:
        """Parse a Telegram update body into an :class:`InboundMessage`.

        The bot username is taken from ``creds["bot_username"]`` if present,
        otherwise falls back to an empty string (mention detection is skipped).
        """
        bot_username: str = (creds or {}).get("bot_username") or ""
        return parse(body, bot_username=bot_username)
