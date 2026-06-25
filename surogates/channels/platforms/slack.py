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
import time
from typing import Any

try:
    from slack_sdk.web.async_client import AsyncWebClient
except ImportError:
    AsyncWebClient = Any  # type: ignore[misc,assignment]

from surogates.channels.base import SendResult
from surogates.channels.inbound import InboundMessage
from surogates.channels.registry import ChannelDescriptor, VerificationResult

__all__ = [
    "SlackPlatform",
    "identifier_of",
    "verify",
    "parse",
]

logger = logging.getLogger(__name__)

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
    # ------------------------------------------------------------------
    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        logger.debug("verify: body is not valid JSON")
        return False

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
    # ------------------------------------------------------------------
    bot_id = event.get("bot_id")
    subtype = event.get("subtype", "")

    if bot_id or subtype == "bot_message":
        return None

    # ------------------------------------------------------------------
    # Gate: edits and deletions.
    # ------------------------------------------------------------------
    if subtype in ("message_changed", "message_deleted"):
        return None

    # ------------------------------------------------------------------
    # Extract message fields (mirrors SlackAdapter._handle_slack_message).
    # ------------------------------------------------------------------
    user_id: str = event.get("user", "")
    if not user_id:
        return None

    text: str = event.get("text", "").strip()
    channel_id: str = event.get("channel", "")
    channel_type: str = event.get("channel_type", "")
    ts: str = event.get("ts", "")
    event_thread_ts: str | None = event.get("thread_ts")

    # DM detection: channel_type "im" or "mpim".
    is_dm = channel_type in ("im", "mpim")

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

    for file_info in event.get("files", []):
        url = file_info.get("url_private_download") or file_info.get("url_private", "")
        if not url:
            continue
        media_urls.append(url)
        file_names.append(file_info.get("name", "file"))

    if file_names:
        names_str = ", ".join(file_names)
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
        self, body: Any, *, creds: dict | None = None
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
        """
        bot_token: str = (creds or {}).get("bot_token") or ""
        bot_user_id = await self._resolve_bot_user_id(bot_token)
        return parse(body, bot_user_id=bot_user_id)

    async def _resolve_bot_user_id(self, bot_token: str) -> str:
        """Return the bot's Slack user id, resolved once per token via auth.test."""
        if not bot_token:
            return ""
        if bot_token in self._bot_user_id_cache:
            return self._bot_user_id_cache[bot_token]
        try:
            client = AsyncWebClient(token=bot_token)
            result = await client.auth_test()
            user_id: str = result.get("user_id") or ""
            self._bot_user_id_cache[bot_token] = user_id
            return user_id
        except Exception:
            logger.debug("SlackPlatform: auth.test failed for token — skipping bot_user_id")
            return ""

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        """Post an outbox item to Slack via ``chat.postMessage``.

        Parameters
        ----------
        item:
            Outbox item with ``destination`` (``channel_id``, optional
            ``thread_ts``) and ``payload`` (``content``).
        creds:
            Credential dict with ``bot_token``.
        """
        bot_token: str = creds.get("bot_token") or ""
        client = AsyncWebClient(token=bot_token)

        channel_id: str = item.destination.get("channel_id", "")
        text: str = item.payload.get("content", "")
        thread_ts: str | None = item.destination.get("thread_ts")

        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "text": text,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        try:
            result = await client.chat_postMessage(**kwargs)
            sent_ts: str | None = result.get("ts") if isinstance(result, dict) else None
            return SendResult(success=True, message_id=sent_ts)
        except Exception as exc:
            logger.error("[SlackPlatform] chat_postMessage failed: %s", exc)
            return SendResult(success=False, error=str(exc))

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

    async def _resolve_user_name(self, bot_token: str, user_id: str) -> str:
        """Return a display name for *user_id*, cached per (token, user_id)."""
        cache_key = (bot_token, user_id)
        if cache_key in self._user_name_cache:
            return self._user_name_cache[cache_key]

        if not bot_token:
            return user_id

        try:
            client = AsyncWebClient(token=bot_token)
            info = await client.users_info(user=user_id)
            user_obj = info.get("user", {}) if isinstance(info, dict) else {}
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
