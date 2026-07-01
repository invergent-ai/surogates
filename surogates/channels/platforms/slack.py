"""Slack Events API webhook channel platform strategy.

Exposes both the three stateless module-level functions used by earlier code
and the registered :class:`SlackPlatform` object that implements the
:class:`~surogates.channels.registry.ChannelPlatform` protocol.

Module-level functions
----------------------
identifier_of(request, body) -> str
    Reads the Slack app id from the URL path parameter ``app_id``.  The path
    is the authoritative identifier because:

    - The dispatcher resolves credentials *before* parsing the body.
    - Slack's ``url_verification`` handshake body carries no ``api_app_id``,
      so the path is the only reliable source.

verify(request, raw_body, *, creds) -> bool | VerificationResult
    Validates the Slack request signature (HMAC-SHA256 over
    ``v0:{timestamp}:{raw_body}``) and enforces a ±5-minute replay window.

    Special cases *after* the signature check passes:

    - ``url_verification`` → returns :class:`VerificationResult` with the
      challenge echoed back; the strategy must return this response verbatim.
    - ``event_callback`` → additionally cross-checks ``body["api_app_id"]``
      against the path ``app_id``; mismatch → ``False``.

    A bad signature or stale timestamp returns ``False``.

parse(body, *, bot_user_id) -> InboundMessage | None
    Unwraps an ``event_callback`` body and maps ``message`` / ``app_mention``
    events to :class:`~surogates.channels.inbound.InboundMessage`.

    Returns ``None`` for:
    - Bot-authored messages (``bot_id`` present or ``subtype == "bot_message"``).
    - Edit / delete subtypes (``message_changed``, ``message_deleted``).
    - Non-message event types (reactions, member joins, …).
    - Non-``event_callback`` bodies (``url_verification``, etc.).
    - Events with no ``user`` field.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import logging
import mimetypes
import time
from typing import Any

import httpx

try:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient
except ImportError:
    AsyncWebClient = Any  # type: ignore[misc,assignment]
    SlackApiError = Exception  # type: ignore[misc,assignment]

from surogates.channels.errors import ChannelApiError

from fastapi.responses import Response

from surogates.channels.base import SendResult
from surogates.channels.channel_backfill import (
    BackfillLimits,
    ChannelMeta,
    RawMessage,
    filter_messages,
    warm_cache as _warm_cache_fn,
)
from surogates.channels.inbound import InboundFileRef, InboundMessage
from surogates.channels.registry import ChannelDescriptor, VerificationResult


def _form_timestamp() -> str:
    """Return a monotonic timestamp string suitable for deduplication.

    Uses ``time.time_ns()`` (nanosecond integer) formatted as a dotted
    decimal to match the Slack ``ts`` convention.  Called only when the
    slash-command form body carries no ``ts`` field of its own.
    """
    ns = time.time_ns()
    seconds, frac = divmod(ns, 1_000_000_000)
    return f"{seconds}.{frac:09d}"

__all__ = [
    "SlackPlatform",
    "identifier_of",
    "verify",
    "parse",
]

logger = logging.getLogger(__name__)

# files.info error codes that mean the file is genuinely gone (-> not found),
# the bot cannot access it (-> forbidden), or Slack is throttling (-> retry).
_FILE_MISSING_CODES = frozenset({"file_not_found", "file_deleted"})
_FORBIDDEN_CODES = frozenset({
    "not_in_channel",
    "access_denied",
    "file_not_visible",
    "not_allowed_token_type",
    "no_permission",
})
_RATE_LIMIT_CODES = frozenset({"ratelimited", "rate_limited"})


def _slack_error_code(exc: SlackApiError) -> str:
    """Extract the ``error`` string from a SlackApiError's response (or "")."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        return resp.get("error") or ""
    except Exception:
        return ""


# Replay-guard window in seconds (±5 minutes matches Slack's recommendation).
_TIMESTAMP_TOLERANCE = 300


# ---------------------------------------------------------------------------
# identifier_of
# ---------------------------------------------------------------------------


def identifier_of(request: Any, body: Any) -> str:
    """Return the Slack app id from the URL path parameter.

    The path is the source of truth; the body is intentionally ignored so
    this works for ``url_verification`` requests (which carry no
    ``api_app_id``) and is safe to call before the body is parsed.
    """
    return request.path_params["app_id"]


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify(
    request: Any,
    raw_body: bytes,
    *,
    creds: dict,
) -> bool | VerificationResult:
    """Validate a Slack webhook request.

    Parameters
    ----------
    request:
        Starlette-like request object.  Must expose ``path_params["app_id"]``
        and ``headers`` (case-insensitive mapping or plain dict with
        lowercase keys).
    raw_body:
        Raw request body bytes — must be the *original* bytes, not
        re-serialised from a parsed dict.
    creds:
        Credential dict with ``signing_secret`` and ``bot_token`` keys.

    Returns
    -------
    True
        Signature valid; proceed to ``parse``.
    False
        Signature invalid, timestamp stale, or ``api_app_id`` mismatch.
    VerificationResult
        Returned for ``url_verification`` challenges so the dispatcher can
        echo the challenge to Slack.
    """
    # Treat a missing OR null signing secret (unconfigured credential) as a
    # hard reject: we cannot verify the signature without it.  A bare
    # ``.get(..., "")`` would return ``None`` for ``{"signing_secret": None}``
    # and then crash on ``None.encode(...)``, surfacing as an unhandled 500.
    signing_secret: str = creds.get("signing_secret") or ""
    if not signing_secret:
        logger.debug("verify: no signing_secret configured — rejecting")
        return False

    headers = request.headers

    # ------------------------------------------------------------------
    # 1. Timestamp replay guard.
    # ------------------------------------------------------------------
    ts_str = _header(headers, "x-slack-request-timestamp")
    try:
        ts = int(ts_str)
    except (ValueError, TypeError):
        logger.debug("verify: missing or malformed X-Slack-Request-Timestamp")
        return False

    now = int(time.time())
    if abs(now - ts) > _TIMESTAMP_TOLERANCE:
        logger.debug(
            "verify: timestamp %s outside ±%ds window (now=%s)",
            ts, _TIMESTAMP_TOLERANCE, now,
        )
        return False

    # ------------------------------------------------------------------
    # 2. HMAC-SHA256 signature check (constant-time compare).
    #
    # Slack signs the RAW request bytes, so build the base string at the byte
    # level (``v0:{timestamp}:{raw_body}``) rather than decoding/re-encoding
    # the body — this matches Slack exactly and avoids any non-UTF-8 edge.
    # ------------------------------------------------------------------
    sig_header = _header(headers, "x-slack-signature")
    basestring = b"v0:" + ts_str.encode("ascii") + b":" + raw_body
    expected_mac = hmac.new(
        signing_secret.encode("utf-8"),
        basestring,
        hashlib.sha256,
    )
    expected_sig = f"v0={expected_mac.hexdigest()}"

    if not hmac.compare_digest(expected_sig, sig_header or ""):
        logger.debug("verify: signature mismatch")
        return False

    # ------------------------------------------------------------------
    # 3. Parse body (only after signature passes).
    #
    # For JSON Events API requests the body is JSON and we can do deeper
    # checks (url_verification echo, api_app_id crosscheck).  For
    # form-encoded requests (slash commands, interactivity) the body is
    # application/x-www-form-urlencoded — not JSON.  In that case the
    # signature already proved authenticity; we return True immediately
    # with no further checks (there is nothing to crosscheck from form
    # fields before the caller has parsed the form).
    # ------------------------------------------------------------------
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        # Signature passed; body is not JSON (likely form-encoded).
        # Return True — the caller is responsible for parsing the form.
        return True

    body_type = body.get("type", "")

    # ------------------------------------------------------------------
    # 4. url_verification handshake — echo the challenge.
    # ------------------------------------------------------------------
    if body_type == "url_verification":
        challenge = body.get("challenge", "")
        return VerificationResult(
            accepted=True,
            response_body={"challenge": challenge},
            status_code=200,
        )

    # ------------------------------------------------------------------
    # 5. event_callback — cross-check api_app_id against path.
    # ------------------------------------------------------------------
    if body_type == "event_callback":
        path_app_id = identifier_of(request, body)
        body_app_id = body.get("api_app_id", "")
        if body_app_id != path_app_id:
            logger.debug(
                "verify: api_app_id mismatch — path=%s body=%s",
                path_app_id, body_app_id,
            )
            return False

    return True


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def parse(body: dict, *, bot_user_id: str) -> InboundMessage | None:
    """Convert a Slack ``event_callback`` body to an :class:`InboundMessage`.

    The ``bot_user_id`` parameter is required to determine ``is_mention``
    (whether ``<@{bot_user_id}>`` appears in the event text).  The strategy
    is responsible for resolving and caching this value (e.g. via
    ``auth.test`` keyed by ``bot_token``).

    Parameters
    ----------
    body:
        Parsed Slack JSON payload.
    bot_user_id:
        The Slack user-id of the bot (e.g. ``"U0BOTUSER"``).

    Returns
    -------
    InboundMessage
        When the event is a processable user message.
    None
        When the event should be silently dropped (bot message, edit,
        delete, non-message type, url_verification, …).
    """
    # Only handle event_callback bodies.
    if body.get("type") != "event_callback":
        return None

    event = body.get("event", {})
    event_type = event.get("type", "")

    # Only process message and app_mention events.
    if event_type not in ("message", "app_mention"):
        return None

    # ------------------------------------------------------------------
    # Gate: bot messages.
    # FIX 5: Drop OWN bot's messages (user == bot_user_id).
    # For OTHER bots (bot_id present / subtype==bot_message but user !=
    # bot_user_id), do NOT drop — return InboundMessage with is_bot=True
    # so the pipeline can apply the allow_bots gate.
    # ------------------------------------------------------------------
    bot_id = event.get("bot_id")
    subtype = event.get("subtype", "")
    user_id_raw: str = event.get("user", "")

    # ------------------------------------------------------------------
    # Loop safety: drop anything authored by our OWN bot user, however Slack
    # marks it — a bot_message, or the bare ``channel_join`` Slack emits (with
    # the bot's user id but no ``bot_id``) when the agent is invited.  Without
    # this unconditional drop the agent would react to its own join/messages.
    # ------------------------------------------------------------------
    if bot_user_id and user_id_raw == bot_user_id:
        return None

    is_bot: bool = False
    if bot_id or subtype == "bot_message":
        # Can't identify our own bot id (resolution failed → empty string) →
        # drop for safety; our own messages are already dropped above.
        if not bot_user_id:
            return None
        # Another bot's message. If there's no user field we can't route it.
        if not user_id_raw:
            return None
        is_bot = True

    # ------------------------------------------------------------------
    # Gate: non-message system subtypes (edits, deletions, member join/leave)
    # are not user messages to respond to.
    # ------------------------------------------------------------------
    if subtype in (
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "group_join",
        "group_leave",
    ):
        return None

    # ------------------------------------------------------------------
    # Extract message fields from the inbound Slack event.
    # ------------------------------------------------------------------
    user_id: str = user_id_raw
    if not user_id:
        return None

    text: str = event.get("text", "").strip()
    channel_id: str = event.get("channel", "")
    channel_type: str = event.get("channel_type", "")
    ts: str = event.get("ts", "")
    event_thread_ts: str | None = event.get("thread_ts")

    # DM detection: channel_type "im" or "mpim".
    is_dm = channel_type in ("im", "mpim")
    if is_dm:
        visibility = "dm"
    elif channel_type == "channel":
        visibility = "public"
    else:
        visibility = "private"  # "group" (private channel) or unknown → fail closed

    # Thread key resolution mirrors the Socket Mode adapter:
    #   - DM:      use thread_ts if present, else None (top-level DM has no thread)
    #   - Channel: use thread_ts if present, else ts (every channel msg starts a thread)
    if is_dm:
        thread_key = event_thread_ts or None
    else:
        thread_key = event_thread_ts or ts

    # Mention detection.
    is_mention = bool(bot_user_id) and f"<@{bot_user_id}>" in text

    # api_app_id from the outer wrapper (used as the platform app identifier).
    api_app_id: str = body.get("api_app_id", "")

    # ------------------------------------------------------------------
    # File attachments: surface as text marker + media_urls so nothing
    # is silently dropped.  Full download/re-hosting is a follow-up task.
    # ------------------------------------------------------------------
    media_urls: list[str] = []
    file_names: list[str] = []
    files: list[InboundFileRef] = []

    for file_info in event.get("files", []):
        url = file_info.get("url_private_download") or file_info.get("url_private", "")
        if not url:
            continue
        name = file_info.get("name", "file")
        mime = file_info.get("mimetype") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        size = file_info.get("size")
        media_urls.append(url)
        file_names.append(name)
        files.append(InboundFileRef(
            url=url, filename=name, mime_type=mime,
            size=int(size) if isinstance(size, int) else None,
            file_id=file_info.get("id"),
        ))

    if file_names:
        from surogates.session.attachment_ingest import safe_display_name
        names_str = ", ".join(safe_display_name(n) for n in file_names)
        marker = f"\n[shared {len(file_names)} file(s): {names_str}]"
        text = text + marker if text else marker

    media_types: list[str] = []

    return InboundMessage(
        kind="text",
        identifier=channel_id,
        thread_key=thread_key,
        platform_user_id=user_id,
        user_name=user_id,  # Strategy resolves display name asynchronously.
        text=text,
        media_urls=media_urls,
        media_types=media_types,
        is_dm=is_dm,
        is_mention=is_mention,
        ts=ts,
        source={
            "platform": "slack",
            "api_app_id": api_app_id,
            "channel_type": channel_type,
        },
        is_bot=is_bot,
        visibility=visibility,
        files=files,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup for dict or Starlette Headers."""
    if hasattr(headers, "get"):
        # Starlette Headers is case-insensitive; plain dicts may need lowercasing.
        value = headers.get(name) or headers.get(name.lower())
        return value
    return None


# ---------------------------------------------------------------------------
# SlackPlatform — ChannelPlatform implementation
# ---------------------------------------------------------------------------


class SlackPlatform:
    """Webhook-based Slack channel platform strategy.

    Implements :class:`~surogates.channels.registry.ChannelPlatform`.

    Each Slack app is identified by its app id, which is the ``{app_id}``
    path parameter in the webhook URL.  Credentials (``bot_token`` and
    ``signing_secret``) are resolved by the dispatcher and passed to every
    method that requires them.

    Instance caches (keyed by bot_token)
    -------------------------------------
    ``_bot_user_id_cache``:
        ``{bot_token: user_id}`` — populated lazily by the first ``parse``
        call for a given token via ``auth.test``.
    ``_user_name_cache``:
        ``{(bot_token, user_id): display_name}`` — populated lazily by
        ``enrich`` via ``users_info``.
    """

    kind = "slack"
    topology = "webhook"

    _THINKING_TEXT = "_Thinking…_"

    interactive_paths: tuple[str, ...] = (
        "/slack/{app_id}/interact",
        "/slack/{app_id}/commands",
    )

    descriptor = ChannelDescriptor(
        vault_refs=lambda identifier: {
            "bot_token": "bot_token",
            "signing_secret": "signing_secret",
        },
        config_keys=(
            "require_mention",
            "free_response_channels",
            "allow_bots",
            "reply_in_thread",
            "reply_broadcast",
        ),
        webhook_registration="manual",
    )

    def __init__(self) -> None:
        # Cache keyed by bot_token → AsyncWebClient instance.
        # SlackPlatform instances are process-lifetime singletons; no
        # explicit close is needed (the SDK does not hold open sockets
        # between API calls).
        self._clients: dict[str, AsyncWebClient] = {}
        # Cache keyed by bot_token → bot user_id (from auth.test).
        self._bot_user_id_cache: dict[str, str] = {}
        # Cache keyed by (bot_token, platform_user_id) → display_name.
        self._user_name_cache: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------
    # Route path
    # ------------------------------------------------------------------

    def route_path(self, identifier: str | None = None) -> str:
        """Return the FastAPI path for this platform.

        Parameters
        ----------
        identifier:
            Slack app id.  When ``None`` (template form used by
            ``build_app``), returns the parametrised path template.
        """
        if identifier is None:
            return "/slack/{app_id}"
        return f"/slack/{identifier}"

    # ------------------------------------------------------------------
    # identifier_of / verify — delegates to module functions
    # ------------------------------------------------------------------

    def identifier_of(self, request: Any, body: Any) -> str:
        return identifier_of(request, body)

    def verify(
        self, request: Any, raw_body: bytes, *, creds: dict
    ) -> bool | VerificationResult:
        return verify(request, raw_body, creds=creds)

    # ------------------------------------------------------------------
    # parse — async; requires bot_user_id from auth.test (cached)
    # ------------------------------------------------------------------

    async def parse(
        self,
        body: Any,
        *,
        creds: dict | None = None,
        identifier: str | None = None,
    ) -> InboundMessage | None:
        """Parse a Slack event_callback body into an :class:`InboundMessage`.

        Resolves the bot user id via ``auth.test`` (cached per bot token)
        so that mention detection (``<@{bot_user_id}>`` in text) works
        correctly.

        Parameters
        ----------
        body:
            Parsed Slack JSON payload.
        creds:
            Credential dict with at least ``bot_token``.  When ``None``
            (e.g. called directly from tests without creds), mention
            detection is skipped (``bot_user_id=""``).
        identifier:
            The resolved path identifier (Slack app id) — accepted but
            ignored because Slack's bot_user_id is obtained via ``auth.test``
            and is NOT the same as the app id.
        """
        bot_token: str = (creds or {}).get("bot_token") or ""
        bot_user_id = await self._resolve_bot_user_id(bot_token)
        return parse(body, bot_user_id=bot_user_id)

    def _get_client(self, bot_token: str) -> AsyncWebClient:
        """Return a cached :class:`AsyncWebClient` for *bot_token*.

        One client is created per distinct token and reused for all calls
        (parse, send, enrich).  SlackPlatform instances are process-lifetime
        singletons so no explicit close is needed.
        """
        if bot_token not in self._clients:
            self._clients[bot_token] = AsyncWebClient(token=bot_token)
        return self._clients[bot_token]

    async def _resolve_bot_user_id(self, bot_token: str) -> str:
        """Return the bot's Slack user id, resolved once per token via auth.test."""
        if not bot_token:
            return ""
        if bot_token in self._bot_user_id_cache:
            return self._bot_user_id_cache[bot_token]
        try:
            client = self._get_client(bot_token)
            result = await client.auth_test()
            user_id: str = result.get("user_id") or ""
            self._bot_user_id_cache[bot_token] = user_id
            return user_id
        except Exception:
            logger.debug("SlackPlatform: auth.test failed for token — skipping bot_user_id")
            return ""

    # ------------------------------------------------------------------
    # handle_non_message_update — warm cache on bot self-join
    # ------------------------------------------------------------------

    async def handle_non_message_update(
        self, body, *, routing, creds, deps,
    ) -> bool:
        """Warm the channel-context cache when THIS bot is added to a channel.

        Returns True (ACK, skip parse) only when we handled a bot self-join;
        all other non-message events fall through (return False).
        """
        if (body or {}).get("type") != "event_callback":
            return False
        event = body.get("event", {})
        if event.get("type") != "member_joined_channel":
            return False
        bot_token = (creds or {}).get("bot_token") or ""
        bot_user_id = await self._resolve_bot_user_id(bot_token)
        if not bot_user_id or event.get("user") != bot_user_id:
            return False  # someone else joined — not our concern

        channel_id = event.get("channel") or ""
        if not channel_id:
            return True
        limits = BackfillLimits.from_config(
            (getattr(routing, "config", None) or {}).get("history_backfill")
        )
        warm = getattr(self, "_warm_cache", None) or _warm_cache_fn
        try:
            await warm(
                platform=self, creds=creds, redis=deps.redis,
                org_id=routing.org_id, agent_id=routing.agent_id,
                identifier=routing.identifier, channel_id=channel_id,
                limits=limits, now=time.time())
        except Exception:
            logger.warning(
                "member_joined backfill warm failed for %s", channel_id, exc_info=True,
            )
        return True

    # ------------------------------------------------------------------
    # handle_interactive — slash commands and Block Kit interactions
    # ------------------------------------------------------------------

    async def handle_interactive(
        self,
        path_template: str,
        form: dict,
        *,
        request: Any,
        creds: dict,
        routing: Any,
        deps: Any = None,
    ) -> InboundMessage | Any:
        """Handle a form-encoded interactive Slack request.

        Dispatches on *path_template* to one of two sub-handlers:

        ``/slack/{app_id}/commands``
            Slash command.  Builds a synthetic :class:`InboundMessage` so the
            inbound pipeline processes it as a DM from the issuing user.  An
            empty (or whitespace-only) text field returns a
            :class:`~fastapi.responses.PlainTextResponse` with usage guidance
            instead — Slack displays it ephemerally to the user.

        ``/slack/{app_id}/interact``
            Block Kit button clicks / modal submissions.  Opens a modal for
            pending input questions on ``block_actions`` with the answer
            action, and resolves the modal submission on ``view_submission``.

        Parameters
        ----------
        path_template:
            The FastAPI route path template that matched this request, e.g.
            ``"/slack/{app_id}/commands"``.
        form:
            Parsed ``application/x-www-form-urlencoded`` body as a plain dict
            (keys and values are strings).
        request:
            Starlette-like request object (used for path_params if needed).
        creds:
            Resolved credential dict (``bot_token``, ``signing_secret``).
        routing:
            Routing object from the dispatcher (may be ``None`` in tests).
        deps:
            Pipeline dependencies bundle (carries ``session_store``).

        Returns
        -------
        InboundMessage
            When the event should be forwarded through the inbound pipeline.
        Response
            When the event has been fully handled (usage hint or ack).
        None
            When the event should be silently acked with no side effects.
        """
        if path_template.endswith("/commands"):
            return await self._handle_slash(form)
        if path_template.endswith("/interact"):
            return await self._handle_interact(form, creds=creds, deps=deps)
        logger.debug(
            "[SlackPlatform] Unknown interactive path template %r - acking silently",
            path_template,
        )
        return Response(status_code=200)

    async def _handle_slash(self, form: dict) -> InboundMessage | Any:
        """Handle a /surogates slash command form payload.

        Returns a :class:`InboundMessage` for non-empty text so the inbound
        pipeline processes it as a DM, or a :class:`PlainTextResponse` for
        empty/whitespace-only text (Slack shows it ephemerally to the user).
        """
        from fastapi.responses import PlainTextResponse

        text: str = form.get("text", "").strip()
        channel_id: str = form.get("channel_id", "")
        user_id: str = form.get("user_id", "")
        team_id: str = form.get("team_id", "")

        if not text:
            return PlainTextResponse("Usage: /surogates <message>", status_code=200)

        # Use the form's own timestamp if Slack provides one; otherwise derive
        # a monotonic counter-style string from the current epoch with enough
        # granularity to serve as a dedup key without importing time at module
        # level (avoids the "don't call time.time() at import" anti-pattern).
        ts: str = form.get("ts", "") or _form_timestamp()

        return InboundMessage(
            kind="text",
            identifier=channel_id,
            thread_key=None,
            platform_user_id=user_id,
            user_name=user_id,  # Enrich step resolves display name asynchronously.
            text=text,
            media_urls=[],
            media_types=[],
            is_dm=True,
            is_mention=False,
            ts=ts,
            source={
                "platform": "slack",
                "channel_type": "im",
                "team": team_id,
                "via": "slash_command",
            },
            visibility="dm",
        )

    async def _handle_interact(
        self,
        form: dict,
        *,
        creds: dict | None = None,
        deps: Any = None,
    ) -> Any:
        """Handle a Block Kit button-click or modal-submission payload.

        ``block_actions`` with the answer action_id: opens a modal with the
        pending question(s) for the session.  ``view_submission`` with the
        modal callback_id: resolves the response, or returns Slack field errors
        to keep the modal open when validation fails.

        Everything is best-effort — any exception is caught and logged; Slack
        always receives a 200 ack.
        """
        from fastapi.responses import JSONResponse
        from surogates.channels.platforms.slack_interactive import (
            ANSWER_ACTION_ID,
            MODAL_CALLBACK_ID,
            ModalErrors,
            build_question_modal,
            parse_modal_submission,
        )
        from surogates.session import interactive_input

        try:
            payload = json.loads(form.get("payload", "{}"))
        except (json.JSONDecodeError, ValueError):
            logger.debug("[SlackPlatform] /interact payload is not valid JSON - acking silently")
            return Response(status_code=200)

        if deps is None:
            return Response(status_code=200)

        payload_type = payload.get("type")
        if payload_type == "block_actions":
            action = next(
                (
                    a for a in payload.get("actions", [])
                    if a.get("action_id") == ANSWER_ACTION_ID
                ),
                None,
            )
            if action is None:
                return Response(status_code=200)
            try:
                ref = json.loads(action.get("value") or "{}")
                session_id = ref.get("session_id") or ""
                tool_call_id = ref.get("tool_call_id") or ""
                pending = await interactive_input.pending_input_for_session(
                    deps.session_store,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                )
                if not pending:
                    return Response(status_code=200)
                view = build_question_modal(
                    session_id=session_id,
                    tool_call_id=pending["tool_call_id"],
                    questions=pending["questions"],
                )
                await self._get_client((creds or {}).get("bot_token") or "").views_open(
                    trigger_id=payload.get("trigger_id"),
                    view=view,
                )
            except Exception:
                logger.warning("[SlackPlatform] /interact views_open failed", exc_info=True)
            return Response(status_code=200)

        if payload_type == "view_submission":
            view = payload.get("view") or {}
            if view.get("callback_id") != MODAL_CALLBACK_ID:
                return Response(status_code=200)
            try:
                meta = json.loads(view.get("private_metadata") or "{}")
                pending = await interactive_input.pending_input_for_session(
                    deps.session_store,
                    session_id=meta.get("session_id") or "",
                    tool_call_id=meta.get("tool_call_id") or "",
                )
                if not pending:
                    return Response(status_code=200)
                parsed = parse_modal_submission(view, pending["questions"])
                if isinstance(parsed, ModalErrors):
                    return JSONResponse(parsed.to_response(), status_code=200)
                await interactive_input.resolve_input_response(
                    deps.session_store,
                    session_id=parsed.session_id,
                    tool_call_id=parsed.tool_call_id,
                    responses=parsed.responses,
                )
            except Exception:
                logger.warning("[SlackPlatform] /interact view_submission resolve failed", exc_info=True)
            return Response(status_code=200)

        return Response(status_code=200)

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    async def post_input_nudge(
        self,
        *,
        creds: dict,
        channel: str,
        thread_ts: Any,
        text: str,
    ) -> str | None:
        """Post a nudge to prompt the user to use the Answer button; return its ts or None."""
        bot_token: str = (creds or {}).get("bot_token") or ""
        if not bot_token or not channel:
            return None
        kwargs: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            resp = await self._get_client(bot_token).chat_postMessage(**kwargs)
            return resp.get("ts")
        except Exception:
            logger.warning("[SlackPlatform] post_input_nudge failed for %s", channel, exc_info=True)
            return None

    async def post_thinking_placeholder(
        self, *, creds: dict, channel: str, thread_ts,
    ) -> str | None:
        """Post the '_Thinking…_' placeholder; return its ts, or None on error."""
        bot_token: str = (creds or {}).get("bot_token") or ""
        if not bot_token or not channel:
            return None
        kwargs: dict = {"channel": channel, "text": self._THINKING_TEXT}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            resp = await self._get_client(bot_token).chat_postMessage(**kwargs)
            return resp.get("ts")
        except Exception:
            logger.warning("[SlackPlatform] post_thinking_placeholder failed for %s", channel, exc_info=True)
            return None

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        """Post an outbox item to Slack via ``chat.postMessage``.

        When ``item.destination["update_ts"]`` is set, edits that message via
        ``chat.update`` instead of posting a fresh message.  Falls back to a
        fresh ``chat.postMessage`` if the edit fails so the reply still lands.

        Parameters
        ----------
        item:
            Outbox item with ``destination`` (``channel_id``, optional
            ``thread_ts``, optional ``update_ts``) and ``payload`` (``content``).
        creds:
            Credential dict with ``bot_token``.
        """
        bot_token: str = creds.get("bot_token") or ""
        client = self._get_client(bot_token)

        channel_id: str = item.destination.get("channel_id", "")

        if item.payload.get("input_prompt"):
            from surogates.channels.platforms.slack_interactive import build_input_prompt_blocks

            text, blocks = build_input_prompt_blocks(
                session_id=str(getattr(item, "session_id", "")),
                tool_call_id=item.payload.get("tool_call_id", ""),
                questions=item.payload.get("questions") or [],
                context=item.payload.get("context", ""),
            )
            thread_ts: str | None = item.destination.get("thread_ts")
            update_ts = item.destination.get("update_ts")
            post_kwargs: dict[str, Any] = {
                "channel": channel_id,
                "text": text,
                "blocks": blocks,
            }
            if thread_ts:
                post_kwargs["thread_ts"] = thread_ts
            if update_ts:
                try:
                    edited = await client.chat_update(
                        channel=channel_id,
                        ts=update_ts,
                        text=text,
                        blocks=blocks,
                    )
                    return SendResult(success=True, message_id=edited.get("ts") or update_ts)
                except Exception as exc:
                    logger.warning(
                        "[SlackPlatform] input prompt chat_update failed (%s); posting fresh",
                        exc,
                    )
            try:
                posted = await client.chat_postMessage(**post_kwargs)
                return SendResult(success=True, message_id=posted.get("ts"))
            except Exception as exc:
                logger.error("[SlackPlatform] input prompt chat_postMessage failed: %s", exc)
                return SendResult(success=False, error=str(exc))

        text: str = item.payload.get("content", "")
        thread_ts: str | None = item.destination.get("thread_ts")

        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "text": text,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        update_ts = item.destination.get("update_ts")
        if update_ts:
            try:
                edited = await client.chat_update(channel=channel_id, ts=update_ts, text=text)
                return SendResult(success=True, message_id=edited.get("ts") or update_ts)
            except Exception as exc:
                logger.warning(
                    "[SlackPlatform] chat_update failed (%s); posting a fresh message", exc,
                )
                # fall through to a fresh post so the reply still lands

        try:
            result = await client.chat_postMessage(**kwargs)
            return SendResult(success=True, message_id=result.get("ts"))
        except Exception as exc:
            logger.error("[SlackPlatform] chat_postMessage failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    # ------------------------------------------------------------------
    # download_file — fetch a private Slack file URL
    # ------------------------------------------------------------------

    async def download_file(
        self, *, creds: dict, url: str, max_bytes: int,
    ) -> bytes | None:
        """Download a Slack file. url_private requires the bot token as Bearer
        auth. Returns the bytes, or None on missing token, non-2xx, timeout,
        Content-Length over cap, or body over cap (never raises)."""
        bot_token = (creds or {}).get("bot_token") or ""
        if not bot_token or not url:
            return None
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        if host != "slack.com" and not host.endswith(".slack.com"):
            logger.warning("[SlackPlatform] download_file refusing non-slack host: %s", host)
            return None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {bot_token}"})
                if resp.status_code < 200 or resp.status_code >= 300:
                    logger.warning("[SlackPlatform] download_file %s -> HTTP %s", url, resp.status_code)
                    return None
                cl = resp.headers.get("Content-Length")
                if cl is not None and cl.isdigit() and int(cl) > max_bytes:
                    logger.warning("[SlackPlatform] download_file %s over cap (Content-Length=%s)", url, cl)
                    return None
                data = resp.content
                if len(data) > max_bytes:
                    logger.warning("[SlackPlatform] download_file %s body over cap (%d bytes)", url, len(data))
                    return None
                return data
        except Exception:
            logger.warning("[SlackPlatform] download_file failed for %s", url, exc_info=True)
            return None

    async def fetch_file_meta(
        self, *, creds: dict, file_id: str,
    ) -> dict | None:
        """Return Slack ``files.info`` metadata for *file_id*.

        The returned object carries ``url_private_download``, ``name``,
        ``mimetype``, ``size`` and the membership info used to enforce that the
        file was shared in the agent's own channel.  Returns None when the file
        genuinely does not exist (missing token/file_id, or Slack
        ``file_not_found``/``file_deleted``).  Raises :class:`ChannelApiError`
        for access-denied (``"forbidden"``), rate-limit (``"rate_limited"``)
        and transient/unknown (``"unavailable"``) failures so the caller can
        surface a precise status instead of a blanket "not found".
        """
        bot_token = (creds or {}).get("bot_token") or ""
        if not bot_token or not file_id:
            return None
        client = self._get_client(bot_token)
        try:
            resp = await client.files_info(file=file_id)
        except SlackApiError as exc:
            code = _slack_error_code(exc)
            if code in _FILE_MISSING_CODES:
                return None
            if code in _FORBIDDEN_CODES:
                raise ChannelApiError("forbidden", code)
            if code in _RATE_LIMIT_CODES:
                raise ChannelApiError("rate_limited", code)
            logger.warning(
                "[SlackPlatform] files_info error %r for %s", code, file_id,
            )
            raise ChannelApiError("unavailable", code or "files_info_failed")
        except ChannelApiError:
            raise
        except Exception:
            logger.warning(
                "[SlackPlatform] files_info failed for %s", file_id, exc_info=True,
            )
            raise ChannelApiError("unavailable", "files_info_failed")
        file_obj = resp.get("file")
        return file_obj if file_obj else None

    async def list_channel_files(
        self, *, creds: dict, channel_id: str, max_pages: int = 3,
    ) -> list[dict]:
        """List files shared in *channel_id* (Slack ``files.list``), across
        threads too. Bounded by *max_pages*. Needs the ``files:read`` scope.
        Returns the raw file objects (each with ``id``/``name``/``title``/
        ``created``). Raises :class:`ChannelApiError` for forbidden/rate-limit;
        returns ``[]`` when the token/channel is missing or the call fails
        transiently.
        """
        bot_token = (creds or {}).get("bot_token") or ""
        if not bot_token or not channel_id:
            return []
        client = self._get_client(bot_token)
        out: list[dict] = []
        page = 1
        while page <= max(1, max_pages):
            try:
                resp = await client.files_list(channel=channel_id, count=100, page=page)
            except SlackApiError as exc:
                code = _slack_error_code(exc)
                if code in _RATE_LIMIT_CODES:
                    raise ChannelApiError("rate_limited", code)
                if code in _FORBIDDEN_CODES:
                    raise ChannelApiError("forbidden", code)
                logger.warning("[SlackPlatform] files_list error %r for %s", code, channel_id)
                break
            except ChannelApiError:
                raise
            except Exception:
                logger.warning("[SlackPlatform] files_list failed for %s", channel_id, exc_info=True)
                break
            out.extend(resp.get("files") or [])
            pages = (resp.get("paging") or {}).get("pages") or 1
            if page >= pages:
                break
            page += 1
        return out

    # ------------------------------------------------------------------
    # send_files — upload workspace files referenced by MEDIA: markers
    # ------------------------------------------------------------------

    async def send_files(
        self, item: Any, *, creds: dict, files: list,
    ) -> list[str]:
        """Upload *files* to the item's Slack channel via ``files_upload_v2``.

        Each file is uploaded into ``destination["thread_ts"]`` when present.
        Returns the uploaded Slack file ids (empty when nothing uploaded).
        Best-effort per file: any error is logged and skipped, never raised —
        the same contract as ``download_file``.
        """
        bot_token: str = (creds or {}).get("bot_token") or ""
        if not bot_token or not files:
            return []
        client = self._get_client(bot_token)
        channel_id: str = item.destination.get("channel_id", "")
        thread_ts = item.destination.get("thread_ts")
        uploaded: list[str] = []
        for f in files:
            kwargs: dict[str, Any] = {
                "channel": channel_id,
                "content": f.data,
                "filename": f.filename,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            try:
                resp = await client.files_upload_v2(**kwargs)
                file_id = _uploaded_file_id(resp)
            except Exception as exc:
                logger.warning(
                    "[SlackPlatform] files_upload_v2 failed for %s: %s", f.filename, exc,
                )
                continue
            if file_id:
                uploaded.append(file_id)
        return uploaded

    # ------------------------------------------------------------------
    # delete_message — best-effort chat.delete (placeholder cleanup)
    # ------------------------------------------------------------------

    async def delete_message(self, *, creds: dict, channel: str, ts: str) -> None:
        """Delete a message via ``chat.delete``. Best-effort; never raises."""
        bot_token: str = (creds or {}).get("bot_token") or ""
        if not bot_token or not channel or not ts:
            return
        try:
            await self._get_client(bot_token).chat_delete(channel=channel, ts=ts)
        except Exception as exc:
            logger.warning(
                "[SlackPlatform] chat_delete failed for %s/%s: %s", channel, ts, exc,
            )

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
        """Privately deliver *text* to *sender_id* by opening a DM.

        Returns ``True`` on delivery, ``False`` on any error (never raises) —
        the caller withholds the code if private delivery fails (so a usable
        code is never printed into a shared channel).
        """
        bot_token: str = creds.get("bot_token") or ""
        client = self._get_client(bot_token)
        try:
            opened = await client.conversations_open(users=sender_id)
            dm_channel = opened["channel"]["id"]
            await client.chat_postMessage(channel=dm_channel, text=text)
            return True
        except Exception as exc:
            logger.error("[SlackPlatform] send_private failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # enrich — async user name resolution
    # ------------------------------------------------------------------

    async def enrich(
        self, msg: InboundMessage, *, creds: dict
    ) -> InboundMessage:
        """Resolve the sender's display name and return an enriched message.

        Uses ``users_info`` keyed by ``(bot_token, platform_user_id)`` with
        an in-process LRU-style cache.  Falls back to the raw user id on any
        error.

        Parameters
        ----------
        msg:
            Parsed inbound message (frozen dataclass).
        creds:
            Credential dict with ``bot_token``.
        """
        bot_token: str = creds.get("bot_token") or ""
        user_id = msg.platform_user_id
        display_name = await self._resolve_user_name(bot_token, user_id)
        if display_name == msg.user_name:
            return msg
        return dataclasses.replace(msg, user_name=display_name)

    async def fetch_channel_context(
        self, *, creds: dict, channel_id: str, limits: BackfillLimits,
    ) -> tuple[ChannelMeta, list[RawMessage]] | None:
        """Fetch channel metadata + recent history (newest-first).

        Returns None for DMs/MPDMs (v1 is channel-only), when the bot is not a
        member, or on any Slack error. Bounding by count/token/age is the
        coordinator's job; here we only honour the page + time budgets.
        """
        bot_token: str = (creds or {}).get("bot_token") or ""
        if not bot_token:
            return None
        client = self._get_client(bot_token)

        def _retry_after_seconds(exc: Exception) -> float | None:
            response = getattr(exc, "response", None)
            headers = {}
            if isinstance(response, dict):
                headers = response.get("headers") or {}
            else:
                headers = getattr(response, "headers", {}) or {}
            raw = headers.get("Retry-After") or headers.get("retry-after")
            try:
                return float(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        try:
            info_resp = await client.conversations_info(channel=channel_id)
            # AsyncWebClient returns an AsyncSlackResponse (dict-like .get(), but
            # NOT a dict), so never gate field access on isinstance(_, dict).
            ch = info_resp.get("channel") or {}
            if ch.get("is_im") or ch.get("is_mpim"):
                return None  # DMs / MPDMs are out of scope for v1
            meta = ChannelMeta(
                name=ch.get("name") or "",
                topic=((ch.get("topic") or {}).get("value") or ""),
                purpose=((ch.get("purpose") or {}).get("value") or ""),
            )

            bot_user_id = await self._resolve_bot_user_id(bot_token)
            raw: list[RawMessage] = []
            cursor: str = ""
            deadline = time.monotonic() + limits.fetch_time_budget_s
            for _page in range(max(1, limits.max_pages)):
                if time.monotonic() >= deadline:
                    break
                kwargs: dict = {"channel": channel_id, "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                try:
                    hist = await client.conversations_history(**kwargs)
                except Exception as exc:
                    retry_after = _retry_after_seconds(exc)
                    if raw and retry_after is not None and time.monotonic() + retry_after >= deadline:
                        break
                    raise
                msgs = hist.get("messages") or []
                for m in filter_messages(msgs, bot_user_id=bot_user_id):
                    try:
                        ts = float(m.get("ts") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    author = await self._resolve_user_name(bot_token, m.get("user") or "")
                    files = tuple(
                        (f.get("id") or "", f.get("name") or "")
                        for f in (m.get("files") or [])
                        if f.get("id")
                    )
                    raw.append(RawMessage(
                        ts=ts,
                        author=author,
                        text=(m.get("text") or "").strip(),
                        files=files,
                    ))
                cursor = (hist.get("response_metadata") or {}).get("next_cursor") or ""
                if not hist.get("has_more") or not cursor:
                    break
            return meta, raw
        except Exception:
            logger.warning(
                "SlackPlatform.fetch_channel_context failed for %s", channel_id,
                exc_info=True,
            )
            return None

    async def _resolve_user_name(self, bot_token: str, user_id: str) -> str:
        """Return a display name for *user_id*, cached per (token, user_id)."""
        cache_key = (bot_token, user_id)
        if cache_key in self._user_name_cache:
            return self._user_name_cache[cache_key]

        if not bot_token:
            return user_id

        try:
            client = self._get_client(bot_token)
            info = await client.users_info(user=user_id)
            # AsyncSlackResponse is dict-like but not a dict; .get() works directly.
            user_obj = info.get("user") or {}
            profile = user_obj.get("profile", {})
            name: str = (
                profile.get("display_name")
                or profile.get("real_name")
                or user_obj.get("name", user_id)
            ) or user_id
            self._user_name_cache[cache_key] = name
            return name
        except Exception:
            return user_id


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _uploaded_file_id(resp: Any) -> str | None:
    """Extract the uploaded file id from a files_upload_v2 response.

    ``resp`` is an ``AsyncSlackResponse`` — dict-like but not a dict subclass —
    so access fields with ``.get`` / indexing directly, never via isinstance.
    Handles both the ``{"files": [{"id": …}]}`` and ``{"file": {"id": …}}``
    response shapes.
    """
    files = resp.get("files")
    if files:
        fid = files[0].get("id")
        if fid:
            return str(fid)
    single = resp.get("file")
    if single:
        fid = single.get("id")
        if fid:
            return str(fid)
    return None


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

def _register() -> None:
    """Register the singleton SlackPlatform in the module-level registry.

    Called once at import time.  Guarded against double-registration so that
    test suites that reimport the module (e.g. via importlib.reload) do not
    raise a ValueError from the registry.
    """
    from surogates.channels.registry import registry

    if registry.get("slack") is None:
        registry.register(SlackPlatform())


_register()
