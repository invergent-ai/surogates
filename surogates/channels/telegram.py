"""Telegram channel adapter.

Uses python-telegram-bot library for:
- Receiving messages from users/groups
- Sending responses back
- Handling media and commands
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import UUID

from surogates.channels.base import MessageEvent, MessageType, SendResult
from surogates.channels.dedup import MessageDeduplicator
from surogates.channels.source import SessionSource, build_session_key

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from surogates.channels.delivery import DeliveryService
    from surogates.config import TelegramSettings
    from surogates.session.store import SessionStore

__all__ = ["TelegramAdapter"]

logger = logging.getLogger(__name__)

try:
    from telegram import Update, Bot, Message
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        MessageHandler as TelegramMessageHandler,
        ContextTypes,
        filters,
    )
    from telegram.constants import ParseMode, ChatType
    from telegram.request import HTTPXRequest
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = Any  # type: ignore[assignment,misc]
    Bot = Any  # type: ignore[assignment,misc]
    Message = Any  # type: ignore[assignment,misc]
    Application = Any  # type: ignore[assignment,misc]
    CallbackQueryHandler = Any  # type: ignore[assignment,misc]
    TelegramMessageHandler = Any  # type: ignore[assignment,misc]
    HTTPXRequest = Any  # type: ignore[assignment,misc]
    filters = None  # type: ignore[assignment]
    ParseMode = None  # type: ignore[assignment]
    ChatType = None  # type: ignore[assignment]

    # Mock ContextTypes so type annotations using ContextTypes.DEFAULT_TYPE
    # don't crash during class definition when the library isn't installed.
    class _MockContextTypes:  # type: ignore[no-redef]
        DEFAULT_TYPE = Any
    ContextTypes = _MockContextTypes  # type: ignore[assignment,misc]


from surogates.channels.media import (
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    SUPPORTED_DOCUMENT_TYPES,
)
from surogates.channels.telegram_format import (
    format_message,
    strip_mdv2,
    truncate_message,
    MAX_MESSAGE_LENGTH,
)
from surogates.channels.telegram_network import (
    TelegramFallbackTransport,
    discover_fallback_ips,
    parse_fallback_ip_env,
)


# Reverse lookup: MIME type -> file extension (for document type detection).
_MIME_TO_EXT: dict[str, str] = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}

# Telegram caption limit (characters).
CAPTION_LIMIT = 1024

# Session map size limit before eviction.
_SESSION_MAP_MAX = 10_000

# Redis work queue key (shared with orchestrator/dispatcher).
WORK_QUEUE_KEY = "surogates:work_queue"

# Telegram error types — resolved once at import time, not per-call.
try:
    from telegram.error import NetworkError as _TelegramNetworkError
except ImportError:
    _TelegramNetworkError = OSError  # type: ignore[assignment,misc]

try:
    from telegram.error import BadRequest as _TelegramBadRequest
except ImportError:
    _TelegramBadRequest = None  # type: ignore[assignment,misc]

try:
    from telegram.error import TimedOut as _TelegramTimedOut
except (ImportError, AttributeError):
    _TelegramTimedOut = None  # type: ignore[assignment,misc]


class TelegramAdapter:
    """Telegram bot adapter.

    Satisfies the :class:`ChannelAdapter` protocol.

    Handles:
    - Receiving messages from users and groups
    - Sending responses with Telegram MarkdownV2
    - Forum topics (thread_id support)
    - Media messages (photos, voice, audio, documents)
    - Text message aggregation (Telegram client-side splits)
    - Media group / album aggregation
    - Group mention gating
    - Inline keyboard callbacks (approval buttons)
    - Message lifecycle reactions
    - Fallback IP transport for restricted networks
    """

    # Threshold for detecting Telegram client-side message splits.
    # When a chunk is near this limit, a continuation is almost certain.
    _SPLIT_THRESHOLD = 4000
    MEDIA_GROUP_WAIT_SECONDS = 0.8

    def __init__(
        self,
        telegram_settings: TelegramSettings,
        delivery_service: DeliveryService,
        session_store: SessionStore,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Redis,
        agent_id: str,
    ) -> None:
        self._settings = telegram_settings
        self._delivery = delivery_service
        self._session_store = session_store
        self._session_factory = session_factory
        self._redis = redis_client
        self._agent_id = agent_id

        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
        self._webhook_mode: bool = False
        self._running: bool = False
        self._delivery_task: Optional[asyncio.Task[None]] = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._dedup = MessageDeduplicator()

        # Mention gating (computed once, not per-message)
        self._mention_patterns = self._compile_mention_patterns()
        self._free_response_chats: set[str] = self._parse_free_response_chats()

        # Buffer rapid/album photo updates so Telegram image bursts are handled
        # as a single MessageEvent instead of self-interrupting multiple turns.
        self._pending_photo_batches: Dict[str, MessageEvent] = {}
        self._pending_photo_batch_tasks: Dict[str, asyncio.Task[None]] = {}
        self._media_group_events: Dict[str, MessageEvent] = {}
        self._media_group_tasks: Dict[str, asyncio.Task[None]] = {}

        # Buffer rapid text messages so Telegram client-side splits of long
        # messages are aggregated into a single MessageEvent.
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task[None]] = {}

        self._polling_error_task: Optional[asyncio.Task[None]] = None
        self._polling_conflict_count: int = 0
        self._polling_network_error_count: int = 0
        self._polling_error_callback_ref: object = None

        # Session key -> session_id cache
        self._session_map: Dict[str, UUID] = {}

        # Approval button state: approval_id -> session_key
        self._approval_state: Dict[int, str] = {}
        self._approval_counter_val: int = 0


    # ------------------------------------------------------------------
    # Polling error handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_polling_conflict(error: Exception) -> bool:
        text = str(error).lower()
        return (
            error.__class__.__name__.lower() == "conflict"
            or "terminated by other getupdates request" in text
            or "another bot instance is running" in text
        )

    @staticmethod
    def _looks_like_network_error(error: Exception) -> bool:
        """Return True for transient network errors that warrant a reconnect attempt."""
        if isinstance(error, _TelegramNetworkError):
            return True
        name = error.__class__.__name__.lower()
        if name in ("networkerror", "timedout", "connectionerror"):
            return True
        return isinstance(error, OSError)

    async def _restart_polling(self) -> None:
        """Stop the updater (if running) and restart polling."""
        try:
            if self._app and self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
        except Exception:
            pass
        await self._app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            error_callback=self._polling_error_callback_ref,
        )

    async def _handle_polling_network_error(self, error: Exception) -> None:
        """Reconnect polling after a transient network interruption.

        Strategy: exponential back-off (5s, 10s, 20s, 40s, 60s cap) up to
        MAX_NETWORK_RETRIES attempts, then log and stop.
        """
        MAX_NETWORK_RETRIES = 10
        BASE_DELAY = 5
        MAX_DELAY = 60

        self._polling_network_error_count += 1
        attempt = self._polling_network_error_count

        if attempt > MAX_NETWORK_RETRIES:
            logger.error(
                "[telegram] Polling could not reconnect after %d network error retries. "
                "Last error: %s",
                MAX_NETWORK_RETRIES, error,
            )
            return

        delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
        logger.warning(
            "[telegram] Network error (attempt %d/%d), reconnecting in %ds. Error: %s",
            attempt, MAX_NETWORK_RETRIES, delay, error,
        )
        await asyncio.sleep(delay)

        try:
            await self._restart_polling()
            logger.info(
                "[telegram] Polling resumed after network error (attempt %d)",
                attempt,
            )
            self._polling_network_error_count = 0
        except Exception as retry_err:
            logger.warning("[telegram] Polling reconnect failed: %s", retry_err)
            # start_polling failed -- polling is dead and no further error
            # callbacks will fire, so schedule the next retry ourselves.
            task = asyncio.ensure_future(
                self._handle_polling_network_error(retry_err)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _handle_polling_conflict(self, error: Exception) -> None:
        """Handle 409 Conflict from concurrent polling instances."""
        self._polling_conflict_count += 1

        MAX_CONFLICT_RETRIES = 3
        RETRY_DELAY = 10  # seconds

        if self._polling_conflict_count <= MAX_CONFLICT_RETRIES:
            logger.warning(
                "[telegram] Polling conflict (%d/%d), will retry in %ds. Error: %s",
                self._polling_conflict_count, MAX_CONFLICT_RETRIES,
                RETRY_DELAY, error,
            )
            await asyncio.sleep(RETRY_DELAY)
            try:
                await self._restart_polling()
                logger.info(
                    "[telegram] Polling resumed after conflict retry %d",
                    self._polling_conflict_count,
                )
                self._polling_conflict_count = 0
                return
            except Exception as retry_err:
                logger.warning("[telegram] Polling retry failed: %s", retry_err)
                return

        # Exhausted retries -- fatal
        logger.error(
            "[telegram] Another process is already polling this bot token. "
            "Stopped after %d retries. Original error: %s",
            MAX_CONFLICT_RETRIES, error,
        )
        try:
            if self._app and self._app.updater:
                await self._app.updater.stop()
        except Exception as stop_error:
            logger.warning(
                "[telegram] Failed stopping polling after conflict: %s",
                stop_error, exc_info=True,
            )

    # ------------------------------------------------------------------
    # connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Telegram via polling or webhook.

        By default, uses long polling (outbound connection to Telegram).
        If ``SUROGATES_TELEGRAM_WEBHOOK_URL`` is set, starts an HTTP webhook
        server instead.
        """
        if not TELEGRAM_AVAILABLE:
            raise RuntimeError(
                "python-telegram-bot not installed. "
                "Run: uv add python-telegram-bot"
            )

        if not self._settings.bot_token:
            raise RuntimeError("No Telegram bot token configured")

        try:
            # Build the application
            builder = Application.builder().token(self._settings.bot_token)
            if self._settings.base_url:
                builder = builder.base_url(self._settings.base_url)
                builder = builder.base_file_url(self._settings.base_url)
                logger.info(
                    "[telegram] Using custom base_url: %s",
                    self._settings.base_url,
                )

            request_kwargs = {
                "connection_pool_size": self._settings.http_pool_size,
                "pool_timeout": self._settings.http_pool_timeout,
                "connect_timeout": self._settings.http_connect_timeout,
                "read_timeout": self._settings.http_read_timeout,
                "write_timeout": self._settings.http_write_timeout,
            }

            proxy_configured = any(
                (os.getenv(k) or "").strip()
                for k in (
                    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                    "https_proxy", "http_proxy", "all_proxy",
                )
            )
            disable_fallback = os.getenv(
                "SUROGATES_TELEGRAM_DISABLE_FALLBACK_IPS", ""
            ).strip().lower() in ("1", "true", "yes", "on")

            fallback_ips = parse_fallback_ip_env(self._settings.fallback_ips or None)
            if not fallback_ips:
                fallback_ips = await discover_fallback_ips()
                logger.info(
                    "[telegram] Auto-discovered fallback IPs: %s",
                    ", ".join(fallback_ips),
                )

            if fallback_ips and not proxy_configured and not disable_fallback:
                logger.info(
                    "[telegram] Fallback IPs active: %s",
                    ", ".join(fallback_ips),
                )
                # Keep request/update pools separate to reduce contention during
                # polling reconnect + bot API bootstrap/delete_webhook calls.
                request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
                )
                get_updates_request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
                )
            else:
                if proxy_configured:
                    logger.info("[telegram] Proxy configured; skipping fallback-IP transport")
                elif disable_fallback:
                    logger.info("[telegram] Fallback-IP transport disabled via env")
                request = HTTPXRequest(**request_kwargs)
                get_updates_request = HTTPXRequest(**request_kwargs)

            builder = builder.request(request).get_updates_request(get_updates_request)
            self._app = builder.build()
            self._bot = self._app.bot

            # Register handlers
            self._app.add_handler(TelegramMessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_text_message,
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.COMMAND,
                self._handle_command,
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.LOCATION | getattr(filters, "VENUE", filters.LOCATION),
                self._handle_location_message,
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.PHOTO | filters.VIDEO | filters.AUDIO
                | filters.VOICE | filters.Document.ALL | filters.Sticker.ALL,
                self._handle_media_message,
            ))
            # Handle inline keyboard button callbacks (approval prompts)
            self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))

            # Start polling -- retry initialize() for transient TLS resets
            _max_connect = 3
            for _attempt in range(_max_connect):
                try:
                    await self._app.initialize()
                    break
                except (_TelegramNetworkError, OSError) as init_err:
                    if _attempt < _max_connect - 1:
                        wait = 2 ** _attempt
                        logger.warning(
                            "[telegram] Connect attempt %d/%d failed: %s -- retrying in %ds",
                            _attempt + 1, _max_connect, init_err, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
            await self._app.start()

            # Decide between webhook and polling mode
            webhook_url = self._settings.webhook_url.strip()

            if webhook_url:
                # -- Webhook mode --
                webhook_port = self._settings.webhook_port
                webhook_secret = self._settings.webhook_secret.strip() or None
                from urllib.parse import urlparse
                webhook_path = urlparse(webhook_url).path or "/telegram"

                await self._app.updater.start_webhook(
                    listen="0.0.0.0",
                    port=webhook_port,
                    url_path=webhook_path,
                    webhook_url=webhook_url,
                    secret_token=webhook_secret,
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                )
                self._webhook_mode = True
                logger.info(
                    "[telegram] Webhook server listening on 0.0.0.0:%d%s",
                    webhook_port, webhook_path,
                )
            else:
                # -- Polling mode (default) --
                # Clear any stale webhook first so polling doesn't inherit a
                # previous webhook registration and silently stop receiving updates.
                delete_webhook = getattr(self._bot, "delete_webhook", None)
                if callable(delete_webhook):
                    await delete_webhook(drop_pending_updates=False)

                loop = asyncio.get_running_loop()

                def _polling_error_callback(error: Exception) -> None:
                    if self._polling_error_task and not self._polling_error_task.done():
                        return
                    if self._looks_like_polling_conflict(error):
                        self._polling_error_task = loop.create_task(
                            self._handle_polling_conflict(error)
                        )
                    elif self._looks_like_network_error(error):
                        logger.warning(
                            "[telegram] Network error, scheduling reconnect: %s", error,
                        )
                        self._polling_error_task = loop.create_task(
                            self._handle_polling_network_error(error)
                        )
                    else:
                        logger.error(
                            "[telegram] Polling error: %s", error, exc_info=True,
                        )

                # Store reference for retry use in _handle_polling_conflict
                self._polling_error_callback_ref = _polling_error_callback

                await self._app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                    error_callback=_polling_error_callback,
                )

            self._running = True
            mode = "webhook" if self._webhook_mode else "polling"
            logger.info("[telegram] Connected (%s mode)", mode)

            # Start the background delivery loop
            self._delivery_task = asyncio.create_task(self._delivery_loop())

        except Exception as e:
            logger.error("[telegram] Failed to connect: %s", e, exc_info=True)
            raise

    async def disconnect(self) -> None:
        """Stop polling/webhook, cancel pending batches, and disconnect."""
        self._running = False

        # Cancel delivery task
        if self._delivery_task and not self._delivery_task.done():
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except asyncio.CancelledError:
                pass

        # Cancel polling error recovery tasks
        if self._polling_error_task and not self._polling_error_task.done():
            self._polling_error_task.cancel()
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

        # Cancel media group tasks
        pending_media_group_tasks = list(self._media_group_tasks.values())
        for task in pending_media_group_tasks:
            task.cancel()
        if pending_media_group_tasks:
            await asyncio.gather(*pending_media_group_tasks, return_exceptions=True)
        self._media_group_tasks.clear()
        self._media_group_events.clear()

        if self._app:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning("[telegram] Error during disconnect: %s", e, exc_info=True)

        for task in self._pending_photo_batch_tasks.values():
            if task and not task.done():
                task.cancel()
        self._pending_photo_batch_tasks.clear()
        self._pending_photo_batches.clear()

        for task in self._pending_text_batch_tasks.values():
            if task and not task.done():
                task.cancel()
        self._pending_text_batch_tasks.clear()
        self._pending_text_batches.clear()

        self._app = None
        self._bot = None
        logger.info("[telegram] Disconnected")

    # ------------------------------------------------------------------
    # send / send_typing
    # ------------------------------------------------------------------

    def _should_thread_reply(self, reply_to: Optional[str], chunk_index: int) -> bool:
        """Determine if this message chunk should thread to the original message."""
        if not reply_to:
            return False
        mode = self._settings.reply_to_mode
        if mode == "off":
            return False
        elif mode == "all":
            return True
        else:  # "first" (default)
            return chunk_index == 0

    async def send(
        self,
        target: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a message to a Telegram chat."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        # Skip whitespace-only text to prevent Telegram 400 empty-text errors.
        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        try:
            # Format and split message if needed
            formatted = format_message(content)
            chunks = truncate_message(formatted, MAX_MESSAGE_LENGTH)
            if len(chunks) > 1:
                # truncate_message appends a raw " (1/2)" suffix. Escape the
                # MarkdownV2-special parentheses so Telegram doesn't reject the
                # chunk and fall back to plain text.
                chunks = [
                    re.sub(r" \((\d+)/(\d+)\)$", r" \\(\1/\2\\)", chunk)
                    for chunk in chunks
                ]

            message_ids: list[str] = []
            thread_id = metadata.get("thread_id") if metadata else None

            for i, chunk in enumerate(chunks):
                should_thread = self._should_thread_reply(reply_to, i)
                reply_to_id = int(reply_to) if should_thread else None
                effective_thread_id = int(thread_id) if thread_id else None

                msg = None
                for _send_attempt in range(3):
                    try:
                        # Try Markdown first, fall back to plain text if it fails
                        try:
                            msg = await self._bot.send_message(
                                chat_id=int(target),
                                text=chunk,
                                parse_mode=ParseMode.MARKDOWN_V2,
                                reply_to_message_id=reply_to_id,
                                message_thread_id=effective_thread_id,
                            )
                        except Exception as md_error:
                            # Markdown parsing failed, try plain text
                            if "parse" in str(md_error).lower() or "markdown" in str(md_error).lower():
                                logger.warning(
                                    "[telegram] MarkdownV2 parse failed, falling back to plain text: %s",
                                    md_error,
                                )
                                plain_chunk = strip_mdv2(chunk)
                                msg = await self._bot.send_message(
                                    chat_id=int(target),
                                    text=plain_chunk,
                                    parse_mode=None,
                                    reply_to_message_id=reply_to_id,
                                    message_thread_id=effective_thread_id,
                                )
                            else:
                                raise
                        break  # success
                    except _TelegramNetworkError as send_err:
                        # BadRequest is a subclass of NetworkError in
                        # python-telegram-bot but represents permanent errors.
                        if _TelegramBadRequest and isinstance(send_err, _TelegramBadRequest):
                            err_lower = str(send_err).lower()
                            if "thread not found" in err_lower and effective_thread_id is not None:
                                logger.warning(
                                    "[telegram] Thread %s not found, retrying without message_thread_id",
                                    effective_thread_id,
                                )
                                effective_thread_id = None
                                continue
                            if "message to be replied not found" in err_lower and reply_to_id is not None:
                                logger.warning(
                                    "[telegram] Reply target deleted, retrying without reply_to: %s",
                                    send_err,
                                )
                                reply_to_id = None
                                continue
                            # Other BadRequest errors are permanent
                            raise
                        # TimedOut indicates the request may have reached the server
                        if _TelegramTimedOut and isinstance(send_err, _TelegramTimedOut):
                            raise
                        if _send_attempt < 2:
                            wait = 2 ** _send_attempt
                            logger.warning(
                                "[telegram] Network error on send (attempt %d/3), retrying in %ds: %s",
                                _send_attempt + 1, wait, send_err,
                            )
                            await asyncio.sleep(wait)
                        else:
                            raise
                    except Exception as send_err:
                        retry_after = getattr(send_err, "retry_after", None)
                        if retry_after is not None or "retry after" in str(send_err).lower():
                            if _send_attempt < 2:
                                wait = float(retry_after) if retry_after is not None else 1.0
                                logger.warning(
                                    "[telegram] Flood control on send (attempt %d/3), "
                                    "retrying in %.1fs: %s",
                                    _send_attempt + 1, wait, send_err,
                                )
                                await asyncio.sleep(wait)
                                continue
                        raise
                message_ids.append(str(msg.message_id))

            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
            )

        except Exception as e:
            logger.error("[telegram] Failed to send message: %s", e, exc_info=True)
            # TimedOut means the request may have reached Telegram --
            # mark as non-retryable so callers don't re-send.
            err_str = str(e).lower()
            is_timeout = (
                (_TelegramTimedOut and isinstance(e, _TelegramTimedOut))
                or "timed out" in err_str
            )
            return SendResult(success=False, error=str(e), retryable=not is_timeout)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> SendResult:
        """Edit a previously sent Telegram message."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        try:
            formatted = format_message(content)
            try:
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=formatted,
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            except Exception as fmt_err:
                # "Message is not modified" is a no-op, not an error
                if "not modified" in str(fmt_err).lower():
                    return SendResult(success=True, message_id=message_id)
                # Fallback: retry without markdown formatting
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=content,
                )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            err_str = str(e).lower()
            # "Message is not modified" -- content identical, treat as success
            if "not modified" in err_str:
                return SendResult(success=True, message_id=message_id)
            # Message too long -- truncate and succeed so the stream consumer can
            # split the overflow into a new message instead of dying.
            if "message_too_long" in err_str or "too long" in err_str:
                truncated = content[: MAX_MESSAGE_LENGTH - 20] + "\u2026"
                try:
                    await self._bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=int(message_id),
                        text=truncated,
                    )
                except Exception:
                    pass  # best-effort truncation
                return SendResult(success=True, message_id=message_id)
            # Flood control / RetryAfter -- short waits are retried inline,
            # long waits return a failure immediately so streaming can fall back
            # to a normal final send instead of leaving a truncated partial.
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None or "retry after" in err_str:
                wait = retry_after if retry_after else 1.0
                logger.warning(
                    "[telegram] Flood control on edit, waiting %.1fs", wait,
                )
                if wait > 5.0:
                    return SendResult(success=False, error=f"flood_control:{wait}")
                await asyncio.sleep(wait)
                try:
                    await self._bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=int(message_id),
                        text=content,
                    )
                    return SendResult(success=True, message_id=message_id)
                except Exception as retry_err:
                    logger.error(
                        "[telegram] Edit retry failed after flood wait: %s",
                        retry_err,
                    )
                    return SendResult(success=False, error=str(retry_err))
            logger.error(
                "[telegram] Failed to edit message %s: %s",
                message_id, e, exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_typing(
        self,
        target: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Send typing indicator."""
        if self._bot:
            try:
                thread_id = metadata.get("thread_id") if metadata else None
                await self._bot.send_chat_action(
                    chat_id=int(target),
                    action="typing",
                    message_thread_id=int(thread_id) if thread_id else None,
                )
            except Exception as e:
                # Typing failures are non-fatal; log at debug level only.
                logger.debug("[telegram] Failed to send typing indicator: %s", e)

    # ------------------------------------------------------------------
    # Outbound media methods
    # ------------------------------------------------------------------

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send audio as a native Telegram voice message or audio file."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(audio_path):
                return SendResult(success=False, error=f"Audio file not found: {audio_path}")

            thread_id = metadata.get("thread_id") if metadata else None
            with open(audio_path, "rb") as audio_file:
                # .ogg files -> send as voice (round playable bubble)
                if audio_path.endswith((".ogg", ".opus")):
                    msg = await self._bot.send_voice(
                        chat_id=int(chat_id),
                        voice=audio_file,
                        caption=caption[:CAPTION_LIMIT] if caption else None,
                        reply_to_message_id=int(reply_to) if reply_to else None,
                        message_thread_id=int(thread_id) if thread_id else None,
                    )
                else:
                    # .mp3 and others -> send as audio file
                    msg = await self._bot.send_audio(
                        chat_id=int(chat_id),
                        audio=audio_file,
                        caption=caption[:CAPTION_LIMIT] if caption else None,
                        reply_to_message_id=int(reply_to) if reply_to else None,
                        message_thread_id=int(thread_id) if thread_id else None,
                    )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[telegram] Failed to send voice/audio: %s", e, exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send an image natively as a Telegram photo.

        Tries URL-based send first (fast, works for <5MB images).
        Falls back to downloading and uploading as file (supports up to 10MB).
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        from surogates.tools.utils.url_safety import is_safe_url
        if not is_safe_url(image_url):
            logger.warning("[telegram] Blocked unsafe image URL (SSRF protection)")
            return SendResult(success=False, error="Blocked unsafe URL")

        try:
            thread_id = metadata.get("thread_id") if metadata else None
            msg = await self._bot.send_photo(
                chat_id=int(chat_id),
                photo=image_url,
                caption=caption[:CAPTION_LIMIT] if caption else None,
                reply_to_message_id=int(reply_to) if reply_to else None,
                message_thread_id=int(thread_id) if thread_id else None,
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning(
                "[telegram] URL-based send_photo failed, trying file upload: %s", e,
            )
            # Fallback: download and upload as file (supports up to 10MB)
            try:
                from surogates.channels.media import _ssrf_redirect_guard
                import httpx
                async with httpx.AsyncClient(
                    timeout=30.0,
                    follow_redirects=True,
                    event_hooks={"response": [_ssrf_redirect_guard]},
                ) as client:
                    resp = await client.get(image_url)
                    resp.raise_for_status()
                    image_data = resp.content

                msg = await self._bot.send_photo(
                    chat_id=int(chat_id),
                    photo=image_data,
                    caption=caption[:CAPTION_LIMIT] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    message_thread_id=int(thread_id) if thread_id else None,
                )
                return SendResult(success=True, message_id=str(msg.message_id))
            except Exception as e2:
                logger.error(
                    "[telegram] File upload send_photo also failed: %s",
                    e2, exc_info=True,
                )
                return SendResult(success=False, error=str(e2))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a local image file natively as a Telegram photo."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(image_path):
                return SendResult(success=False, error=f"Image file not found: {image_path}")

            thread_id = metadata.get("thread_id") if metadata else None
            with open(image_path, "rb") as image_file:
                msg = await self._bot.send_photo(
                    chat_id=int(chat_id),
                    photo=image_file,
                    caption=caption[:CAPTION_LIMIT] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    message_thread_id=int(thread_id) if thread_id else None,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[telegram] Failed to send local image: %s", e, exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        file_name: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a document/file natively as a Telegram file attachment."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(file_path):
                return SendResult(success=False, error=f"File not found: {file_path}")

            display_name = file_name or os.path.basename(file_path)
            thread_id = metadata.get("thread_id") if metadata else None

            with open(file_path, "rb") as f:
                msg = await self._bot.send_document(
                    chat_id=int(chat_id),
                    document=f,
                    filename=display_name,
                    caption=caption[:CAPTION_LIMIT] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    message_thread_id=int(thread_id) if thread_id else None,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[telegram] Failed to send document: %s", e, exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a video natively as a Telegram video message."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(video_path):
                return SendResult(success=False, error=f"Video file not found: {video_path}")

            thread_id = metadata.get("thread_id") if metadata else None
            with open(video_path, "rb") as f:
                msg = await self._bot.send_video(
                    chat_id=int(chat_id),
                    video=f,
                    caption=caption[:CAPTION_LIMIT] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    message_thread_id=int(thread_id) if thread_id else None,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[telegram] Failed to send video: %s", e, exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send an animated GIF natively as a Telegram animation (auto-plays inline)."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            thread_id = metadata.get("thread_id") if metadata else None
            msg = await self._bot.send_animation(
                chat_id=int(chat_id),
                animation=animation_url,
                caption=caption[:CAPTION_LIMIT] if caption else None,
                reply_to_message_id=int(reply_to) if reply_to else None,
                message_thread_id=int(thread_id) if thread_id else None,
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[telegram] Failed to send animation, falling back to photo: %s",
                e, exc_info=True,
            )
            # Fallback: try as a regular photo
            return await self.send_image(
                chat_id, animation_url, caption, reply_to, metadata,
            )

    # ------------------------------------------------------------------
    # Chat info
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Get information about a Telegram chat."""
        if not self._bot:
            return {"name": "Unknown", "type": "dm"}

        try:
            chat = await self._bot.get_chat(int(chat_id))

            chat_type = "dm"
            if chat.type == ChatType.GROUP:
                chat_type = "group"
            elif chat.type == ChatType.SUPERGROUP:
                chat_type = "group"
                if chat.is_forum:
                    chat_type = "forum"
            elif chat.type == ChatType.CHANNEL:
                chat_type = "channel"

            return {
                "name": chat.title or chat.full_name or str(chat_id),
                "type": chat_type,
                "username": chat.username,
                "is_forum": getattr(chat, "is_forum", False),
            }
        except Exception as e:
            logger.error(
                "[telegram] Failed to get chat info for %s: %s",
                chat_id, e, exc_info=True,
            )
            return {"name": str(chat_id), "type": "dm", "error": str(e)}

    # ------------------------------------------------------------------
    # Group mention gating
    # ------------------------------------------------------------------

    def _telegram_require_mention(self) -> bool:
        """Return whether group chats should require an explicit bot trigger."""
        return self._settings.require_mention

    def _parse_free_response_chats(self) -> set[str]:
        """Parse free_response_chats setting once at startup."""
        raw = self._settings.free_response_chats
        if not raw:
            return set()
        return {part.strip() for part in raw.split(",") if part.strip()}

    def _compile_mention_patterns(self) -> List[re.Pattern[str]]:
        """Compile optional regex wake-word patterns for group triggers."""
        raw = self._settings.mention_patterns
        if not raw:
            return []

        try:
            loaded = json.loads(raw)
        except Exception:
            loaded = [part.strip() for part in raw.split(",") if part.strip()]

        if isinstance(loaded, str):
            loaded = [loaded]
        if not isinstance(loaded, list):
            logger.warning(
                "[telegram] mention_patterns must be a list or string; got %s",
                type(loaded).__name__,
            )
            return []

        compiled: List[re.Pattern[str]] = []
        for pattern in loaded:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("[telegram] Invalid mention pattern %r: %s", pattern, exc)
        if compiled:
            logger.info("[telegram] Loaded %d mention pattern(s)", len(compiled))
        return compiled

    def _is_group_chat(self, message: Message) -> bool:
        chat = getattr(message, "chat", None)
        if not chat:
            return False
        chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower()
        return chat_type in ("group", "supergroup")

    def _is_reply_to_bot(self, message: Message) -> bool:
        if not self._bot or not getattr(message, "reply_to_message", None):
            return False
        reply_user = getattr(message.reply_to_message, "from_user", None)
        return bool(
            reply_user
            and getattr(reply_user, "id", None) == getattr(self._bot, "id", None)
        )

    def _message_mentions_bot(self, message: Message) -> bool:
        if not self._bot:
            return False

        bot_username = (getattr(self._bot, "username", None) or "").lstrip("@").lower()
        bot_id = getattr(self._bot, "id", None)

        def _iter_sources():
            yield (
                getattr(message, "text", None) or "",
                getattr(message, "entities", None) or [],
            )
            yield (
                getattr(message, "caption", None) or "",
                getattr(message, "caption_entities", None) or [],
            )

        for source_text, entities in _iter_sources():
            if bot_username and f"@{bot_username}" in source_text.lower():
                return True
            for entity in entities:
                entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
                if entity_type == "mention" and bot_username:
                    offset = int(getattr(entity, "offset", -1))
                    length = int(getattr(entity, "length", 0))
                    if offset < 0 or length <= 0:
                        continue
                    if source_text[offset:offset + length].strip().lower() == f"@{bot_username}":
                        return True
                elif entity_type == "text_mention":
                    user = getattr(entity, "user", None)
                    if user and getattr(user, "id", None) == bot_id:
                        return True
        return False

    def _message_matches_mention_patterns(self, message: Message) -> bool:
        if not self._mention_patterns:
            return False
        for candidate in (getattr(message, "text", None), getattr(message, "caption", None)):
            if not candidate:
                continue
            for pattern in self._mention_patterns:
                if pattern.search(candidate):
                    return True
        return False

    def _clean_bot_trigger_text(self, text: Optional[str]) -> Optional[str]:
        if not text or not self._bot or not getattr(self._bot, "username", None):
            return text
        username = re.escape(self._bot.username)
        cleaned = re.sub(rf"(?i)@{username}\b[,:\-]*\s*", "", text).strip()
        return cleaned or text

    def _should_process_message(self, message: Message, *, is_command: bool = False) -> bool:
        """Apply Telegram group trigger rules.

        DMs remain unrestricted.  Group/supergroup messages are accepted when:
        - the chat is explicitly allowlisted in ``free_response_chats``
        - ``require_mention`` is disabled
        - the message is a command
        - the message replies to the bot
        - the bot is @mentioned
        - the text/caption matches a configured regex wake-word pattern
        """
        if not self._is_group_chat(message):
            return True
        if str(getattr(getattr(message, "chat", None), "id", "")) in self._free_response_chats:
            return True
        if not self._telegram_require_mention():
            return True
        if is_command:
            return True
        if self._is_reply_to_bot(message):
            return True
        if self._message_mentions_bot(message):
            return True
        return self._message_matches_mention_patterns(message)

    # ------------------------------------------------------------------
    # Message event construction
    # ------------------------------------------------------------------

    def _build_source(self, message: Message) -> SessionSource:
        """Build a SessionSource from a Telegram message."""
        chat = message.chat
        user = message.from_user

        # Determine chat type
        chat_type = "dm"
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            chat_type = "group"
        elif chat.type == ChatType.CHANNEL:
            chat_type = "channel"

        thread_id_raw = message.message_thread_id
        thread_id_str = str(thread_id_raw) if thread_id_raw else None

        return SessionSource(
            platform="telegram",
            chat_id=str(chat.id),
            chat_name=chat.title or (chat.full_name if hasattr(chat, "full_name") else None),
            chat_type=chat_type,
            user_id=str(user.id) if user else "0",
            user_name=user.full_name if user else None,
            thread_id=thread_id_str,
        )

    def _build_message_event(self, message: Message, msg_type: MessageType) -> MessageEvent:
        """Build a MessageEvent from a Telegram message."""
        source = self._build_source(message)

        # Extract reply context if this message is a reply
        reply_to_id = None
        if message.reply_to_message:
            reply_to_id = str(message.reply_to_message.message_id)

        return MessageEvent(
            source=source,
            content=message.text or "",
            message_type=msg_type,
            reply_to_message_id=reply_to_id,
            timestamp=message.date,
            raw_payload={"message_id": str(message.message_id)},
        )

    # ------------------------------------------------------------------
    # Inbound message handlers
    # ------------------------------------------------------------------

    async def _handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming text messages.

        Telegram clients split long messages into multiple updates.  Buffer
        rapid successive text messages from the same user/chat and aggregate
        them into a single MessageEvent before dispatching.
        """
        if not update.message or not update.message.text:
            return
        if not self._should_process_message(update.message):
            return

        event = self._build_message_event(update.message, MessageType.TEXT)
        event.content = self._clean_bot_trigger_text(event.content) or ""
        self._enqueue_text_event(event)

    async def _handle_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming command messages."""
        if not update.message or not update.message.text:
            return
        if not self._should_process_message(update.message, is_command=True):
            return

        event = self._build_message_event(update.message, MessageType.COMMAND)
        await self._dispatch_event(event)

    async def _handle_location_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming location/venue pin messages."""
        if not update.message:
            return
        if not self._should_process_message(update.message):
            return

        msg = update.message
        venue = getattr(msg, "venue", None)
        location = getattr(venue, "location", None) if venue else getattr(msg, "location", None)

        if not location:
            return

        lat = getattr(location, "latitude", None)
        lon = getattr(location, "longitude", None)
        if lat is None or lon is None:
            return

        # Build a text message with coordinates and context
        parts = ["[The user shared a location pin.]"]
        if venue:
            title = getattr(venue, "title", None)
            address = getattr(venue, "address", None)
            if title:
                parts.append(f"Venue: {title}")
            if address:
                parts.append(f"Address: {address}")
        parts.append(f"latitude: {lat}")
        parts.append(f"longitude: {lon}")
        parts.append(f"Map: https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        parts.append("Ask what they'd like to find nearby (restaurants, cafes, etc.) and any preferences.")

        event = self._build_message_event(msg, MessageType.TEXT)
        event.content = "\n".join(parts)
        await self._dispatch_event(event)

    # ------------------------------------------------------------------
    # Text message aggregation (handles Telegram client-side splits)
    # ------------------------------------------------------------------

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching."""
        return build_session_key(
            event.source,
            per_user_groups=self._settings.per_user_groups,
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer.

        When Telegram splits a long user message into multiple updates,
        they arrive within a few hundred milliseconds.  This method
        concatenates them and waits for a short quiet period before
        dispatching the combined message.
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.content or "")
        if existing is None:
            self._pending_text_batches[key] = event
            event.raw_payload["_last_chunk_len"] = chunk_len
        else:
            # Append text from the follow-up chunk
            if event.content:
                existing.content = (
                    f"{existing.content}\n{event.content}" if existing.content else event.content
                )
            existing.raw_payload["_last_chunk_len"] = chunk_len
            # Merge any media that might be attached
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        # Cancel any pending flush and restart the timer
        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text.

        Uses a longer delay when the latest chunk is near Telegram's 4096-char
        split point, since a continuation chunk is almost certain.
        """
        current_task = asyncio.current_task()
        try:
            # Adaptive delay: if the latest chunk is near Telegram's 4096-char
            # split point, a continuation is almost certain -- wait longer.
            pending = self._pending_text_batches.get(key)
            last_len = pending.raw_payload.get("_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._settings.text_batch_split_delay
            else:
                delay = self._settings.text_batch_delay
            await asyncio.sleep(delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[telegram] Flushing text batch %s (%d chars)",
                key, len(event.content or ""),
            )
            await self._dispatch_event(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    # ------------------------------------------------------------------
    # Photo batching
    # ------------------------------------------------------------------

    def _photo_batch_key(self, event: MessageEvent, msg: Message) -> str:
        """Return a batching key for Telegram photos/albums."""
        session_key = build_session_key(
            event.source,
            per_user_groups=self._settings.per_user_groups,
        )
        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            return f"{session_key}:album:{media_group_id}"
        return f"{session_key}:photo-burst"

    async def _flush_photo_batch(self, batch_key: str) -> None:
        """Send a buffered photo burst/album as a single MessageEvent."""
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._settings.media_batch_delay)
            event = self._pending_photo_batches.pop(batch_key, None)
            if not event:
                return
            logger.info(
                "[telegram] Flushing photo batch %s with %d image(s)",
                batch_key, len(event.media_urls),
            )
            await self._dispatch_event(event)
        finally:
            if self._pending_photo_batch_tasks.get(batch_key) is current_task:
                self._pending_photo_batch_tasks.pop(batch_key, None)

    def _enqueue_photo_event(self, batch_key: str, event: MessageEvent) -> None:
        """Merge photo events into a pending batch and schedule flush."""
        existing = self._pending_photo_batches.get(batch_key)
        if existing is None:
            self._pending_photo_batches[batch_key] = event
        else:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.content:
                existing.content = self._merge_caption(existing.content, event.content)

        prior_task = self._pending_photo_batch_tasks.get(batch_key)
        if prior_task and not prior_task.done():
            prior_task.cancel()

        self._pending_photo_batch_tasks[batch_key] = asyncio.create_task(
            self._flush_photo_batch(batch_key)
        )

    @staticmethod
    def _merge_caption(existing: str, new: str) -> str:
        """Merge two captions, avoiding duplicates."""
        if not existing:
            return new
        if not new or new == existing:
            return existing
        return f"{existing}\n{new}"

    # ------------------------------------------------------------------
    # Media group (album) aggregation
    # ------------------------------------------------------------------

    async def _queue_media_group_event(self, media_group_id: str, event: MessageEvent) -> None:
        """Buffer Telegram media-group items so albums arrive as one logical event.

        Telegram delivers albums as multiple updates with a shared media_group_id.
        If we forward each item immediately, the gateway thinks the second image is a
        new user message and interrupts the first.  We debounce briefly and merge the
        attachments into a single MessageEvent.
        """
        existing = self._media_group_events.get(media_group_id)
        if existing is None:
            self._media_group_events[media_group_id] = event
        else:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.content:
                existing.content = self._merge_caption(existing.content, event.content)

        prior_task = self._media_group_tasks.get(media_group_id)
        if prior_task:
            prior_task.cancel()

        self._media_group_tasks[media_group_id] = asyncio.create_task(
            self._flush_media_group_event(media_group_id)
        )

    async def _flush_media_group_event(self, media_group_id: str) -> None:
        try:
            await asyncio.sleep(self.MEDIA_GROUP_WAIT_SECONDS)
            event = self._media_group_events.pop(media_group_id, None)
            if event is not None:
                await self._dispatch_event(event)
        except asyncio.CancelledError:
            return
        finally:
            self._media_group_tasks.pop(media_group_id, None)

    # ------------------------------------------------------------------
    # Media message handler
    # ------------------------------------------------------------------

    async def _handle_media_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming media messages, downloading files to local cache."""
        if not update.message:
            return
        if not self._should_process_message(update.message):
            return

        msg = update.message

        # Determine media type
        if msg.sticker:
            # Sticker analysis requires vision tooling not yet ported.
            # Inject a text description and dispatch.
            event = self._build_message_event(msg, MessageType.TEXT)
            emoji = getattr(msg.sticker, "emoji", "") or ""
            set_name = getattr(msg.sticker, "set_name", "") or ""
            if msg.sticker.is_animated or msg.sticker.is_video:
                event.content = f"[The user sent an animated sticker: {emoji}]"
            else:
                event.content = (
                    f"[The user sent a sticker: {emoji}]"
                    + (f" from set '{set_name}'" if set_name else "")
                )
            await self._dispatch_event(event)
            return

        if msg.photo:
            msg_type = MessageType.IMAGE
        elif msg.video:
            msg_type = MessageType.IMAGE  # treat video as rich media
        elif msg.voice or msg.audio:
            msg_type = MessageType.AUDIO
        elif msg.document:
            msg_type = MessageType.DOCUMENT
        else:
            msg_type = MessageType.DOCUMENT

        event = self._build_message_event(msg, msg_type)

        # Add caption as text
        if msg.caption:
            event.content = self._clean_bot_trigger_text(msg.caption) or ""

        # Download video to local cache
        if msg.video:
            try:
                file_obj = await msg.video.get_file()
                video_bytes = await file_obj.download_as_bytearray()
                ext = ".mp4"
                if file_obj.file_path:
                    for candidate_ext in [".mp4", ".mov", ".avi", ".mkv"]:
                        if file_obj.file_path.lower().endswith(candidate_ext):
                            ext = candidate_ext
                            break
                cached_path = cache_document_from_bytes(
                    bytes(video_bytes), f"video{ext}"
                )
                event.media_urls = [cached_path]
                event.media_types = [f"video/{ext.lstrip('.')}"]
                logger.info("[telegram] Cached user video at %s", cached_path)
            except Exception as e:
                logger.warning("[telegram] Failed to cache video: %s", e, exc_info=True)
                event.content = event.content or "[The user sent a video that could not be downloaded]"

            media_group_id = getattr(msg, "media_group_id", None)
            if media_group_id:
                await self._queue_media_group_event(str(media_group_id), event)
                return
            await self._dispatch_event(event)
            return

        # Download photo to local image cache so the vision tool can access it
        # even after Telegram's ephemeral file URLs expire (~1 hour).
        if msg.photo:
            try:
                # msg.photo is a list of PhotoSize sorted by size; take the largest
                photo = msg.photo[-1]
                file_obj = await photo.get_file()
                # Download the image bytes directly into memory
                image_bytes = await file_obj.download_as_bytearray()
                # Determine extension from the file path if available
                ext = ".jpg"
                if file_obj.file_path:
                    for candidate in [".png", ".webp", ".gif", ".jpeg", ".jpg"]:
                        if file_obj.file_path.lower().endswith(candidate):
                            ext = candidate
                            break
                # Save to local cache
                cached_path = cache_image_from_bytes(bytes(image_bytes), ext=ext)
                event.media_urls = [cached_path]
                event.media_types = [f"image/{ext.lstrip('.')}"]
                logger.info("[telegram] Cached user photo at %s", cached_path)
                media_group_id = getattr(msg, "media_group_id", None)
                if media_group_id:
                    await self._queue_media_group_event(str(media_group_id), event)
                else:
                    batch_key = self._photo_batch_key(event, msg)
                    self._enqueue_photo_event(batch_key, event)
                return

            except Exception as e:
                logger.warning("[telegram] Failed to cache photo: %s", e, exc_info=True)

        # Download voice/audio messages to cache for STT transcription
        if msg.voice:
            try:
                file_obj = await msg.voice.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = cache_audio_from_bytes(bytes(audio_bytes), ext=".ogg")
                event.media_urls = [cached_path]
                event.media_types = ["audio/ogg"]
                logger.info("[telegram] Cached user voice at %s", cached_path)
            except Exception as e:
                logger.warning("[telegram] Failed to cache voice: %s", e, exc_info=True)
        elif msg.audio:
            try:
                file_obj = await msg.audio.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = cache_audio_from_bytes(bytes(audio_bytes), ext=".mp3")
                event.media_urls = [cached_path]
                event.media_types = ["audio/mp3"]
                logger.info("[telegram] Cached user audio at %s", cached_path)
            except Exception as e:
                logger.warning("[telegram] Failed to cache audio: %s", e, exc_info=True)

        # Download document files to cache
        elif msg.document:
            doc = msg.document
            try:
                # Determine file extension
                ext = ""
                original_filename = doc.file_name or ""
                if original_filename:
                    _, ext = os.path.splitext(original_filename)
                    ext = ext.lower()

                # If no extension from filename, reverse-lookup from MIME type
                if not ext and doc.mime_type:
                    ext = _MIME_TO_EXT.get(doc.mime_type, "")

                # Check if supported
                if ext not in SUPPORTED_DOCUMENT_TYPES:
                    supported_list = ", ".join(sorted(SUPPORTED_DOCUMENT_TYPES.keys()))
                    event.content = (
                        f"Unsupported document type '{ext or 'unknown'}'. "
                        f"Supported types: {supported_list}"
                    )
                    logger.info("[telegram] Unsupported document type: %s", ext or "unknown")
                    await self._dispatch_event(event)
                    return

                # Check file size (Telegram Bot API limit: 20 MB)
                MAX_DOC_BYTES = 20 * 1024 * 1024
                if not doc.file_size or doc.file_size > MAX_DOC_BYTES:
                    event.content = (
                        "The document is too large or its size could not be verified. "
                        "Maximum: 20 MB."
                    )
                    logger.info("[telegram] Document too large: %s bytes", doc.file_size)
                    await self._dispatch_event(event)
                    return

                # Download and cache
                file_obj = await doc.get_file()
                doc_bytes = await file_obj.download_as_bytearray()
                raw_bytes = bytes(doc_bytes)
                cached_path = cache_document_from_bytes(
                    raw_bytes, original_filename or f"document{ext}"
                )
                mime_type = SUPPORTED_DOCUMENT_TYPES[ext]
                event.media_urls = [cached_path]
                event.media_types = [mime_type]
                logger.info("[telegram] Cached user document at %s", cached_path)

                # For text files, inject content into event.content (capped at 100 KB)
                MAX_TEXT_INJECT_BYTES = 100 * 1024
                if ext in (".md", ".txt") and len(raw_bytes) <= MAX_TEXT_INJECT_BYTES:
                    try:
                        text_content = raw_bytes.decode("utf-8")
                        display_name = original_filename or f"document{ext}"
                        display_name = re.sub(r'[^\w.\- ]', '_', display_name)
                        injection = f"[Content of {display_name}]:\n{text_content}"
                        if event.content:
                            event.content = f"{injection}\n\n{event.content}"
                        else:
                            event.content = injection
                    except UnicodeDecodeError:
                        logger.warning(
                            "[telegram] Could not decode text file as UTF-8, "
                            "skipping content injection",
                            exc_info=True,
                        )

            except Exception as e:
                logger.warning("[telegram] Failed to cache document: %s", e, exc_info=True)

        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            await self._queue_media_group_event(str(media_group_id), event)
            return

        await self._dispatch_event(event)

    # ------------------------------------------------------------------
    # Callback query handler (approval buttons)
    # ------------------------------------------------------------------

    async def _handle_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle inline keyboard button clicks."""
        query = update.callback_query
        if not query or not query.data:
            return
        data = query.data

        # --- Exec approval callbacks (ea:choice:id) ---
        if data.startswith("ea:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                choice = parts[1]  # once, session, always, deny
                try:
                    approval_id = int(parts[2])
                except (ValueError, IndexError):
                    await query.answer(text="Invalid approval data.")
                    return

                session_key = self._approval_state.pop(approval_id, None)
                if not session_key:
                    await query.answer(text="This approval has already been resolved.")
                    return

                # Map choice to human-readable label
                label_map = {
                    "once": "Approved once",
                    "session": "Approved for session",
                    "always": "Approved permanently",
                    "deny": "Denied",
                }
                user_display = getattr(query.from_user, "first_name", "User")
                label = label_map.get(choice, "Resolved")

                await query.answer(text=label)

                # Edit message to show decision, remove buttons
                try:
                    await query.edit_message_text(
                        text=f"{label} by {user_display}",
                        parse_mode=None,
                        reply_markup=None,
                    )
                except Exception:
                    pass  # non-fatal if edit fails

                logger.info(
                    "[telegram] Approval resolved for session %s (choice=%s, user=%s)",
                    session_key, choice, user_display,
                )
            return

        # Unrecognized callback -- acknowledge to prevent spinner
        await query.answer()

    # ------------------------------------------------------------------
    # Message lifecycle reactions
    # ------------------------------------------------------------------

    def _reactions_enabled(self) -> bool:
        """Check if message reactions are enabled."""
        return self._settings.reactions_enabled

    async def _set_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Set a single emoji reaction on a Telegram message."""
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=int(chat_id),
                message_id=int(message_id),
                reaction=emoji,
            )
            return True
        except Exception as e:
            logger.debug("[telegram] set_message_reaction failed (%s): %s", emoji, e)
            return False

    # ------------------------------------------------------------------
    # Event dispatch + identity resolution
    # ------------------------------------------------------------------

    async def _dispatch_event(self, event: MessageEvent) -> None:
        """Resolve identity, get or create session, emit event, enqueue to worker."""
        from surogates.channels.identity import (
            get_or_create_channel_session,
            resolve_identity,
        )
        from surogates.session.events import EventType

        source = event.source
        message_id = event.raw_payload.get("message_id", "")

        # Dedup
        if message_id and self._dedup.is_duplicate(message_id):
            logger.debug("[telegram] Duplicate message %s, skipping", message_id)
            return

        # Resolve platform identity -> Surogates user
        identity = await resolve_identity(
            self._session_factory,
            platform="telegram",
            platform_user_id=source.user_id,
        )
        if identity is None:
            logger.info(
                "[telegram] Unknown user %s (%s) -- ignoring message",
                source.user_id, source.user_name,
            )
            return

        # Build session key and get or create session
        session_key = build_session_key(
            source,
            per_user_groups=self._settings.per_user_groups,
        )

        # Check session cache first
        session_id = self._session_map.get(session_key)
        if session_id is None:
            session_id = await get_or_create_channel_session(
                self._session_store,
                self._redis,
                session_key=session_key,
                user_id=identity.user_id,
                org_id=identity.org_id,
                agent_id=self._agent_id,
                channel="telegram",
                config={
                    "chat_id": source.chat_id,
                    "chat_type": source.chat_type,
                    "thread_id": source.thread_id,
                    "user_name": source.user_name,
                },
                session_factory=self._session_factory,
            )
            # Cache (with size limit -- evict older half when full)
            if len(self._session_map) > _SESSION_MAP_MAX:
                keys = list(self._session_map.keys())
                for k in keys[: len(keys) // 2]:
                    self._session_map.pop(k, None)
            self._session_map[session_key] = session_id

        # Add reaction on processing start
        if self._reactions_enabled() and message_id:
            await self._set_reaction(source.chat_id, message_id, "\U0001f440")

        # Emit the user message event
        await self._session_store.emit_event(
            session_id=session_id,
            event_type=EventType.USER_MESSAGE,
            data={
                "content": event.content,
                "message_type": event.message_type.value,
                "media_urls": event.media_urls,
                "media_types": event.media_types,
                "reply_to_message_id": event.reply_to_message_id,
                "platform_message_id": message_id,
                "source": {
                    "platform": source.platform,
                    "chat_id": source.chat_id,
                    "chat_type": source.chat_type,
                    "user_id": source.user_id,
                    "user_name": source.user_name,
                    "thread_id": source.thread_id,
                },
            },
        )

        # Enqueue to Redis work queue so the worker picks it up
        await self._redis.zadd(WORK_QUEUE_KEY, {str(session_id): 0})

        logger.info(
            "[telegram] Message from %s (%s) -> session %s",
            source.user_name or source.user_id,
            source.chat_type,
            session_id,
        )

    # ------------------------------------------------------------------
    # Background delivery loop
    # ------------------------------------------------------------------

    async def _delivery_loop(self) -> None:
        """Background loop that claims outbox items and delivers via Telegram Bot API."""
        worker_id = f"telegram-{os.getpid()}"
        while self._running:
            try:
                batch = await self._delivery.claim_batch("telegram", worker_id, limit=20)
                if not batch:
                    await asyncio.sleep(2.0)
                    continue

                for item in batch:
                    try:
                        dest = item.destination
                        chat_id = dest.get("chat_id", "")
                        thread_id = dest.get("thread_id")
                        reply_to = dest.get("reply_to_message_id")
                        meta = {"thread_id": thread_id} if thread_id else None

                        payload = item.payload
                        content = payload.get("content", "")
                        media_type = payload.get("media_type")
                        media_path = payload.get("media_path")
                        media_url = payload.get("media_url")
                        caption = payload.get("caption")

                        # Route to specialised send methods based on payload type
                        result: SendResult
                        if media_type == "voice" and media_path:
                            result = await self.send_voice(
                                chat_id, media_path, caption, reply_to, meta,
                            )
                        elif media_type == "image" and media_path:
                            result = await self.send_image_file(
                                chat_id, media_path, caption, reply_to, meta,
                            )
                        elif media_type == "image" and media_url:
                            result = await self.send_image(
                                chat_id, media_url, caption, reply_to, meta,
                            )
                        elif media_type == "video" and media_path:
                            result = await self.send_video(
                                chat_id, media_path, caption, reply_to, meta,
                            )
                        elif media_type == "document" and media_path:
                            result = await self.send_document(
                                chat_id, media_path, caption,
                                payload.get("file_name"), reply_to, meta,
                            )
                        elif media_type == "animation" and media_url:
                            result = await self.send_animation(
                                chat_id, media_url, caption, reply_to, meta,
                            )
                        elif content:
                            result = await self.send(
                                target=chat_id,
                                content=content,
                                reply_to=reply_to,
                                metadata=meta,
                            )
                        else:
                            # Nothing to send -- mark delivered and skip
                            await self._delivery.mark_delivered(item.id)
                            continue

                        if result.success:
                            await self._delivery.mark_delivered(
                                item.id, provider_message_id=result.message_id,
                            )
                            # Set success reaction on the original user message
                            if self._reactions_enabled():
                                orig_msg_id = dest.get("original_message_id")
                                if orig_msg_id and chat_id:
                                    await self._set_reaction(
                                        chat_id, orig_msg_id, "\U0001f44d",
                                    )
                        else:
                            await self._delivery.mark_failed(
                                item.id, error=result.error or "unknown",
                            )

                    except Exception as item_err:
                        logger.error(
                            "[telegram] Delivery failed for outbox %d: %s",
                            item.id, item_err, exc_info=True,
                        )
                        try:
                            await self._delivery.mark_failed(item.id, error=str(item_err))
                        except Exception:
                            pass

            except asyncio.CancelledError:
                return
            except Exception as loop_err:
                logger.error(
                    "[telegram] Delivery loop error: %s", loop_err, exc_info=True,
                )
                await asyncio.sleep(5.0)
