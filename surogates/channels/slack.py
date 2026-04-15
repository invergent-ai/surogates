"""Slack channel adapter — Socket Mode inbound + outbox delivery outbound.

Features:
- Socket Mode connection with multi-workspace support
- DM and channel messages (mention-gated in channels)
- Thread support with context fetching and caching
- File/image/audio/document attachments
- Slash commands (/surogates)
- Approval buttons (Block Kit) with double-click prevention
- Reaction feedback (eyes → checkmark)
- Bot message filtering (configurable: none/mentions/all)
- Message deduplication for Socket Mode reconnects
- Markdown → Slack mrkdwn conversion
- Assistant thread lifecycle events

Requires (via ``SlackSettings``):
- ``bot_token`` (xoxb-...) — comma-separated for multi-workspace
- ``app_token`` (xapp-...) — Socket Mode token

Also uses ``APISettings.web_url`` for pairing links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

try:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_sdk.web.async_client import AsyncWebClient
    SLACK_AVAILABLE = True
except ImportError:
    SLACK_AVAILABLE = False
    AsyncApp = Any  # type: ignore[misc,assignment]
    AsyncSocketModeHandler = Any  # type: ignore[misc,assignment]
    AsyncWebClient = Any  # type: ignore[misc,assignment]

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.channels.base import MessageEvent, MessageType, SendResult
from surogates.channels.dedup import MessageDeduplicator
from surogates.channels.delivery import DeliveryService
from surogates.channels.identity import get_or_create_channel_session, resolve_identity
from surogates.channels.pairing import PairingStore
from surogates.channels.media import (
    SUPPORTED_DOCUMENT_TYPES,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    safe_url_for_log,
)
from surogates.channels.slack_format import markdown_to_mrkdwn, truncate_message
from surogates.channels.source import SessionSource, build_session_key
from surogates.config import APISettings, SlackSettings
from surogates.session.events import EventType
from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)


@dataclass
class _ThreadContextCache:
    """Cache entry for fetched thread context."""
    content: str
    fetched_at: float = field(default_factory=time.monotonic)
    message_count: int = 0


class SlackAdapter:
    """Slack bot adapter using Socket Mode.

    Requires two tokens:
    - ``SUROGATES_SLACK_BOT_TOKEN`` (xoxb-...) for API calls
    - ``SUROGATES_SLACK_APP_TOKEN`` (xapp-...) for Socket Mode connection

    Configuration is provided via typed pydantic settings:
    - ``SlackSettings`` for Slack-specific options (tokens, mention gating, etc.)
    - ``APISettings`` for platform-wide values like ``web_url``.
    """

    MAX_MESSAGE_LENGTH = 39000  # Slack API allows 40,000 chars; leave margin

    def __init__(
        self,
        slack_settings: SlackSettings,
        api_settings: APISettings,
        delivery_service: DeliveryService,
        session_store: SessionStore,
        session_factory: async_sessionmaker[AsyncSession],
        redis_client: Redis,
        agent_id: str,
    ) -> None:
        self._slack_settings = slack_settings
        self._api_settings = api_settings
        self._delivery = delivery_service
        self._session_store = session_store
        self._sf = session_factory
        self._redis = redis_client
        self._agent_id = agent_id
        self._pairing = PairingStore(redis=redis_client)

        # Slack SDK objects.
        self._app: AsyncApp | None = None
        self._handler: AsyncSocketModeHandler | None = None
        self._bot_user_id: str | None = None
        self._user_name_cache: Dict[str, str] = {}

        # Multi-workspace support.
        self._team_clients: Dict[str, AsyncWebClient] = {}
        self._team_bot_user_ids: Dict[str, str] = {}
        self._channel_team: Dict[str, str] = {}

        # Dedup: prevents duplicate processing on Socket Mode reconnects.
        self._dedup = MessageDeduplicator()

        # Approval tracking.
        self._approval_resolved: Dict[str, bool] = {}

        # Bot message tracking — respond to thread replies without @mention.
        self._bot_message_ts: set[str] = set()
        self._BOT_TS_MAX = 5000

        # Threads where bot was @mentioned — auto-respond to all subsequent.
        self._mentioned_threads: set[str] = set()
        self._MENTIONED_THREADS_MAX = 5000

        # Assistant thread metadata.
        self._assistant_threads: Dict[Tuple[str, str], Dict[str, str]] = {}
        self._ASSISTANT_THREADS_MAX = 5000

        # Thread context cache.
        self._thread_context_cache: Dict[str, _ThreadContextCache] = {}
        self._THREAD_CACHE_TTL = 60.0

        # Background delivery task.
        self._delivery_task: asyncio.Task | None = None

        # Session key → session_id cache (lost on restart, rebuilt on @mention).
        self._session_map: Dict[str, UUID] = {}
        self._SESSION_MAP_MAX = 10000
        self._USER_NAME_CACHE_MAX = 5000

        self._running = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to Slack via Socket Mode."""
        if not SLACK_AVAILABLE:
            raise RuntimeError(
                "slack-bolt not installed. Run: pip install slack-bolt"
            )

        bot_token = self._slack_settings.bot_token
        app_token = self._slack_settings.app_token

        if not bot_token:
            raise RuntimeError("SUROGATES_SLACK_BOT_TOKEN not set")
        if not app_token:
            raise RuntimeError("SUROGATES_SLACK_APP_TOKEN not set")

        # Support comma-separated bot tokens for multi-workspace.
        bot_tokens = [t.strip() for t in bot_token.split(",") if t.strip()]

        # First token is the primary — used for AsyncApp / Socket Mode.
        primary_token = bot_tokens[0]
        self._app = AsyncApp(token=primary_token)

        # Register each bot token and map team_id → client.
        for token in bot_tokens:
            client = AsyncWebClient(token=token)
            auth_response = await client.auth_test()
            team_id = auth_response.get("team_id", "")
            bot_user_id = auth_response.get("user_id", "")
            bot_name = auth_response.get("user", "unknown")
            team_name = auth_response.get("team", "unknown")

            self._team_clients[team_id] = client
            self._team_bot_user_ids[team_id] = bot_user_id

            if self._bot_user_id is None:
                self._bot_user_id = bot_user_id

            logger.info(
                "[Slack] Authenticated as @%s in workspace %s (team: %s)",
                bot_name, team_name, team_id,
            )

        # Register event handlers.
        @self._app.event("message")
        async def handle_message_event(event, say):
            await self._handle_slack_message(event)

        @self._app.event("app_mention")
        async def handle_app_mention(event, say):
            pass  # No-op — message handler already processes @mentions.

        @self._app.event("assistant_thread_started")
        async def handle_assistant_thread_started(event, say):
            await self._handle_assistant_thread_lifecycle_event(event)

        @self._app.event("assistant_thread_context_changed")
        async def handle_assistant_thread_context_changed(event, say):
            await self._handle_assistant_thread_lifecycle_event(event)

        @self._app.command("/surogates")
        async def handle_slash_command(ack, command):
            await ack()
            await self._handle_slash_command(command)

        # Block Kit action handlers for approval buttons.
        for _action_id in (
            "surogates_approve_once",
            "surogates_approve_session",
            "surogates_approve_always",
            "surogates_deny",
        ):
            self._app.action(_action_id)(self._handle_approval_action)

        # Start Socket Mode handler in background.
        self._handler = AsyncSocketModeHandler(self._app, app_token)
        asyncio.create_task(self._handler.start_async())

        # Start background delivery loop.
        self._delivery_task = asyncio.create_task(self._delivery_loop())

        self._running = True
        logger.info(
            "[Slack] Socket Mode connected (%d workspace(s))",
            len(self._team_clients),
        )

    async def disconnect(self) -> None:
        """Disconnect from Slack."""
        self._running = False
        if self._delivery_task:
            self._delivery_task.cancel()
            try:
                await self._delivery_task
            except asyncio.CancelledError:
                pass
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception:
                pass
        logger.info("[Slack] Disconnected")

    # ------------------------------------------------------------------
    # Client routing (multi-workspace)
    # ------------------------------------------------------------------

    def _get_client(self, chat_id: str) -> AsyncWebClient:
        """Return the correct WebClient for a channel's workspace."""
        team_id = self._channel_team.get(chat_id, "")
        if team_id and team_id in self._team_clients:
            return self._team_clients[team_id]
        if self._team_clients:
            return next(iter(self._team_clients.values()))
        raise RuntimeError("No Slack clients registered")

    # ------------------------------------------------------------------
    # Sending messages
    # ------------------------------------------------------------------

    async def send(
        self,
        target: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a message to a Slack channel or thread."""
        formatted = markdown_to_mrkdwn(content)
        chunks = truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

        thread_ts = reply_to
        if metadata and not thread_ts:
            thread_ts = metadata.get("thread_ts")

        result: dict[str, Any] = {}
        for i, chunk in enumerate(chunks):
            kwargs: dict[str, Any] = {
                "channel": target,
                "text": chunk,
                "mrkdwn": True,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
                reply_broadcast = self._slack_settings.reply_broadcast
                if reply_broadcast and i == 0:
                    kwargs["reply_broadcast"] = True

            try:
                result = await self._get_client(target).chat_postMessage(**kwargs)
            except Exception as exc:
                logger.error("[Slack] Failed to send message: %s", exc)
                return SendResult(success=False, error=str(exc))

        sent_ts = result.get("ts") if result else None
        if sent_ts:
            self._bot_message_ts.add(sent_ts)
            if thread_ts:
                self._bot_message_ts.add(thread_ts)
            if len(self._bot_message_ts) > self._BOT_TS_MAX:
                self._bot_message_ts = set(
                    sorted(self._bot_message_ts, reverse=True)[:self._BOT_TS_MAX // 2]
                )

        return SendResult(success=True, message_id=sent_ts)

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def _add_reaction(self, channel: str, timestamp: str, emoji: str) -> bool:
        """Add a reaction emoji to a message."""
        try:
            await self._get_client(channel).reactions_add(
                channel=channel, timestamp=timestamp, name=emoji,
            )
            return True
        except Exception:
            return False

    async def _remove_reaction(self, channel: str, timestamp: str, emoji: str) -> bool:
        """Remove a reaction emoji from a message."""
        try:
            await self._get_client(channel).reactions_remove(
                channel=channel, timestamp=timestamp, name=emoji,
            )
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # User name resolution
    # ------------------------------------------------------------------

    async def _resolve_user_name(self, user_id: str, chat_id: str = "") -> str:
        """Resolve a Slack user ID to a display name, with caching."""
        if user_id in self._user_name_cache:
            return self._user_name_cache[user_id]

        try:
            client = self._get_client(chat_id) if chat_id else next(iter(self._team_clients.values()))
            info = await client.users_info(user=user_id)
            profile = info.get("user", {}).get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or info.get("user", {}).get("name", user_id)
            )
            self._user_name_cache[user_id] = name
            if len(self._user_name_cache) > self._USER_NAME_CACHE_MAX:
                keys = list(self._user_name_cache.keys())
                for k in keys[: len(keys) // 2]:
                    self._user_name_cache.pop(k, None)
            return name
        except Exception:
            return user_id

    # ------------------------------------------------------------------
    # Pairing prompt for unknown users
    # ------------------------------------------------------------------

    async def _send_pairing_prompt(
        self, channel_id: str, user_id: str, user_name: str = "",
    ) -> None:
        """Send a self-registration link to an unknown Slack user."""
        code = await self._pairing.create(
            platform="slack",
            platform_user_id=user_id,
            platform_meta={"user_name": user_name},
        )

        if code is None:
            # Rate limited — don't spam the user.
            return

        base_url = self._api_settings.web_url.rstrip("/")

        link = f"{base_url}/link?code={code}"

        try:
            await self._get_client(channel_id).chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=(
                    f"Your Slack account is not linked yet. "
                    f"Link it now: {link}\n\n"
                    f"Your pairing code is `{code}` (expires in 10 minutes)."
                ),
            )
        except Exception as exc:
            logger.warning("[Slack] Failed to send pairing prompt: %s", exc)

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _free_response_channel_set(self) -> set[str]:
        """Return channel IDs where @mention is NOT required.

        ``SlackSettings.free_response_channels`` is a comma-separated
        string; this helper splits it into a set for O(1) lookup.
        """
        raw = self._slack_settings.free_response_channels
        if not raw:
            return set()
        return {c.strip() for c in raw.split(",") if c.strip()}

    # ------------------------------------------------------------------
    # Active session check
    # ------------------------------------------------------------------

    def _has_active_session_for_thread(
        self, channel_id: str, thread_ts: str, user_id: str,
    ) -> bool:
        """Check if there's an active session for this thread."""
        source = SessionSource(
            platform="slack",
            chat_id=channel_id,
            chat_type="group",
            user_id=user_id,
            thread_id=thread_ts,
        )
        key = build_session_key(source)
        return key in self._session_map

    # ------------------------------------------------------------------
    # Assistant thread lifecycle
    # ------------------------------------------------------------------

    def _assistant_thread_key(self, channel_id: str, thread_ts: str) -> Tuple[str, str]:
        return (channel_id, thread_ts)

    def _extract_assistant_thread_metadata(self, event: dict) -> Dict[str, str]:
        """Extract user_id and thread_ts from assistant thread events."""
        assistant = event.get("assistant_thread", {})
        return {
            "user_id": assistant.get("user_id", ""),
            "channel_id": assistant.get("channel_id", ""),
            "thread_ts": assistant.get("thread_ts", ""),
            "context": json.dumps(assistant.get("context", {})),
        }

    def _cache_assistant_thread_metadata(
        self, channel_id: str, thread_ts: str, metadata: Dict[str, str],
    ) -> None:
        """Cache metadata from an assistant thread lifecycle event."""
        key = self._assistant_thread_key(channel_id, thread_ts)
        self._assistant_threads[key] = metadata
        if len(self._assistant_threads) > self._ASSISTANT_THREADS_MAX:
            keys = list(self._assistant_threads.keys())
            for k in keys[: len(keys) // 2]:
                self._assistant_threads.pop(k, None)

    def _lookup_assistant_thread_metadata(
        self, event: dict, channel_id: str, thread_ts: str,
    ) -> Dict[str, str] | None:
        """Look up cached assistant thread metadata."""
        if not thread_ts:
            return None
        key = self._assistant_thread_key(channel_id, thread_ts)
        return self._assistant_threads.get(key)

    async def _handle_assistant_thread_lifecycle_event(self, event: dict) -> None:
        """Handle assistant_thread_started / context_changed events."""
        metadata = self._extract_assistant_thread_metadata(event)
        channel_id = metadata.get("channel_id", "")
        thread_ts = metadata.get("thread_ts", "")
        if channel_id and thread_ts:
            self._cache_assistant_thread_metadata(channel_id, thread_ts, metadata)

    # ------------------------------------------------------------------
    # Thread context fetching
    # ------------------------------------------------------------------

    async def _fetch_thread_context(
        self,
        channel_id: str,
        thread_ts: str,
        current_ts: str,
        team_id: str = "",
        limit: int = 30,
    ) -> str:
        """Fetch and format prior messages in a thread for context injection.

        Results are cached for ``_THREAD_CACHE_TTL`` seconds.
        Retries on Slack API rate limits (Tier 3).
        """
        cache_key = f"{channel_id}:{thread_ts}"
        now = time.monotonic()

        cached = self._thread_context_cache.get(cache_key)
        if cached and (now - cached.fetched_at) < self._THREAD_CACHE_TTL:
            return cached.content

        client = self._get_client(channel_id)
        bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id or "")

        result = None
        for attempt in range(3):
            try:
                result = await client.conversations_replies(
                    channel=channel_id, ts=thread_ts,
                    limit=limit + 1, inclusive=True,
                )
                break
            except Exception as exc:
                err_str = str(exc).lower()
                if "ratelimited" in err_str or "429" in err_str:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                logger.warning("[Slack] Thread context fetch failed: %s", exc)
                return ""

        if not result:
            return ""

        messages = result.get("messages", [])
        context_parts: list[str] = []

        for msg in messages:
            msg_ts = msg.get("ts", "")
            if msg_ts == current_ts:
                continue
            msg_user = msg.get("user", "")
            if msg_user == bot_uid:
                continue
            msg_text = msg.get("text", "").strip()
            if not msg_text:
                continue
            if bot_uid:
                msg_text = msg_text.replace(f"<@{bot_uid}>", "").strip()
            if not msg_text:
                continue

            name = await self._resolve_user_name(msg_user, channel_id)
            prefix = "[bot] " if msg.get("bot_id") else ""
            context_parts.append(f"{prefix}{name}: {msg_text}")

        if not context_parts:
            return ""

        content = (
            "[Thread context — prior messages in this thread]:\n"
            + "\n".join(context_parts)
            + "\n[End of thread context]\n\n"
        )

        self._thread_context_cache[cache_key] = _ThreadContextCache(
            content=content, fetched_at=now, message_count=len(context_parts),
        )
        return content

    # ------------------------------------------------------------------
    # File downloads
    # ------------------------------------------------------------------

    def _get_bot_token(self, team_id: str = "") -> str:
        """Resolve the bot token for a workspace."""
        if team_id and team_id in self._team_clients:
            return self._team_clients[team_id].token
        if self._team_clients:
            return next(iter(self._team_clients.values())).token
        return ""

    async def _slack_http_get(self, url: str, team_id: str = "", retries: int = 3) -> bytes:
        """Download raw bytes from a Slack file URL with retry."""
        import httpx

        bot_token = self._get_bot_token(team_id)
        async with httpx.AsyncClient(timeout=30.0) as client:
            for attempt in range(retries):
                try:
                    response = await client.get(
                        url, headers={"Authorization": f"Bearer {bot_token}"},
                    )
                    content_type = response.headers.get("content-type", "")
                    if "text/html" in content_type:
                        raise ValueError(
                            "Slack returned HTML instead of media — "
                            "token may be expired or file access restricted"
                        )
                    response.raise_for_status()
                    return response.content
                except Exception as exc:
                    if attempt < retries - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise
        raise RuntimeError(f"Failed to download Slack file after {retries} attempts")

    async def _download_slack_file(
        self, url: str, ext: str, audio: bool = False, team_id: str = "",
    ) -> str:
        """Download a file from Slack and cache it locally."""
        data = await self._slack_http_get(url, team_id=team_id)
        if audio:
            return cache_audio_from_bytes(data, ext)
        return cache_image_from_bytes(data, ext)

    async def _download_slack_file_bytes(self, url: str, team_id: str = "") -> bytes:
        """Download raw bytes from a Slack file URL."""
        return await self._slack_http_get(url, team_id=team_id)

    # ------------------------------------------------------------------
    # Main message processing pipeline (12 steps from Hermes)
    # ------------------------------------------------------------------

    async def _handle_slack_message(self, event: dict) -> None:
        """Process an incoming Slack message event."""

        # Step 1: Deduplication.
        ts = event.get("ts", "")
        if self._dedup.is_duplicate(ts):
            return

        # Step 2: Bot message filtering.
        user_id = event.get("user", "")
        bot_id = event.get("bot_id")
        allow_bots = self._slack_settings.allow_bots

        if bot_id or event.get("subtype") == "bot_message":
            if allow_bots == "none":
                return
            if user_id == self._bot_user_id:
                return
            if allow_bots == "mentions":
                text_check = event.get("text", "")
                if self._bot_user_id and f"<@{self._bot_user_id}>" not in text_check:
                    return

        # Step 3: Ignore edits and deletions.
        subtype = event.get("subtype", "")
        if subtype in ("message_changed", "message_deleted"):
            return

        # Step 4: Extract message data.
        text = event.get("text", "").strip()
        channel_id = event.get("channel", "")
        team_id = event.get("team", "")
        event_thread_ts = event.get("thread_ts")

        assistant_meta = self._lookup_assistant_thread_metadata(
            event, channel_id, event_thread_ts or "",
        )
        if assistant_meta and not user_id:
            user_id = assistant_meta.get("user_id", "")

        if not user_id:
            return

        # Step 5: Track channel → team mapping.
        if team_id:
            self._channel_team[channel_id] = team_id

        # Step 6: DM vs channel detection.
        channel_type = event.get("channel_type", "")
        is_dm = channel_type in ("im", "mpim")

        # Step 7: Thread_ts resolution.
        if is_dm:
            thread_ts = event_thread_ts
        else:
            thread_ts = event_thread_ts or ts

        # Step 8: Channel mention gating.
        bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id or "")
        is_mentioned = bool(bot_uid) and f"<@{bot_uid}>" in text
        is_thread_reply = bool(event_thread_ts and event_thread_ts != ts)

        should_process = True
        if not is_dm and bot_uid:
            if channel_id in self._free_response_channel_set():
                should_process = True
            elif not self._slack_settings.require_mention:
                should_process = True
            elif is_mentioned:
                should_process = True
            else:
                reply_to_bot_thread = is_thread_reply and event_thread_ts in self._bot_message_ts
                in_mentioned_thread = event_thread_ts in self._mentioned_threads
                has_session = self._has_active_session_for_thread(
                    channel_id, event_thread_ts or "", user_id,
                )
                should_process = reply_to_bot_thread or in_mentioned_thread or has_session

        if not should_process:
            return

        # Step 9: Strip bot mention, track mentioned threads.
        if is_mentioned and bot_uid:
            text = text.replace(f"<@{bot_uid}>", "").strip()
            if event_thread_ts:
                self._mentioned_threads.add(event_thread_ts)
                if len(self._mentioned_threads) > self._MENTIONED_THREADS_MAX:
                    self._mentioned_threads = set(
                        list(self._mentioned_threads)[-self._MENTIONED_THREADS_MAX // 2:]
                    )

        if not text and not event.get("files"):
            return

        # Step 10: Thread context fetching.
        if is_thread_reply and not self._has_active_session_for_thread(
            channel_id, event_thread_ts or "", user_id,
        ):
            context = await self._fetch_thread_context(
                channel_id, event_thread_ts or "", ts, team_id=team_id,
            )
            if context:
                text = context + text

        # Step 11: File/media attachment processing.
        msg_type = MessageType.TEXT
        media_urls: list[str] = []
        media_types: list[str] = []

        for file_info in event.get("files", []):
            mimetype = file_info.get("mimetype", "")
            url = file_info.get("url_private_download") or file_info.get("url_private", "")
            filename = file_info.get("name", "file")
            ext = os.path.splitext(filename)[1].lower() or ".bin"

            if not url:
                continue

            try:
                if mimetype.startswith("image/"):
                    path = await self._download_slack_file(url, ext, team_id=team_id)
                    media_urls.append(path)
                    media_types.append(mimetype)
                    msg_type = MessageType.IMAGE
                elif mimetype.startswith("audio/"):
                    path = await self._download_slack_file(url, ext, audio=True, team_id=team_id)
                    media_urls.append(path)
                    media_types.append(mimetype)
                    msg_type = MessageType.AUDIO
                elif ext in SUPPORTED_DOCUMENT_TYPES:
                    raw_bytes = await self._download_slack_file_bytes(url, team_id=team_id)
                    path = cache_document_from_bytes(raw_bytes, filename)
                    doc_mime = SUPPORTED_DOCUMENT_TYPES[ext]
                    media_urls.append(path)
                    media_types.append(doc_mime)
                    msg_type = MessageType.DOCUMENT
                    if ext in (".txt", ".md") and len(raw_bytes) <= 100_000:
                        file_text = raw_bytes.decode("utf-8", errors="replace")
                        text = f"{text}\n\n--- {filename} ---\n{file_text}" if text else file_text
            except Exception as exc:
                logger.warning("[Slack] Failed to download file %s: %s", filename, exc)

        # Step 12: Identity resolution + session routing + event emission.
        user_name = await self._resolve_user_name(user_id, channel_id)

        identity = await resolve_identity(self._sf, "slack", user_id)
        if identity is None:
            logger.warning(
                "[Slack] Unknown user %s (%s) — no channel_identity registered",
                user_id, user_name,
            )
            await self._send_pairing_prompt(channel_id, user_id, user_name)
            return

        source = SessionSource(
            platform="slack",
            chat_id=channel_id,
            chat_type="dm" if is_dm else "group",
            user_id=user_id,
            user_name=user_name,
            thread_id=thread_ts,
            chat_name=channel_id,
        )

        session_key = build_session_key(source)
        session_id = await get_or_create_channel_session(
            self._session_store, self._redis,
            session_key=session_key,
            user_id=identity.user_id,
            org_id=identity.org_id,
            agent_id=self._agent_id,
            channel="slack",
            config={
                "slack_channel_id": channel_id,
                "slack_thread_ts": thread_ts,
                "slack_team_id": team_id,
            },
            session_factory=self._sf,
        )
        self._session_map[session_key] = session_id
        if len(self._session_map) > self._SESSION_MAP_MAX:
            keys = list(self._session_map.keys())
            for k in keys[: len(keys) // 2]:
                self._session_map.pop(k, None)

        should_react = is_dm or is_mentioned
        if should_react:
            await self._add_reaction(channel_id, ts, "eyes")

        await self._session_store.emit_event(
            session_id, EventType.USER_MESSAGE,
            {
                "content": text,
                "media_urls": media_urls,
                "media_types": media_types,
                "source": {
                    "platform": "slack",
                    "chat_id": channel_id,
                    "user_id": user_id,
                    "user_name": user_name,
                    "thread_id": thread_ts,
                },
            },
        )

        await self._redis.zadd("surogates:work_queue", {str(session_id): 0})

        if should_react:
            await self._remove_reaction(channel_id, ts, "eyes")
            await self._add_reaction(channel_id, ts, "white_check_mark")

    # ------------------------------------------------------------------
    # Slash command handler
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, command: dict) -> None:
        """Handle /surogates slash commands."""
        text = command.get("text", "").strip()
        channel_id = command.get("channel_id", "")
        user_id = command.get("user_id", "")

        if not text:
            await self._get_client(channel_id).chat_postEphemeral(
                channel=channel_id, user=user_id,
                text="Usage: /surogates <message>",
            )
            return

        synthetic_event = {
            "text": text,
            "channel": channel_id,
            "user": user_id,
            "ts": str(time.time()),
            "channel_type": "im",
            "team": command.get("team_id", ""),
        }
        await self._handle_slack_message(synthetic_event)

    # ------------------------------------------------------------------
    # Approval button handler
    # ------------------------------------------------------------------

    async def _handle_approval_action(self, ack, body, action) -> None:
        """Handle Block Kit button clicks for approval prompts."""
        await ack()

        action_id = action.get("action_id", "")
        msg_ts = body.get("message", {}).get("ts", "")

        already_resolved = self._approval_resolved.pop(msg_ts, True)
        if already_resolved:
            return

        choice = {
            "surogates_approve_once": "approve_once",
            "surogates_approve_session": "approve_session",
            "surogates_approve_always": "approve_always",
            "surogates_deny": "deny",
        }.get(action_id, "deny")

        user_name = body.get("user", {}).get("username", "unknown")
        channel_id = body.get("channel", {}).get("id", "")

        try:
            decision_text = f"{'✅ Approved' if 'approve' in choice else '❌ Denied'} by {user_name}"
            await self._get_client(channel_id).chat_update(
                channel=channel_id, ts=msg_ts,
                text=decision_text,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": decision_text}}],
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Background delivery loop
    # ------------------------------------------------------------------

    async def _delivery_loop(self) -> None:
        """Claim outbox items for channel='slack' and deliver via Slack API."""
        worker_id = f"slack-{os.getpid()}"
        while self._running:
            try:
                items = await self._delivery.claim_batch("slack", worker_id, limit=20)
                for item in items:
                    try:
                        dest = item.destination
                        content = item.payload.get("content", "")
                        channel_id = dest.get("channel_id", dest.get("chat_id", ""))
                        thread_ts = dest.get("thread_ts")

                        result = await self.send(
                            target=channel_id, content=content,
                            reply_to=thread_ts, metadata=dest,
                        )
                        if result.success:
                            await self._delivery.mark_delivered(
                                item.id, provider_message_id=result.message_id,
                            )
                        else:
                            await self._delivery.mark_failed(item.id, result.error or "unknown")
                    except Exception as exc:
                        logger.error("[Slack] Delivery failed: %s", exc)
                        try:
                            await self._delivery.mark_failed(item.id, str(exc))
                        except Exception:
                            pass

                if not items:
                    await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[Slack] Delivery loop error")
                await asyncio.sleep(5.0)
