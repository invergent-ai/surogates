"""Platform-agnostic inbound message pipeline.

Every messaging-platform adapter (Slack, Telegram, …) normalises its raw
events into an :class:`InboundMessage` and calls
:meth:`ChannelInboundPipeline.handle`.  The pipeline applies the shared
gating logic (dedup → bot-filter → mention gate → identity → session) and
returns an :class:`InboundOutcome` so callers can react (e.g. send a
reaction emoji, log the decision) without re-implementing the gates.

Design constraints:
- Slack-free: no slack_bolt imports; all platform facts already live in the
  normalised ``InboundMessage``.
- Dependency-injected: callers pass a :class:`PipelineDeps` bundle so tests
  need no network, database, or Redis.
- Replay-stable: identical ``(platform_user_id, identifier, thread_key)``
  tuples produce the same ``session_key`` via :func:`build_session_key`
  regardless of how many times the pipeline runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable
from uuid import UUID

from surogates.channels.dedup import MessageDeduplicator
from surogates.channels.source import SessionSource, build_session_key
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

__all__ = [
    "InboundMessage",
    "InboundOutcome",
    "PipelineDeps",
    "ChannelInboundPipeline",
]


# ---------------------------------------------------------------------------
# Normalised message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundMessage:
    """Platform-agnostic representation of an inbound message.

    All platform-specific facts (is DM? was the bot mentioned? what is the
    thread identifier?) are resolved by the adapter before this object is
    constructed, so the pipeline can operate without any platform SDK.

    Attributes
    ----------
    kind:
        Message kind: ``"text"``, ``"image"``, ``"audio"``, ``"document"``.
    identifier:
        Platform chat / channel identifier (e.g. Slack channel_id, Telegram
        chat_id).
    thread_key:
        Platform thread identifier within the chat, or ``None`` for top-level
        messages (DMs without an explicit thread, non-threaded channels).
    platform_user_id:
        Raw platform user identifier (e.g. Slack ``U123456``).
    user_name:
        Human-readable display name for the sender.
    text:
        Normalised message text (bot mention stripped by the adapter if
        applicable).
    media_urls:
        Local file paths or remote URLs for any attached media.
    media_types:
        MIME types corresponding to each entry in ``media_urls``.
    is_dm:
        ``True`` when the message arrived in a direct-message conversation.
    is_mention:
        ``True`` when the bot was @-mentioned in the message text.
    ts:
        Platform-issued monotonic timestamp string used for deduplication.
    source:
        Freeform platform-specific metadata forwarded verbatim into the
        ``USER_MESSAGE`` event payload.
    """

    kind: str
    identifier: str
    thread_key: str | None
    platform_user_id: str
    user_name: str
    text: str
    media_urls: list[str]
    media_types: list[str]
    is_dm: bool
    is_mention: bool
    ts: str
    source: dict


# ---------------------------------------------------------------------------
# Outcome enum
# ---------------------------------------------------------------------------


class InboundOutcome(str, Enum):
    """Result returned by :meth:`ChannelInboundPipeline.handle`."""

    PROCESSED = "processed"
    """Session created/resumed, USER_MESSAGE emitted, session enqueued."""

    FIREHOSED = "firehosed"
    """Message appended as a non-waking channel observation (follow mode)."""

    PAIRING_PROMPTED = "pairing_prompted"
    """Sender is unknown; pairing code sent, no session created."""

    DROPPED = "dropped"
    """Message discarded (duplicate, mention gate, bot filter, empty body)."""


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------

#: Callable type for the firehose append helper.
_FirehoseAppend = Callable[..., Awaitable[None]]

#: Callable type for get-or-create-session.
_GetOrCreateSession = Callable[..., Awaitable[UUID]]

#: Callable type for enqueue_session.
_EnqueueSession = Callable[..., Awaitable[None]]

#: Callable type for resolve_identity.
_ResolveIdentity = Callable[..., Awaitable[Any]]

#: Callable type for the pairing sender (platform-specific prompt).
_PairingSender = Callable[[str, str, str], Awaitable[None]]


@dataclass
class PipelineDeps:
    """Injectable dependencies for :class:`ChannelInboundPipeline`.

    Using a dataclass keeps the ``handle`` signature clean and makes fakes
    easy to construct in tests.

    Attributes
    ----------
    session_store:
        The Surogates :class:`~surogates.session.store.SessionStore`.
    redis:
        Async Redis client (used by ``enqueue_session`` and
        ``firehose_append``).
    state:
        Adapter state object exposing ``is_mentioned_thread``,
        ``mark_mentioned_thread``, ``get_session``, and ``remember_session``.
        Compatible with :class:`~surogates.channels.slack_state.SlackAdapterState`.
    pairing:
        :class:`~surogates.channels.pairing.PairingStore` instance.
    firehose_append:
        Callable matching the signature of
        :func:`~surogates.channels.channel_observations.append_channel_observation`.
    get_or_create_session:
        Callable matching the signature of
        :func:`~surogates.channels.identity.get_or_create_channel_session`.
    enqueue_session:
        Callable matching the signature of
        :func:`~surogates.config.enqueue_session`.
    resolve_identity:
        Callable matching the signature of
        :func:`~surogates.channels.identity.resolve_identity`.
    session_factory:
        SQLAlchemy ``async_sessionmaker`` forwarded to ``get_or_create_session``
        and ``resolve_identity`` (may be ``None`` in tests that override both).
    pairing_sender:
        Platform-specific coroutine ``(platform_user_id, user_name, code) →
        None`` that delivers the pairing prompt to the user.
    """

    session_store: Any
    redis: Any
    state: Any
    pairing: Any
    firehose_append: _FirehoseAppend
    get_or_create_session: _GetOrCreateSession
    enqueue_session: _EnqueueSession
    resolve_identity: _ResolveIdentity
    session_factory: Any
    pairing_sender: _PairingSender


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class ChannelInboundPipeline:
    """Shared inbound message pipeline for all channel adapters.

    Each adapter instance should create **one** pipeline and reuse it across
    messages so the :class:`~surogates.channels.dedup.MessageDeduplicator`
    accumulates state across calls.

    Usage::

        pipeline = ChannelInboundPipeline()
        outcome = await pipeline.handle(msg, routing=routing, config=cfg, deps=deps)
    """

    def __init__(self, *, dedup_max_size: int = 2000, dedup_ttl: float = 300) -> None:
        self._dedup = MessageDeduplicator(max_size=dedup_max_size, ttl_seconds=dedup_ttl)

    async def handle(
        self,
        msg: InboundMessage,
        *,
        routing: Any,
        config: dict,
        deps: PipelineDeps,
    ) -> InboundOutcome:
        """Process one normalised inbound message through the shared gate.

        Parameters
        ----------
        msg:
            Normalised message (platform facts already resolved).
        routing:
            Object with ``org_id``, ``agent_id``, and ``platform`` attributes.
        config:
            Channel gating settings extracted from ``channel_routing.config``:

            * ``require_mention`` (bool) — gate non-DMs on @mention.
            * ``free_response_channels`` (set[str]) — channel identifiers
              that bypass the mention gate.
            * ``allow_bots`` (str) — ``"none"`` / ``"mentions"`` / ``"all"``.
            * ``follow_enabled`` (bool) — append non-mention messages as
              channel observations instead of dropping them silently.

        deps:
            Injected dependencies (session store, Redis, state, pairing, …).
        """

        # ------------------------------------------------------------------
        # Gate 1: Deduplication.
        # ------------------------------------------------------------------
        if self._dedup.is_duplicate(msg.ts):
            return InboundOutcome.DROPPED

        # ------------------------------------------------------------------
        # Gate 2: Non-empty body (text or media required).
        #
        # Runs before the mention/firehose gates: an empty message (no text,
        # no media) is dropped outright and never becomes a firehose
        # observation, matching the Slack reference where the firehose helper
        # also no-ops on empty text.
        # ------------------------------------------------------------------
        if not msg.text and not msg.media_urls:
            return InboundOutcome.DROPPED

        # ------------------------------------------------------------------
        # Gate 3: Mention gating (non-DM only).
        # ------------------------------------------------------------------
        should_process = self._evaluate_mention_gate(msg, config)

        if not should_process:
            # Check Redis state for thread-based bypass gates.
            should_process = await self._check_thread_gates(msg, routing, deps)

        if not should_process:
            # Not gated for processing — optionally firehose.
            if not msg.is_dm and config.get("follow_enabled", False):
                await deps.firehose_append(
                    deps.redis,
                    agent_id=routing.agent_id,
                    channel_id=msg.identifier,
                    observation={
                        "content": msg.text,
                        "ts": msg.ts,
                        "source": {
                            "platform": routing.platform,
                            "chat_id": msg.identifier,
                            "user_id": msg.platform_user_id,
                            "user_name": msg.user_name,
                        },
                    },
                )
                return InboundOutcome.FIREHOSED
            return InboundOutcome.DROPPED

        # ------------------------------------------------------------------
        # Gate 4: Track mentioned threads for future replies.
        # ------------------------------------------------------------------
        if msg.is_mention and msg.thread_key:
            await deps.state.mark_mentioned_thread(msg.thread_key)

        # ------------------------------------------------------------------
        # Gate 5: Identity resolution.
        # ------------------------------------------------------------------
        identity = await deps.resolve_identity(
            deps.session_factory,
            routing.platform,
            msg.platform_user_id,
        )
        if identity is None:
            logger.warning(
                "[inbound] Unknown user %s (%s) on %s — no channel_identity registered",
                msg.platform_user_id, msg.user_name, routing.platform,
            )
            code = await deps.pairing.create(
                routing.platform,
                msg.platform_user_id,
                platform_meta={"identifier": msg.identifier, "user_name": msg.user_name},
            )
            if code:
                await deps.pairing_sender(msg.platform_user_id, msg.user_name, code)
            return InboundOutcome.PAIRING_PROMPTED

        # ------------------------------------------------------------------
        # Gate 6: Session resolution (get-or-create).
        # ------------------------------------------------------------------
        chat_type = "dm" if msg.is_dm else "group"
        source = SessionSource(
            platform=routing.platform,
            chat_id=msg.identifier,
            chat_type=chat_type,
            user_id=msg.platform_user_id,
            user_name=msg.user_name,
            thread_id=msg.thread_key,
            chat_name=msg.identifier,
        )
        session_key = build_session_key(source)

        session_id = await deps.get_or_create_session(
            deps.session_store,
            deps.redis,
            session_key=session_key,
            user_id=identity.user_id,
            org_id=routing.org_id,
            agent_id=routing.agent_id,
            channel=routing.platform,
            config={
                f"{routing.platform}_channel_id": msg.identifier,
                f"{routing.platform}_thread_key": msg.thread_key,
            },
            session_factory=deps.session_factory,
        )

        # Remember in Redis-backed state for thread-gate lookups.
        await deps.state.remember_session(session_key, str(session_id))

        # ------------------------------------------------------------------
        # Gate 7: Emit USER_MESSAGE event.
        # ------------------------------------------------------------------
        await deps.session_store.emit_event(
            session_id,
            EventType.USER_MESSAGE,
            {
                "content": msg.text,
                "media_urls": msg.media_urls,
                "media_types": msg.media_types,
                "source": {
                    # Adapter-supplied metadata first; pipeline-derived keys
                    # win so an adapter can't silently shadow them.
                    **msg.source,
                    "platform": routing.platform,
                    "chat_id": msg.identifier,
                    "user_id": msg.platform_user_id,
                    "user_name": msg.user_name,
                    "thread_id": msg.thread_key,
                },
            },
        )

        # ------------------------------------------------------------------
        # Gate 8: Enqueue for worker pickup.
        # ------------------------------------------------------------------
        await deps.enqueue_session(
            deps.redis,
            org_id=str(routing.org_id),
            agent_id=routing.agent_id,
            session_id=session_id,
        )

        return InboundOutcome.PROCESSED

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate_mention_gate(msg: InboundMessage, config: dict) -> bool:
        """Decide whether the message passes static mention-gating rules.

        Returns ``True`` if the message should be processed (ignoring Redis
        thread state, which is checked separately by
        :meth:`_check_thread_gates`).
        """
        # DMs always bypass mention gating.
        if msg.is_dm:
            return True

        # Free-response channels bypass mention gating.
        free_channels: set[str] = config.get("free_response_channels", set())
        if msg.identifier in free_channels:
            return True

        # No mention requirement configured → process everything.
        if not config.get("require_mention", True):
            return True

        # Explicit @mention → process.
        if msg.is_mention:
            return True

        return False

    @staticmethod
    async def _check_thread_gates(
        msg: InboundMessage,
        routing: Any,
        deps: PipelineDeps,
    ) -> bool:
        """Return ``True`` if Redis thread state grants processing rights.

        Called only when the static mention gate returned ``False``.  Checks:
        1. The thread root was authored by the bot (``state.is_bot_message``).
        2. The thread was previously mentioned (``state.is_mentioned_thread``).
        3. An active session already exists for this thread
           (``state.get_session``).
        """
        thread_key = msg.thread_key
        if not thread_key:
            return False

        if await deps.state.is_bot_message(thread_key):
            return True

        if await deps.state.is_mentioned_thread(thread_key):
            return True

        # Build the session key for the thread and check state. Mirror Gate 6's
        # chat_type derivation so the lookup key matches the stored key.
        chat_type = "dm" if msg.is_dm else "group"
        source = SessionSource(
            platform=routing.platform,
            chat_id=msg.identifier,
            chat_type=chat_type,
            user_id=msg.platform_user_id,
            thread_id=thread_key,
        )
        key = build_session_key(source)
        return await deps.state.get_session(key) is not None
