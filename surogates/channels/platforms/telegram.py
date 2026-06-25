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

import httpx

from surogates.channels.base import SendResult
from surogates.channels.inbound import InboundMessage
from surogates.channels.registry import ChannelDescriptor

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

    # FIX 5: Detect bot messages.
    # Drop OWN bot messages (the bot's username matches ours).
    # For OTHER bots, return an InboundMessage with is_bot=True.
    sender_is_bot: bool = bool(from_user.get("is_bot", False))
    is_bot: bool = False
    if sender_is_bot:
        clean_bot = bot_username.lstrip("@").lower()
        sender_username = (from_user.get("username") or "").lower()
        # Loop safety: drop if (a) we can't identify our own bot username
        # (resolution failed → empty), OR (b) this IS our own bot's message.
        if (not clean_bot) or (sender_username == clean_bot):
            return None
        # A different bot's message — mark is_bot=True.
        is_bot = True

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
    # FIX 1: Use update_id as dedup key (globally unique per bot; Telegram
    # repeats the same update_id on webhook retries, so dedup still catches
    # real retries). Fall back to "chat_id:message_id" if update_id is absent.
    update_id = body.get("update_id")
    message_id = message.get("message_id")
    if update_id is not None:
        ts = str(update_id)
    else:
        ts = f"{identifier}:{message_id}"

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
        is_bot=is_bot,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bot_api_url(bot_token: str, method: str) -> str:
    """Return the full Telegram Bot API URL for *method*."""
    return f"https://api.telegram.org/bot{bot_token}/{method}"


# ---------------------------------------------------------------------------
# TelegramPlatform — ChannelPlatform implementation
# ---------------------------------------------------------------------------


async def _register_webhook_impl(
    identifier: str, url: str, creds: dict
) -> None:
    """Async implementation for :attr:`TelegramPlatform.descriptor.register_webhook`.

    Calls ``setWebhook`` on the Telegram Bot API.  Raises :class:`RuntimeError`
    when the response ``ok`` field is ``False`` so the reconciler's
    per-identifier error isolation can log it.
    """
    bot_token: str = creds.get("bot_token") or ""
    webhook_secret: str = creds.get("webhook_secret") or ""
    api_url = _bot_api_url(bot_token, "setWebhook")

    payload: dict[str, Any] = {
        "url": url,
        "allowed_updates": ["message", "callback_query"],
    }
    if webhook_secret:
        payload["secret_token"] = webhook_secret

    async with httpx.AsyncClient() as client:
        resp = await client.post(api_url, json=payload)

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError(
            f"TelegramPlatform.register_webhook: non-JSON response "
            f"(status={resp.status_code}) for identifier={identifier!r}"
        )

    if not data.get("ok"):
        description = data.get("description", "no description")
        raise RuntimeError(
            f"TelegramPlatform.register_webhook: setWebhook returned ok=false "
            f"for identifier={identifier!r}: {description}"
        )

    logger.debug(
        "TelegramPlatform: setWebhook succeeded for identifier=%r url=%r",
        identifier,
        url,
    )


class TelegramPlatform:
    """Webhook-based Telegram channel platform strategy.

    Implements :class:`~surogates.channels.registry.ChannelPlatform`.

    Each Telegram bot is identified by its username, which is the
    ``{username}`` path parameter in the webhook URL.  Credentials
    (``bot_token`` and ``webhook_secret``) are resolved by the dispatcher
    and passed to every method that requires them.

    The bot username is available directly from the URL path (the
    ``identifier`` kwarg passed to :meth:`parse` by the dispatcher), so no
    ``getMe`` network round-trip is needed on the hot inbound path.
    """

    kind = "telegram"
    topology = "webhook"

    descriptor = ChannelDescriptor(
        vault_refs=lambda identifier: {
            "bot_token": "bot_token",
            "webhook_secret": "webhook_secret",
        },
        config_keys=(
            "require_mention",
            "free_response_channels",
            "mention_patterns",
            "reply_to_mode",
            "reactions_enabled",
            "per_user_groups",
        ),
        webhook_registration="api",
        register_webhook=_register_webhook_impl,
    )

    def __init__(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Route path
    # ------------------------------------------------------------------

    def route_path(self, identifier: str | None = None) -> str:
        """Return the FastAPI path for this platform.

        Parameters
        ----------
        identifier:
            Bot username (e.g. ``"@my_bot"``).  When ``None`` (template form
            used by ``build_app``), returns the parametrised path template.
        """
        if identifier is None:
            return "/telegram/{username}"
        return f"/telegram/{identifier}"

    # ------------------------------------------------------------------
    # identifier_of / verify — delegates to module functions
    # ------------------------------------------------------------------

    def identifier_of(self, request: Any, body: Any) -> str:
        return identifier_of(request, body)

    def verify(self, request: Any, raw_body: bytes, *, creds: dict) -> bool:
        return verify(request, raw_body, creds=creds)

    # ------------------------------------------------------------------
    # parse — async; uses path identifier as bot username
    # ------------------------------------------------------------------

    async def parse(
        self,
        body: Any,
        *,
        creds: dict | None = None,
        identifier: str | None = None,
    ) -> InboundMessage | None:
        """Parse a Telegram update body into an :class:`InboundMessage`.

        Uses the ``identifier`` kwarg (the bot username from the URL path
        resolved by the dispatcher) for mention detection.  No network
        call is made — ``getMe`` is not needed because the path parameter
        is the authoritative bot identity for Telegram webhooks.

        Parameters
        ----------
        body:
            Parsed Telegram JSON update.
        creds:
            Credential dict — accepted for protocol compatibility but not
            used for username resolution.
        identifier:
            The bot username from the URL path (e.g. ``"@my_bot"``).
            When ``None`` (e.g. called from tests without a dispatcher),
            mention detection is skipped (``bot_username=""``).
        """
        bot_username: str = identifier or ""
        return parse(body, bot_username=bot_username)

    # ------------------------------------------------------------------
    # send — POST sendMessage to the Telegram Bot API
    # ------------------------------------------------------------------

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        """Post an outbox item to Telegram via ``sendMessage``.

        Parameters
        ----------
        item:
            Outbox item with ``destination`` (``chat_id``, optional
            ``message_thread_id`` for Telegram forum topics) and ``payload``
            (``content``).
        creds:
            Credential dict with ``bot_token``.

        Returns
        -------
        SendResult
            ``success=True`` with ``message_id`` on success; ``success=False``
            with ``error`` on any Telegram API error or HTTP failure.  Never
            raises.
        """
        bot_token: str = creds.get("bot_token") or ""
        api_url = _bot_api_url(bot_token, "sendMessage")

        chat_id = item.destination.get("chat_id")
        text: str = item.payload.get("content", "")
        message_thread_id: Any = item.destination.get("message_thread_id")

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(api_url, json=payload)
            data = resp.json()
            if data.get("ok"):
                msg_id = str(data["result"]["message_id"])
                return SendResult(success=True, message_id=msg_id)
            description: str = data.get("description", "Telegram API error")
            return SendResult(success=False, error=description)
        except Exception as exc:
            logger.error("[TelegramPlatform] sendMessage failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # handle_non_message_update — callback_query ack-only
    # ------------------------------------------------------------------

    async def handle_non_message_update(
        self, body: Any, *, routing: Any, creds: dict, deps: Any
    ) -> bool:
        """Handle non-message Telegram updates.

        Currently handles ``callback_query`` updates by sending an
        ``answerCallbackQuery`` ack (which stops Telegram's loading spinner)
        and returning ``True`` (handled; inbound pipeline skipped).

        All other update types return ``False`` so the dispatcher falls
        through to ``parse``.

        Approval rendering and resolution
        ----------------------------------
        This method ACKs the callback query but does **not** implement
        approval resolution.  Full approval handling (rendering approval
        buttons in ``send``, persisting the decision to a durable store such
        as Redis, and unblocking the waiting session) is a unified
        cross-platform follow-up task that applies to both Slack (``/interact``
        is also ack-only today) and Telegram.  The old in-process dict pattern
        from the Socket Mode adapter is intentionally not ported — it is
        process-local and wrong for the stateless multi-replica model.

        Parameters
        ----------
        body:
            Parsed Telegram JSON update.
        routing:
            Routing object from the dispatcher (may be ``None`` in tests).
        creds:
            Resolved credential dict with ``bot_token``.
        deps:
            Pipeline deps (not used here).

        Returns
        -------
        True
            Update was fully handled; pipeline must not be called.
        False
            Fall through to the inbound pipeline (``parse`` will handle it).
        """
        callback_query = body.get("callback_query")
        if callback_query is None:
            return False

        callback_id: str = str(callback_query.get("id", ""))
        callback_data: str | None = callback_query.get("data")
        logger.debug(
            "[TelegramPlatform] callback_query ack — id=%r data=%r "
            "(approval handling is a follow-up)",
            callback_id,
            callback_data,
        )

        bot_token: str = creds.get("bot_token") or ""
        if bot_token and callback_id:
            try:
                api_url = _bot_api_url(bot_token, "answerCallbackQuery")
                async with httpx.AsyncClient() as client:
                    await client.post(
                        api_url, json={"callback_query_id": callback_id}
                    )
            except Exception as exc:
                logger.debug(
                    "[TelegramPlatform] answerCallbackQuery failed: %s", exc
                )

        return True


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def _register() -> None:
    """Register the singleton TelegramPlatform in the module-level registry.

    Called once at import time.  Guarded against double-registration so that
    test suites that reimport the module (e.g. via importlib.reload) do not
    raise a ValueError from the registry.
    """
    from surogates.channels.registry import registry

    if registry.get("telegram") is None:
        registry.register(TelegramPlatform())


_register()
