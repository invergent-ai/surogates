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

The firehose gate uses ``deps.follow_enabled`` — an async resolver
``(agent_id, platform, channel_id) → bool`` — rather than a static config
key.  The resolver is wired by :func:`~surogates.channels.runner.run_channels`
using :class:`~surogates.runtime.mate_settings_cache.MateSettingsCache` so
the "follow this channel" toggle is live without a process restart (within
the cache TTL).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy.exc import InterfaceError, OperationalError

from surogates.channels.dedup import MessageDeduplicator
from surogates.channels.source import SessionSource, build_session_key
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

__all__ = [
    "InboundFileRef",
    "InboundMessage",
    "InboundOutcome",
    "PipelineDeps",
    "ChannelInboundPipeline",
]


# ---------------------------------------------------------------------------
# Normalised message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundFileRef:
    """A file attached to an inbound message, before download.

    ``url`` is the platform's download URL (auth-gated for Slack);
    ``mime_type`` is the platform's type or a ``mimetypes.guess_type``
    fallback; ``size`` is the platform-reported byte size when known.
    """
    url: str
    filename: str
    mime_type: str
    size: int | None


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
    visibility:
        Conversation privacy for memory isolation:
        ``"public"``, ``"private"``, or ``"dm"``.  Defaults to
        ``"private"`` so omitted/unknown adapter values fail closed.
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
    is_bot: bool = False
    # Conversation privacy: "public" | "private" | "dm".  Default is the
    # fail-closed value so any constructor that omits it is treated as private.
    visibility: str = "private"
    files: list[InboundFileRef] = field(default_factory=list)


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
    """Linked-mode sender is unknown; a link prompt was sent, no session created."""

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

#: Callable type for the identity resolver (get-or-create).
_ResolveIdentity = Callable[..., Awaitable[Any]]

#: Callable type for the follow-enabled resolver.
#: Args: (agent_id, platform, channel_id) → bool
_FollowEnabled = Callable[[str, str, str], Awaitable[bool]]

#: Async callable: (session_id, channel_id, routing) -> seeded event id | None.
_Backfill = Callable[[Any, str, Any], Awaitable[int | None]]

#: Async callable: (session_id, channel_id, thread_ts) -> None. Posts a
#: "Thinking…" placeholder for a message that will be answered.
_Progress = Callable[[Any, str, Any], Awaitable[None]]

#: Async callable: (session_id, msg) -> {"images": list, "attachments": list, "note": str}.
#: Downloads and ingests platform file attachments into the harness event shapes.
_Attachments = Callable[[Any, Any], Awaitable[dict]]


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
        Compatible with :class:`~surogates.channels.channel_state.ChannelAdapterState`.
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
        :func:`~surogates.channels.identity.get_or_create_channel_identity`
        — resolves a channel sender to an identity, provisioning a lightweight
        external user (scoped to the agent's org) on first contact.
    session_factory:
        SQLAlchemy ``async_sessionmaker`` forwarded to ``get_or_create_session``
        and ``resolve_identity`` (may be ``None`` in tests that override both).
    follow_enabled:
        Async resolver ``(agent_id, platform, channel_id) → bool`` that
        returns ``True`` when the agent has enabled follow mode for this
        channel.  When ``None``, non-mention non-DM messages are DROPPED
        (safe default: no firehose without an explicit follow subscription).
        Wired by :func:`~surogates.channels.runner.run_channels` via
        :class:`~surogates.runtime.mate_settings_cache.MateSettingsCache`.
    """

    session_store: Any
    redis: Any
    state: Any
    firehose_append: _FirehoseAppend
    get_or_create_session: _GetOrCreateSession
    enqueue_session: _EnqueueSession
    resolve_identity: _ResolveIdentity
    session_factory: Any
    follow_enabled: _FollowEnabled | None = None
    # ``linked`` identity policy only — the producer that mints a pairing code
    # and privately delivers the link prompt.  Unused (``None``) in ``shadow``
    # mode, so Mate constructs neither.
    pairing: Any = None
    pairing_sender: Any = None
    backfill: _Backfill | None = None
    progress: _Progress | None = None
    attachments: _Attachments | None = None


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
            Object with ``org_id``, ``agent_id``, ``platform``, and ``identifier``
            attributes.  ``identifier`` is the routing/app identifier (e.g. Slack
            app_id, Telegram bot username) that keys ``channel_routing`` in the
            cache — it is DIFFERENT from ``msg.identifier`` (the chat/channel id).
        config:
            Channel gating settings extracted from ``channel_routing.config``:

            * ``require_mention`` (bool) — gate non-DMs on @mention.
            * ``free_response_channels`` (set[str]) — channel identifiers
              that bypass the mention gate.
            * ``allow_bots`` (str) — ``"none"`` / ``"mentions"`` / ``"all"``.

        deps:
            Injected dependencies (session store, Redis, state, identity
            resolver, …).
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
        # Gate 2b: Bot filter.
        #
        # Runs right after dedup + empty-body so bot messages are gated
        # consistently before any platform-specific mention logic.
        # Own-bot messages are dropped at parse (never reach here); this gate
        # handles OTHER bots' messages based on the allow_bots config key:
        #   "none"     → drop all bot messages.
        #   "mentions" → drop if not @-mentioned; pass if mentioned.
        #   "all"      → pass all bot messages through.
        # Human messages (is_bot=False) are always unaffected.
        # ------------------------------------------------------------------
        if msg.is_bot:
            allow_bots: str = (config.get("allow_bots") or "none")
            if allow_bots == "none":
                return InboundOutcome.DROPPED
            if allow_bots == "mentions" and not msg.is_mention:
                return InboundOutcome.DROPPED
            # allow_bots == "all", or "mentions" + is_mention → fall through.

        # ------------------------------------------------------------------
        # Gate 3: Mention gating (non-DM only).
        # ------------------------------------------------------------------
        should_process = self._evaluate_mention_gate(msg, config)

        if not should_process:
            # Check Redis state for thread-based bypass gates.
            should_process = await self._check_thread_gates(msg, routing, deps, config)

        if not should_process:
            # Not gated for processing — optionally firehose when the agent
            # has enabled follow mode for this channel (resolved from
            # MateSettingsCache, not from a static config key).
            if (
                not msg.is_dm
                and deps.follow_enabled is not None
                and await deps.follow_enabled(routing.agent_id, routing.platform, msg.identifier)
            ):
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
        # ``deps.resolve_identity`` is policy-aware (see the deps factory): in
        # ``shadow`` mode (Mate) it auto-provisions an org-scoped identity —
        # channel membership is the authorisation boundary; in ``linked`` mode
        # (multi-user assistant) it resolves only a real linked account and
        # returns None for an unknown sender, who is prompted to link below.
        #
        # The lookup hits the DB, so it can fail transiently (deadlock,
        # connection drop).  Drop on failure rather than letting the exception
        # 5xx the webhook handler — the platform redelivers, and a retry storm
        # of 5xxs helps no one.
        try:
            identity = await deps.resolve_identity(
                deps.session_factory,
                routing.platform,
                msg.platform_user_id,
                org_id=routing.org_id,
                display_name=msg.user_name,
            )
        except (OperationalError, InterfaceError):
            # Transient DB fault (deadlock, connection drop) — expected under
            # load.  Drop at WARNING; the platform redelivers.
            logger.warning(
                "[inbound] Transient DB error resolving identity for %s on %s — dropping",
                msg.platform_user_id, routing.platform,
            )
            return InboundOutcome.DROPPED
        except Exception:
            # Unexpected — a real bug (bad data, constraint mismatch).  Still
            # drop so we don't 5xx-storm the webhook, but log at ERROR with the
            # traceback so it's surfaced, not masked as a routine drop.
            logger.error(
                "[inbound] Unexpected error resolving identity for %s (%s) on %s — dropping",
                msg.platform_user_id, msg.user_name, routing.platform,
                exc_info=True,
            )
            return InboundOutcome.DROPPED
        if identity is None:
            # ``linked`` (multi-user assistant): an unknown sender is NOT
            # auto-provisioned — mint a code and privately prompt them to link
            # their real Surogate account; no session opens until they do.
            # ``shadow`` (Mate): the resolver provisions, so None means a genuine
            # provisioning failure → drop.
            if config.get("identity_policy", "shadow") == "linked":
                code = await deps.pairing.create(
                    str(routing.org_id),
                    routing.platform,
                    msg.platform_user_id,
                    {"user_name": msg.user_name},
                )
                delivered = False
                if code and deps.pairing_sender is not None:
                    delivered = await deps.pairing_sender(
                        routing.org_id, routing.platform, msg, code,
                    )
                if delivered:
                    return InboundOutcome.PAIRING_PROMPTED
                # The code was minted but the prompt never reached the sender
                # (no private channel, or the user blocked the bot).  Report
                # DROPPED rather than PAIRING_PROMPTED — the sender saw nothing,
                # and the still-live code is retried on their next message.
                logger.warning(
                    "[inbound] Link prompt not delivered to %s (%s) on %s — dropping",
                    msg.platform_user_id, msg.user_name, routing.platform,
                )
                return InboundOutcome.DROPPED
            logger.warning(
                "[inbound] No identity resolved for %s (%s) on %s — dropping",
                msg.platform_user_id, msg.user_name, routing.platform,
            )
            return InboundOutcome.DROPPED

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
        session_key = build_session_key(source, per_user_groups=bool(config.get("per_user_groups", False)))

        from surogates.channels.memory_boundary import boundary_token

        memory_boundary = boundary_token(
            platform=routing.platform,
            channel_id=msg.identifier,
            visibility=msg.visibility,
            source=msg.source,
            fallback_id=session_key,
        )

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
                "channel_identifier": routing.identifier,
                "memory_boundary": memory_boundary,
            },
            session_factory=deps.session_factory,
        )

        # Remember in Redis-backed state for thread-gate lookups.
        await deps.state.remember_session(session_key, str(session_id))

        # Seed channel history on the first message of a Slack channel session
        # (lazy fallback for channels where the join event was missed). Best
        # effort: maybe_seed_session is idempotent and never raises.
        if deps.backfill is not None and routing.platform == "slack" and not msg.is_dm:
            await deps.backfill(session_id, msg.identifier, routing)

        # Download + ingest Slack file attachments into the harness's
        # images/attachments event shapes. Best-effort: never drop the message.
        _images: list = []
        _attachments: list = []
        _att_note = ""
        if (deps.attachments is not None and routing.platform == "slack"
                and getattr(msg, "files", None)):
            try:
                _ingested = await deps.attachments(session_id, msg)
                _images = _ingested.get("images") or []
                _attachments = _ingested.get("attachments") or []
                _att_note = _ingested.get("note") or ""
            except Exception:
                logger.warning("[channels] attachment ingest failed", exc_info=True)

        _content = msg.text
        if _att_note:
            _content = f"{_content}\n{_att_note}" if _content else _att_note

        # ------------------------------------------------------------------
        # Gate 7: Emit USER_MESSAGE event.
        # ------------------------------------------------------------------
        event_data: dict = {
            "content": _content,
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
        }
        if _images:
            event_data["images"] = _images
        if _attachments:
            event_data["attachments"] = _attachments

        await deps.session_store.emit_event(
            session_id,
            EventType.USER_MESSAGE,
            event_data,
        )

        # Post a "Thinking…" placeholder so the user sees progress while the
        # worker runs. Best effort: progress failures must not block enqueue.
        if deps.progress is not None and routing.platform == "slack":
            try:
                await deps.progress(session_id, msg.identifier, msg.thread_key)
            except Exception:
                logger.warning(
                    "[channels] thinking-placeholder progress failed — ignoring",
                    exc_info=True,
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
        config: dict,
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
        key = build_session_key(source, per_user_groups=bool(config.get("per_user_groups", False)))
        return await deps.state.get_session(key) is not None
