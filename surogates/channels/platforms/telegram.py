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
from surogates.channels.inbound import InboundFileRef, InboundMessage
from surogates.channels.registry import ChannelDescriptor
from surogates.channels.platforms.telegram_format import render_html, render_plain
from surogates.channels.text_split import split_text

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


# Telegram service messages (member join/leave, chat created, pinned, …) are
# not user messages to respond to.  Dropping them explicitly — rather than
# relying on the no-text guard below — keeps the loop-safety contract from
# registry.py honest: a service message that ever carries text can't slip
# through and get its (often human) sender auto-provisioned.  Mirrors the Slack
# parser's system-subtype drop.
_SERVICE_MESSAGE_KEYS = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "pinned_message",
    "message_auto_delete_timer_changed",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
)


def _parse(body: dict, *, bot_username: str) -> InboundMessage | None:
    """Internal parser — may raise; wrapped by :func:`parse`."""
    # Only handle message updates.
    message = body.get("message")
    if message is None:
        return None

    if any(key in message for key in _SERVICE_MESSAGE_KEYS):
        return None

    # ------------------------------------------------------------------
    # Chat type → is_dm
    # ------------------------------------------------------------------
    chat = message.get("chat", {})
    chat_type = chat.get("type", "")

    if chat_type == "private":
        is_dm = True
        visibility = "dm"
    else:
        is_dm = False
        visibility = "private"  # group/supergroup/channel → isolated (no public tier)

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
    # Text, media, and content guard
    # ------------------------------------------------------------------
    text: str = message.get("text") or message.get("caption") or ""
    files = _extract_files(message)
    if not text and not files:
        # No usable content — nothing to do.
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
    if message_id is not None:
        source["message_id"] = message_id
    if message_thread_id is not None:
        source["message_thread_id"] = message_thread_id

    kind = "text"
    if files:
        first_mime = files[0].mime_type
        if first_mime.startswith("image/"):
            kind = "image"
        elif first_mime.startswith("audio/"):
            kind = "audio"
        else:
            kind = "document"

    return InboundMessage(
        kind=kind,
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
        visibility=visibility,
        files=files,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _bot_api_url(bot_token: str, method: str) -> str:
    """Return the full Telegram Bot API URL for *method*."""
    return f"https://api.telegram.org/bot{bot_token}/{method}"


def _extract_files(message: dict) -> list[InboundFileRef]:
    """Extract media attachments from a Telegram message as file refs.

    Telegram exposes attachments as ``file_id`` handles, not URLs — the
    actual download URL is only resolvable with the bot token via
    ``getFile``.  The ref therefore carries the ``file_id`` in *both*
    ``url`` and ``file_id``: :meth:`TelegramPlatform.download_file` accepts
    a file id where other platforms take a URL.
    """
    files: list[InboundFileRef] = []

    photos = message.get("photo") or []
    if photos:
        # PhotoSize array is ordered smallest→largest; take the largest.
        best = max(photos, key=lambda p: p.get("file_size") or 0)
        file_id = best.get("file_id")
        if file_id:
            files.append(
                InboundFileRef(
                    url=file_id,
                    filename=f"photo-{best.get('file_unique_id', file_id)}.jpg",
                    mime_type="image/jpeg",
                    size=best.get("file_size"),
                    file_id=file_id,
                )
            )

    for key, default_name, default_mime in (
        ("document", "document", "application/octet-stream"),
        ("voice", "voice.ogg", "audio/ogg"),
        ("audio", "audio", "audio/mpeg"),
        ("video", "video.mp4", "video/mp4"),
        ("video_note", "video-note.mp4", "video/mp4"),
    ):
        media = message.get(key)
        if not media:
            continue
        file_id = media.get("file_id")
        if not file_id:
            continue
        filename = media.get("file_name") or (
            f"{default_name}-{media.get('file_unique_id', file_id)}"
            if "." not in default_name
            else default_name
        )
        files.append(
            InboundFileRef(
                url=file_id,
                filename=filename,
                mime_type=media.get("mime_type") or default_mime,
                size=media.get("file_size"),
                file_id=file_id,
            )
        )
    return files


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
        # Shared HTTP client reused for all Bot API calls (sendMessage,
        # answerCallbackQuery, etc.).  The bot token is embedded in each
        # request URL, not in the client, so one client per platform
        # instance suffices.  TelegramPlatform instances are process-lifetime
        # singletons; no explicit close is needed.
        self._http: httpx.AsyncClient = httpx.AsyncClient()

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

    # Telegram hard-caps sendMessage text at 4096 chars.  Source chunks are
    # cut shorter so the HTML rendering (tags + entity escaping) still fits.
    _MAX_MESSAGE_CHARS = 4096
    _MAX_SOURCE_CHUNK = 3500

    async def _post_text(
        self,
        api_url: str,
        base: dict[str, Any],
        *,
        html_text: str,
        plain_text: str,
    ) -> tuple[str | None, str | None]:
        """POST one message, preferring HTML with a plain-text retry.

        Returns ``(message_id, None)`` on success, ``(None, error)`` on
        failure.  A parse rejection of the HTML body (Telegram's
        "can't parse entities") is retried once as plain text so a
        formatting edge case never loses the reply.
        """
        attempts: list[dict[str, Any]] = [
            {**base, "text": html_text, "parse_mode": "HTML"},
            {**base, "text": plain_text},
        ]
        error: str | None = None
        for attempt_no, payload in enumerate(attempts):
            try:
                resp = await self._http.post(api_url, json=payload)
                data = resp.json()
            except Exception as exc:
                logger.error("[TelegramPlatform] sendMessage failed: %s", exc)
                return None, str(exc)
            if data.get("ok"):
                return str(data["result"]["message_id"]), None
            error = data.get("description", "Telegram API error")
            if attempt_no == 0 and "parse" in error.lower():
                logger.warning(
                    "[TelegramPlatform] HTML rejected (%s); retrying as plain text",
                    error,
                )
                continue
            return None, error
        return None, error

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        """Post an outbox item to Telegram via ``sendMessage``.

        Markdown content is rendered to Telegram HTML (plain-text retry on a
        parse rejection), split at natural boundaries to stay under the 4096
        char cap, and — when the session carries a ``reply_to_message_id`` —
        the first chunk is sent as a reply to the triggering message.

        Parameters
        ----------
        item:
            Outbox item with ``destination`` (``chat_id``, optional
            ``message_thread_id`` for Telegram forum topics, optional
            ``reply_to_message_id``) and ``payload`` (``content``, optional
            ``input_prompt``).
        creds:
            Credential dict with ``bot_token``.

        Returns
        -------
        SendResult
            ``success=True`` with the last ``message_id`` on success;
            ``success=False`` with ``error`` on any Telegram API error or
            HTTP failure.  Never raises.  When a multi-chunk send fails
            midway, the already-delivered prefix is reported as success so a
            redelivery retry cannot spam duplicates.
        """
        bot_token: str = creds.get("bot_token") or ""
        api_url = _bot_api_url(bot_token, "sendMessage")

        chat_id = item.destination.get("chat_id")
        message_thread_id: Any = item.destination.get("message_thread_id")
        reply_to: Any = item.destination.get("reply_to_message_id")

        base: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id is not None:
            base["message_thread_id"] = message_thread_id

        if item.payload.get("input_prompt"):
            return await self._send_input_prompt(item, api_url=api_url, base=base)

        text: str = item.payload.get("content", "")
        chunks = split_text(text, self._MAX_SOURCE_CHUNK) or [text]

        last_id: str | None = None
        for index, chunk in enumerate(chunks):
            chunk_base = dict(base)
            if reply_to and index == 0:
                # allow_sending_without_reply: a user-deleted reply target
                # must degrade to a plain message, not fail the delivery.
                chunk_base["reply_parameters"] = {
                    "message_id": reply_to,
                    "allow_sending_without_reply": True,
                }
            html_text = render_html(chunk)
            plain_text = render_plain(chunk)
            if len(html_text) > self._MAX_MESSAGE_CHARS:
                # Pathological escaping inflation — deliver plain sub-chunks
                # rather than risking a hard API rejection.
                sub_error: str | None = None
                for sub in split_text(plain_text, self._MAX_MESSAGE_CHARS):
                    msg_id, sub_error = await self._post_text(
                        api_url, chunk_base, html_text=sub, plain_text=sub,
                    )
                    if msg_id is None:
                        break
                    last_id = msg_id
                if sub_error is not None:
                    break
                continue
            msg_id, error = await self._post_text(
                api_url, chunk_base, html_text=html_text, plain_text=plain_text,
            )
            if msg_id is None:
                if last_id is not None:
                    # Part of the reply landed; report the delivered prefix
                    # instead of triggering a duplicate redelivery.
                    return SendResult(success=True, message_id=last_id)
                return SendResult(success=False, error=error or "send failed")
            last_id = msg_id
        if last_id is None:
            return SendResult(success=False, error="send failed")
        return SendResult(success=True, message_id=last_id)

    async def _send_input_prompt(
        self, item: Any, *, api_url: str, base: dict[str, Any],
    ) -> SendResult:
        """Post an ``ask_user_question`` prompt with inline-keyboard choices.

        The button ``callback_data`` carries only coordinates
        (``si:<session>:<q>:<c>``); the durable pending-input record is the
        source of truth when the button is tapped (see
        :meth:`handle_non_message_update`).
        """
        from surogates.channels.platforms.telegram_interactive import (
            build_input_prompt,
        )

        html_text, plain_text, reply_markup = build_input_prompt(
            session_id=str(getattr(item, "session_id", "")),
            questions=item.payload.get("questions") or [],
            context=item.payload.get("context", ""),
            tool_call_id=item.payload.get("tool_call_id", ""),
        )
        payload = dict(base)
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        msg_id, error = await self._post_text(
            api_url, payload, html_text=html_text, plain_text=plain_text,
        )
        if msg_id is None:
            return SendResult(success=False, error=error or "send failed")
        return SendResult(success=True, message_id=msg_id)

    # ------------------------------------------------------------------
    # send_private — DM the sender a link prompt
    # ------------------------------------------------------------------

    async def send_private(
        self,
        creds: dict,
        *,
        sender_id: str,
        chat_id: str,
        is_dm: bool,
        text: str,
    ) -> bool:
        """Privately deliver *text* to *sender_id*.

        On Telegram a user's id is their private chat id, so this is a plain
        ``sendMessage`` to the sender.  Returns ``True`` on delivery, ``False``
        on any API/HTTP error (never raises) — the caller withholds the code if
        private delivery fails.
        """
        bot_token: str = creds.get("bot_token") or ""
        api_url = _bot_api_url(bot_token, "sendMessage")
        try:
            resp = await self._http.post(api_url, json={"chat_id": sender_id, "text": text})
            return bool(resp.json().get("ok"))
        except Exception as exc:
            logger.error("[TelegramPlatform] send_private failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # handle_non_message_update — callback_query ack-only
    # ------------------------------------------------------------------

    async def post_input_nudge(
        self, *, creds: dict, channel: str, thread_ts: Any, text: str,
    ) -> str | None:
        """Post a short status line (answer ack, /stop ack) to the chat.

        Same optional-member contract as the Slack implementation: the
        pipeline's ``input_nudge`` hook getattr-dispatches to this. Returns
        the message id, or ``None`` on any failure (never raises).
        """
        bot_token: str = (creds or {}).get("bot_token") or ""
        if not bot_token or not channel:
            return None
        payload: dict[str, Any] = {"chat_id": channel, "text": text}
        if thread_ts:
            try:
                payload["message_thread_id"] = int(thread_ts)
            except (TypeError, ValueError):
                pass
        try:
            resp = await self._http.post(
                _bot_api_url(bot_token, "sendMessage"), json=payload
            )
            data = resp.json()
            if data.get("ok"):
                return str(data["result"]["message_id"])
            return None
        except Exception:
            logger.warning(
                "[TelegramPlatform] post_input_nudge failed for %s",
                channel, exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # download_file — resolve a file_id via getFile and fetch the bytes
    # ------------------------------------------------------------------

    async def download_file(
        self, *, creds: dict, url: str, max_bytes: int,
    ) -> bytes | None:
        """Download a Telegram file by its ``file_id``.

        *url* is the Telegram ``file_id`` (see :func:`_extract_files`) — it
        is resolved to a real path via ``getFile`` and fetched from
        ``api.telegram.org``.  Returns the bytes, or ``None`` on a missing
        token/file, oversize content, or any API/HTTP failure (never raises)
        — the same contract as the Slack downloader.
        """
        bot_token = (creds or {}).get("bot_token") or ""
        file_id = url or ""
        if not bot_token or not file_id:
            return None
        try:
            resp = await self._http.post(
                _bot_api_url(bot_token, "getFile"), json={"file_id": file_id}
            )
            data = resp.json()
            if not data.get("ok"):
                logger.warning(
                    "[TelegramPlatform] getFile failed for %s: %s",
                    file_id, data.get("description", ""),
                )
                return None
            result = data.get("result") or {}
            file_path = result.get("file_path") or ""
            declared = result.get("file_size")
            if not file_path or ".." in file_path:
                return None
            if declared is not None and int(declared) > max_bytes:
                logger.warning(
                    "[TelegramPlatform] file %s over cap (%s bytes)",
                    file_id, declared,
                )
                return None
            download_url = (
                f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            )
            resp = await self._http.get(download_url)
            if resp.status_code < 200 or resp.status_code >= 300:
                logger.warning(
                    "[TelegramPlatform] file download %s -> HTTP %s",
                    file_id, resp.status_code,
                )
                return None
            body = resp.content
            if len(body) > max_bytes:
                logger.warning(
                    "[TelegramPlatform] file %s body over cap (%d bytes)",
                    file_id, len(body),
                )
                return None
            return body
        except Exception:
            logger.warning(
                "[TelegramPlatform] download_file failed for %s",
                file_id, exc_info=True,
            )
            return None

    # ------------------------------------------------------------------
    # ack_received — emoji reaction on the inbound message
    # ------------------------------------------------------------------

    async def ack_received(self, msg: Any, *, creds: dict, config: dict) -> None:
        """React 👀 to the just-received message when ``reactions_enabled``.

        Called by the dispatcher after the inbound pipeline accepts a
        message — the Telegram-native "seen, working on it" signal (the
        Thinking-placeholder equivalent).  Best-effort; never raises.
        """
        if not (config or {}).get("reactions_enabled"):
            return
        bot_token: str = (creds or {}).get("bot_token") or ""
        message_id = (msg.source or {}).get("message_id")
        if not bot_token or message_id is None:
            return
        try:
            await self._http.post(
                _bot_api_url(bot_token, "setMessageReaction"),
                json={
                    "chat_id": msg.identifier,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": "👀"}],
                },
            )
        except Exception as exc:
            logger.debug("[TelegramPlatform] setMessageReaction failed: %s", exc)

    async def _answer_callback(
        self, bot_token: str, callback_id: str, text: str | None = None,
    ) -> None:
        """Ack a callback query (stops the loading spinner). Best-effort."""
        if not bot_token or not callback_id:
            return
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            await self._http.post(
                _bot_api_url(bot_token, "answerCallbackQuery"), json=payload
            )
        except Exception as exc:
            logger.debug("[TelegramPlatform] answerCallbackQuery failed: %s", exc)

    async def handle_non_message_update(
        self, body: Any, *, routing: Any, creds: dict, deps: Any
    ) -> bool:
        """Handle non-message Telegram updates.

        ``callback_query`` updates from input-prompt buttons
        (``si:<session>:<q>:<c>`` callback data) resolve the session's
        durable pending ``ask_user_question`` record: the choice is written
        via :func:`resolve_input_response` (which emits the
        ``ASK_USER_QUESTION_RESPONSE`` event the waiting tool polls for),
        the button message is edited to show the answer, and the callback is
        acked.  Stale or foreign callbacks are acked without side effects.

        All other update types return ``False`` so the dispatcher falls
        through to ``parse``.

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

        from surogates.channels.platforms.telegram_interactive import (
            build_answered_text,
            parse_callback_data,
            tool_call_digest,
        )
        from surogates.session.interactive_input import (
            pending_input_for_session,
            resolve_input_response,
        )

        bot_token: str = creds.get("bot_token") or ""
        callback_id: str = str(callback_query.get("id", ""))
        parsed = parse_callback_data(callback_query.get("data") or "")
        store = getattr(deps, "session_store", None)
        if parsed is None or store is None:
            await self._answer_callback(bot_token, callback_id)
            return True
        session_id_raw, _question_index, choice_index, digest = parsed

        try:
            from uuid import UUID

            session_id = UUID(session_id_raw)
            session = await store.get_session(session_id)
        except Exception:
            logger.warning(
                "[TelegramPlatform] callback for unknown session %r",
                session_id_raw,
            )
            await self._answer_callback(bot_token, callback_id)
            return True

        # Anti-forgery: the callback must originate from the chat this
        # session is bound to — callback_data is attacker-visible, the chat
        # binding is not attacker-controllable.
        chat_id = str(
            ((callback_query.get("message") or {}).get("chat") or {}).get("id", "")
        )
        bound_chat = str((session.config or {}).get("telegram_channel_id", ""))
        if not chat_id or chat_id != bound_chat:
            logger.warning(
                "[TelegramPlatform] callback chat %r does not match session chat %r",
                chat_id, bound_chat,
            )
            await self._answer_callback(bot_token, callback_id)
            return True

        pending = await pending_input_for_session(store, session_id=session_id)
        if not pending or tool_call_digest(pending.get("tool_call_id", "")) != digest:
            # No pending question, or the button belongs to an EARLIER
            # prompt than the one currently pending — a stale button must
            # never resolve a newer question with its coordinates.
            await self._answer_callback(
                bot_token, callback_id, "This question was already answered."
            )
            return True

        questions: list = pending.get("questions") or []
        choices = (questions[0] if questions else {}).get("choices") or []
        if choice_index < 0 or choice_index >= len(choices):
            await self._answer_callback(bot_token, callback_id)
            return True
        prompt = (questions[0].get("prompt") if questions else "") or "Question 1"
        answer = choices[choice_index].get("label") or ""
        responses = [{"question": prompt, "answer": answer, "is_other": False}]

        resolved = await resolve_input_response(
            store,
            session_id=session_id,
            tool_call_id=pending.get("tool_call_id", ""),
            responses=responses,
        )
        if not resolved:
            await self._answer_callback(
                bot_token, callback_id, "This question was already answered."
            )
            return True

        # Replace the button message with the recorded answer. Best-effort.
        message = callback_query.get("message") or {}
        message_id = message.get("message_id")
        if message_id is not None:
            try:
                await self._http.post(
                    _bot_api_url(bot_token, "editMessageText"),
                    json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": build_answered_text(responses),
                        "parse_mode": "HTML",
                    },
                )
            except Exception as exc:
                logger.debug(
                    "[TelegramPlatform] editMessageText failed: %s", exc
                )

        await self._answer_callback(bot_token, callback_id, "Got it.")
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
