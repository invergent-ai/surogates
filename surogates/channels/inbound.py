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
import re
from dataclasses import dataclass, field
from functools import lru_cache
from enum import Enum
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy.exc import InterfaceError, OperationalError

from surogates.channels.dedup import MessageDeduplicator
from surogates.channels.constants import multi_session_disabled
from surogates.channels.source import (
    SessionSource,
    build_session_key_for_config,
)
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
    ``file_id`` is the platform's opaque file identifier (e.g. Slack's
    ``F…`` id) that can be passed to ``fetch_channel_file`` for on-demand
    download.  ``None`` when the platform does not provide one.
    """
    url: str
    filename: str
    mime_type: str
    size: int | None
    file_id: str | None = None


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
    is_group_dm: bool = False
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

    INTERRUPTED = "interrupted"
    """A ``/stop`` command — the running turn was interrupted out-of-band; no
    session message was emitted or enqueued."""


def _allowance_block_notice(code: str | None, buy_url: str | None) -> str:
    """User-facing notice when the allowance gate drops a channel turn.

    Names the reason (subscription vs. usage limit) and appends the buy
    link when the agent projects one, so slack/telegram senders get a
    real path to keep going rather than a dead "try again later".
    """
    if code == "operator_subscription_exhausted":
        # The agent OWNER ran out of platform credit; buying more access
        # cannot help the sender, so no buy link is offered.
        return (
            "This assistant is temporarily unavailable. Its owner has "
            "run out of credit."
        )
    if code == "subscription_required":
        lead = "A subscription is required to keep chatting with this assistant."
    elif code == "channel_not_included":
        lead = "Your current plan doesn't include this channel."
    else:
        lead = "You've reached your usage limit for this assistant."
    if buy_url:
        return f"{lead} Get more access here: {buy_url}"
    return f"{lead} Please try again later."


@lru_cache(maxsize=256)
def _mention_pattern_regex(csv: str) -> re.Pattern | None:
    """Compile a routing config's ``mention_patterns`` CSV into one regex.

    Cached per distinct config string — the gate runs on every group
    message, and routing configs change rarely.  ``None`` when no patterns
    are configured.
    """
    patterns = [p.strip() for p in csv.split(",") if p.strip()]
    if not patterns:
        return None
    alternation = "|".join(re.escape(p) for p in patterns)
    # Lookarounds, not \b: a word boundary needs a \w on one side, so
    # patterns that start or end with punctuation ("@bot", "libra!") would
    # never match after a space.  (?<!\w)/(?!\w) behaves like \b for plain
    # words and still works for punctuated patterns.
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


def is_stop_command(text: str | None) -> bool:
    """True for a bare ``/stop`` (or ``/cancel``) message — a request to
    interrupt the running turn, not a prompt to process."""
    return (text or "").strip().lower() in ("/stop", "/cancel")


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

#: Async callable: (session_id) -> pending input dict | None.
_PendingInput = Callable[[Any], Awaitable[dict | None]]

#: Async callable: (session_id, msg, text) -> None. Posts a nudge to the channel/thread.
_InputNudge = Callable[[Any, "InboundMessage", str], Awaitable[None]]

#: Async callable: (agent_id) -> runtime-config payload dict (for the
#: projected ``end_user_token_allowance``).
_RuntimeConfig = Callable[[str], Awaitable[dict]]


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
    pending_input: _PendingInput | None = None
    input_nudge: _InputNudge | None = None
    # Per-user token allowance enforcement (a slice of the operator's
    # subscription). ``platform_client`` reserves against ops;
    # ``runtime_config`` resolves the agent's projected allowance. Both
    # ``None`` disables the gate (older wiring / tests), so slack/telegram
    # behave exactly as before until this is wired.
    platform_client: Any = None
    runtime_config: _RuntimeConfig | None = None


# ---------------------------------------------------------------------------
# Event-source helpers
# ---------------------------------------------------------------------------


def resolve_chat_type(msg) -> str:
    """Normalized chat kind for routing + attribution.

    'dm' only for a 1:1 direct message. A multi-person DM (``is_group_dm``) is a
    multi-party conversation, so it is 'group' — like a channel — and its
    messages get per-sender attribution, even though it stays DM-like for
    mention gating.
    """
    return "dm" if (msg.is_dm and not msg.is_group_dm) else "group"


def build_message_source(msg, *, platform: str, chat_type: str) -> dict:
    """Source metadata for a USER_MESSAGE event.

    Adapter-supplied metadata comes first; pipeline-derived keys win so an
    adapter cannot shadow them. ``chat_type`` is the normalized chat kind
    ('dm'/'group') and overrides any adapter-native value (Slack
    ``channel_type``, Telegram ``supergroup``).
    """
    return {
        **msg.source,
        "platform": platform,
        "chat_id": msg.identifier,
        "chat_type": chat_type,
        "user_id": msg.platform_user_id,
        "user_name": msg.user_name,
        "thread_id": msg.thread_key,
        "ts": msg.ts,
    }


def build_principal_stamp(*, user_id=None, service_account_id=None) -> dict:
    """The resolved-principal fields to merge onto a USER_MESSAGE event.

    ``principal_user_id`` is the Surogates user UUID (distinct from the
    platform id in ``source.user_id``). API-channel senders can stamp a
    service-account principal instead. Empty when no identity resolved.
    """
    if user_id is not None and service_account_id is not None:
        raise ValueError("principal stamp requires exactly one principal id")
    if user_id is not None:
        return {"principal_user_id": str(user_id)}
    if service_account_id is not None:
        return {"principal_service_account_id": str(service_account_id)}
    return {}


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
        # also no-ops on empty text.  Media can arrive as pre-resolved
        # ``media_urls`` or as platform file refs in ``files`` (Telegram
        # attachments carry only file ids) — either counts as a body.
        # ------------------------------------------------------------------
        if not msg.text and not msg.media_urls and not msg.files:
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
        chat_type = resolve_chat_type(msg)
        source = SessionSource(
            platform=routing.platform,
            chat_id=msg.identifier,
            chat_type=chat_type,
            user_id=msg.platform_user_id,
            user_name=msg.user_name,
            thread_id=msg.thread_key,
            chat_name=msg.identifier,
        )
        # "multi session" off (ops projects the agent capability into the
        # routing config) folds thread suffixes so every thread continues
        # the same conversation; see build_session_key_for_config.
        single_session = multi_session_disabled(config)
        session_key = build_session_key_for_config(source, config)

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
                "multi_party": chat_type in ("group", "channel"),
                # Marks the session as a collapsed single-session
                # conversation; delivery reads the thread key fresh for
                # these (see the outbox enqueue in the session store).
                **({"single_session": True} if single_session else {}),
            },
            session_factory=deps.session_factory,
        )

        # Remember in Redis-backed state for thread-gate lookups.
        await deps.state.remember_session(session_key, str(session_id))

        # ------------------------------------------------------------------
        # /stop — interrupt the running turn OUT-OF-BAND.  A normal message
        # would queue behind the busy worker and never reach it in time (a
        # long coding run, say), so publish the interrupt directly: the
        # worker's interrupt listener picks it up and cancels the in-flight
        # run.  No USER_MESSAGE is emitted and the session is not enqueued.
        # ------------------------------------------------------------------
        if is_stop_command(msg.text):
            import json as _json

            from surogates.config import INTERRUPT_CHANNEL_PREFIX
            try:
                await deps.redis.publish(
                    f"{INTERRUPT_CHANNEL_PREFIX}:{session_id}",
                    _json.dumps({"reason": "channel_stop"}),
                )
            except Exception:
                logger.warning(
                    "[channels] /stop interrupt publish failed", exc_info=True,
                )
            if deps.input_nudge is not None:
                try:
                    await deps.input_nudge(
                        session_id, msg, "⏹ Stopping the current run…",
                    )
                except Exception:
                    logger.warning("[channels] /stop ack failed", exc_info=True)
            return InboundOutcome.INTERRUPTED

        # The agent's runtime config feeds both the disclosure notice and
        # the allowance gate below. Resolve it at most once per message —
        # the two used to fetch it independently — and only when a
        # consumer is actually wired. A failed fetch resolves to ``{}``,
        # which both consumers read as "nothing configured": disclosure
        # stays silent and the allowance gate stays open.
        runtime_payload: dict | None = None
        if deps.runtime_config is not None and (
            deps.input_nudge is not None
            or (identity.user_id is not None and deps.platform_client is not None)
        ):
            try:
                runtime_payload = await deps.runtime_config(routing.agent_id) or {}
            except Exception:
                logger.warning(
                    "[channels] runtime config unavailable for agent %s",
                    routing.agent_id, exc_info=True,
                )
                runtime_payload = {}

        # AI disclosure (EU AI Act Art. 50): on the first contact of a
        # conversation with a disclosure-enabled agent, the channel user
        # gets the notice as its own message before any agent output.
        # After the /stop gate — a control command interrupts a run, it
        # does not open a conversation.
        await self._maybe_send_disclosure(
            msg, routing=routing, deps=deps, session_id=session_id,
            runtime_payload=runtime_payload,
        )

        # While a question is pending, the platforms diverge on what a plain
        # reply means — see _intercept_pending_input.
        if deps.pending_input is not None and routing.platform in (
            "slack", "telegram", "whatsapp",
        ):
            intercepted = await self._intercept_pending_input(
                msg, routing=routing, deps=deps, session_id=session_id,
            )
            if intercepted is not None:
                return intercepted

        # Seed channel history on the first message of a Slack channel session
        # (lazy fallback for channels where the join event was missed). Best
        # effort: maybe_seed_session is idempotent and never raises.
        if deps.backfill is not None and routing.platform == "slack" and not msg.is_dm:
            await deps.backfill(session_id, msg.identifier, routing)

        await self._record_reply_target(
            msg, routing=routing, config=config, deps=deps, session_id=session_id,
        )

        # Download + ingest platform file attachments into the harness's
        # images/attachments event shapes. Best-effort: never drop the message.
        _images: list = []
        _attachments: list = []
        _att_note = ""
        if deps.attachments is not None and getattr(msg, "files", None):
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
            "source": build_message_source(
                msg, platform=routing.platform, chat_type=chat_type,
            ),
        }
        if _images:
            event_data["images"] = _images
        if _attachments:
            event_data["attachments"] = _attachments

        event_data.update(
            build_principal_stamp(
                user_id=identity.user_id,
                service_account_id=None,
            )
        )

        _file_refs = [
            {"id": f.file_id, "name": f.filename}
            for f in (getattr(msg, "files", None) or [])
            if getattr(f, "file_id", None)
        ]
        if _file_refs:
            event_data["source"]["files"] = _file_refs

        # ------------------------------------------------------------------
        # Gate 7.5: per-user token allowance (a slice of the operator's
        # subscription). Reserve this turn against the sender's cap before
        # emitting/enqueueing; an exhausted allowance (or subscription-
        # required / operator plan spent) drops the message with a notice.
        # No-op unless ops projects a positive cap for this agent, so
        # free/uncapped agents are unaffected; an unreachable allowance
        # plane fails closed (a capped agent must not serve unmetered).
        # ------------------------------------------------------------------
        if (
            identity.user_id is not None
            and deps.platform_client is not None
            and deps.runtime_config is not None
        ):
            from surogates.api.routes._commerce_turn import (
                AllowanceReserveError,
                reserve_allowance,
            )
            from surogates.runtime.platform_client import (
                AllowanceExhaustedError,
            )

            # Resolved once above; an unresolvable config is ``{}``, which
            # fails OPEN (do not gate) — only a fetched, capped payload
            # should ever block a turn, and a cache blip must not drop
            # every channel message.
            try:
                await reserve_allowance(
                    platform_client=deps.platform_client,
                    runtime_payload=runtime_payload,
                    session_store=deps.session_store,
                    session_id=session_id,
                    agent_id=routing.agent_id,
                    content=msg.text or "",
                    end_user_id=str(identity.user_id),
                    channel=routing.platform,
                )
            except AllowanceExhaustedError as exc:
                logger.info(
                    "[channels] allowance gate blocked session %s (%s)",
                    session_id,
                    exc.detail,
                )
                if deps.input_nudge is not None:
                    try:
                        await deps.input_nudge(
                            session_id,
                            msg,
                            _allowance_block_notice(
                                exc.detail,
                                runtime_payload.get("commerce_buy_url"),
                            ),
                        )
                    except Exception:
                        logger.warning(
                            "[channels] allowance-limit notice failed",
                            exc_info=True,
                        )
                return InboundOutcome.DROPPED
            except AllowanceReserveError:
                logger.warning(
                    "[channels] allowance plane unreachable — failing closed "
                    "for session %s",
                    session_id,
                    exc_info=True,
                )
                return InboundOutcome.DROPPED

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
    async def _intercept_pending_input(
        msg: InboundMessage,
        *,
        routing: Any,
        deps: PipelineDeps,
        session_id: Any,
    ) -> InboundOutcome | None:
        """Handle a message that arrives while ``ask_user_question`` waits.

        The platforms diverge on what a plain reply means:

        - Slack has a modal surface, so a plain in-thread reply is NOT the
          answer — nudge toward the Answer button and suppress the turn so
          it doesn't pile into a blocked worker.
        - Telegram has no modal; a plain reply IS the answer — resolve the
          durable pending record with it and ack.

        Returns the outcome when the message was consumed, or ``None`` to
        continue normal processing (no question pending, or a Telegram
        resolution lost the race against a button tap — the text is then a
        real user message, not an answer, and must not vanish).
        """
        try:
            pending = await deps.pending_input(session_id)
        except Exception:
            logger.warning(
                "[channels] pending input lookup failed - continuing", exc_info=True,
            )
            return None
        if not pending:
            return None

        async def _nudge(text: str) -> None:
            if deps.input_nudge is None:
                return
            try:
                await deps.input_nudge(session_id, msg, text)
            except Exception:
                logger.warning("[channels] pending input nudge failed", exc_info=True)

        if routing.platform == "slack":
            await _nudge(
                "I'm waiting on your answer - tap *Answer* above, or use the web inbox."
            )
            return InboundOutcome.DROPPED

        if not msg.text.strip():
            return None

        from surogates.channels.platforms.telegram_interactive import (
            resolve_text_answer,
        )
        from surogates.session.interactive_input import resolve_input_response

        resolved = False
        try:
            resolved = await resolve_input_response(
                deps.session_store,
                session_id=session_id,
                tool_call_id=pending.get("tool_call_id", ""),
                responses=resolve_text_answer(
                    pending.get("questions") or [], msg.text,
                ),
            )
        except Exception:
            logger.warning("[channels] pending input resolution failed", exc_info=True)
        if resolved:
            await _nudge("✅ Got it — continuing.")
            return InboundOutcome.DROPPED
        return None

    @staticmethod
    async def _maybe_send_disclosure(
        msg: InboundMessage,
        *,
        routing: Any,
        deps: PipelineDeps,
        session_id: Any,
        runtime_payload: dict | None,
    ) -> None:
        """Deliver the AI disclosure on a conversation's first contact.

        Fires only for agents whose runtime-config governance carries an
        enabled disclosure config.  "First contact" is detected via the
        session row's ``message_count`` — zero means no USER_MESSAGE has
        been recorded yet (the pipeline emits it after this hook), which
        also covers resumed-but-never-messaged sessions; a re-disclosure
        on such a session is harmless, a missing one is not.

        Delivery rides ``deps.input_nudge`` (the same seam as the /stop
        acknowledgement), which posts a plain platform message.  On
        platforms without ``post_input_nudge`` the nudge is a silent
        no-op — those channels must not be enabled for
        disclosure-required agents (see docs/governance-and-security).
        A ``disclosure.presented`` event records the delivery attempt
        with level + channel for the per-session audit trail.  Failures
        never drop the inbound message.
        """
        from surogates.runtime.governance import disclosure_config

        if runtime_payload is None or deps.input_nudge is None:
            return
        cfg = disclosure_config(runtime_payload.get("governance"))
        if cfg is None or not cfg["enabled"] or not cfg["text"]:
            return
        try:
            session = await deps.session_store.get_session(session_id)
            if getattr(session, "message_count", 0):
                return
            await deps.input_nudge(session_id, msg, cfg["text"])
            await deps.session_store.emit_event(
                session_id,
                EventType.DISCLOSURE_PRESENTED,
                {
                    "level": cfg["level"],
                    "channel": routing.platform,
                    "delivery": "channel_message",
                },
            )
        except Exception:
            logger.warning(
                "[channels] AI disclosure delivery failed for session %s",
                session_id, exc_info=True,
            )

    @staticmethod
    async def _record_reply_target(
        msg: InboundMessage,
        *,
        routing: Any,
        config: dict,
        deps: PipelineDeps,
        session_id: Any,
    ) -> None:
        """Remember which inbound message the outbound reply should attach to.

        Driven by the routing config's ``reply_to_mode`` (today only
        Telegram routings carry it): ``all`` tracks the latest message,
        ``first`` pins the session's opening message, anything else leaves
        replies unthreaded.  Groups only — replying to the only other party
        in a DM is noise.  Best-effort; never raises.
        """
        reply_to_mode = str(config.get("reply_to_mode") or "").lower()
        message_id = (msg.source or {}).get("message_id")
        if msg.is_dm or message_id is None or reply_to_mode not in ("all", "first"):
            return
        key = f"{routing.platform}_reply_to_message_id"
        try:
            if reply_to_mode == "first":
                session_row = await deps.session_store.get_session(session_id)
                if key in (session_row.config or {}):
                    return
            await deps.session_store.update_session_config_key(
                session_id, key, message_id,
            )
        except Exception:
            logger.warning(
                "[channels] recording reply-to message id failed", exc_info=True,
            )

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

        # Extra mention patterns (e.g. a nickname for a Telegram bot that
        # can't be @-mentioned by non-members): CSV in routing config,
        # matched case-insensitively on word boundaries — plain containment
        # would let "max" fire on "maximum" and invite accidental triggers.
        pattern = _mention_pattern_regex(str(config.get("mention_patterns") or ""))
        if pattern is not None and pattern.search(msg.text or ""):
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
        chat_type = resolve_chat_type(msg)
        source = SessionSource(
            platform=routing.platform,
            chat_id=msg.identifier,
            chat_type=chat_type,
            user_id=msg.platform_user_id,
            thread_id=thread_key,
        )
        key = build_session_key_for_config(source, config)
        return await deps.state.get_session(key) is not None
