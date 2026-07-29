"""WhatsApp Business Cloud API webhook channel platform strategy.

Exposes three stateless module-level functions used by the dispatcher and
the registered :class:`WhatsAppPlatform` object that implements
:class:`~surogates.channels.registry.ChannelPlatform`.

Module-level functions
----------------------
identifier_of(request, body) -> str
    Reads the tenant's ``phone_number_id`` from the URL path parameter.
    The path is authoritative: the dispatcher resolves the tenant and its
    credentials before the body is parsed, and the body's copy of the id is
    only cross-checked afterwards in :func:`parse`.

verify(request, raw_body, *, creds) -> bool | VerificationResult
    Branches on the HTTP method.  ``GET`` is Meta's callback-URL handshake
    (``hub.mode``/``hub.verify_token``/``hub.challenge``) and returns a
    :class:`VerificationResult` echoing the challenge as plain text.
    ``POST`` validates the ``X-Hub-Signature-256`` HMAC-SHA256 over the raw
    body.

parse(body, *, creds, identifier) -> InboundMessage | None
    Converts a WhatsApp Business Account webhook envelope into an
    :class:`~surogates.channels.inbound.InboundMessage`.  Returns ``None``
    for everything that is not a user message — delivery statuses,
    reactions, system events — per the protocol's loop-safety contract.

WhatsApp Cloud API is DM-only for our purposes: every conversation is 1:1,
so ``thread_key`` is always ``None`` and ``visibility`` is ``"dm"``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import httpx

from surogates.channels.base import SendResult
from surogates.channels.inbound import InboundFileRef, InboundMessage
from surogates.channels.platforms.whatsapp_api import (
    DEFAULT_API_VERSION,
    download_media,
    ext_for_mime,
    media_kind_for_mime,
    send_message,
    upload_media,
)
from surogates.channels.platforms.whatsapp_format import render_whatsapp
from surogates.channels.registry import ChannelDescriptor, VerificationResult
from surogates.channels.text_split import split_text

__all__ = [
    "WhatsAppPlatform",
    "identifier_of",
    "verify",
    "parse",
]

logger = logging.getLogger(__name__)

#: Meta's documented maximum webhook body size.  Checked before any crypto.
_MAX_BODY_BYTES = 3 * 1024 * 1024

#: WhatsApp's ``text.body`` cap.  Unlike Telegram (which feeds ``split_text``
#: 3500 to leave headroom for HTML-render inflation), WhatsApp markup does
#: not inflate and the transcoder runs *before* splitting, so the full cap
#: is correct here.
_MAX_MESSAGE_CHARS = 4096

#: Inbound message types that carry a user message.  Everything else —
#: reaction, system, unsupported, order, location, contacts — returns None
#: rather than starting an agent turn with an empty prompt.
_MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")
_MESSAGE_TYPES = ("text",) + _MEDIA_TYPES


# ---------------------------------------------------------------------------
# identifier_of
# ---------------------------------------------------------------------------


def identifier_of(request: Any, body: Any) -> str:
    """Return the tenant's ``phone_number_id`` from the URL path parameter.

    Parameters
    ----------
    request:
        Starlette-like request exposing ``path_params["phone_number_id"]``.
    body:
        Parsed request body — intentionally ignored.  The dispatcher calls
        this before the body is read.
    """
    return request.path_params["phone_number_id"]


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _verify_handshake(request: Any, *, creds: dict) -> VerificationResult:
    """Handle Meta's GET callback-URL verification handshake.

    Every rejection renders 401: the dispatcher discards
    ``VerificationResult.status_code`` when ``accepted`` is False.  The
    distinguishing signal is therefore the log level, not the response —
    an unconfigured token logs at ERROR because it is a misconfiguration,
    a mismatch at WARNING because it may be an attack.
    """
    expected = (creds or {}).get("verify_token") or ""
    if not expected:
        logger.error(
            "[whatsapp] verify_token is not configured for this tenant — "
            "refusing the handshake",
        )
        return VerificationResult(accepted=False)

    query = request.query_params
    mode = query.get("hub.mode", "")
    token = query.get("hub.verify_token", "")
    challenge = query.get("hub.challenge", "")

    if mode != "subscribe":
        logger.info("[whatsapp] handshake rejected: hub.mode=%r", mode)
        return VerificationResult(accepted=False)

    # Compare bytes: compare_digest on str raises TypeError on non-ASCII,
    # and the token is attacker-controlled.
    if not hmac.compare_digest(
        token.encode("utf-8", "surrogatepass"), expected.encode("utf-8"),
    ):
        logger.warning("[whatsapp] handshake rejected: verify_token mismatch")
        return VerificationResult(accepted=False)

    if not challenge:
        logger.info("[whatsapp] handshake rejected: missing hub.challenge")
        return VerificationResult(accepted=False)

    return VerificationResult(accepted=True, response_body=challenge, status_code=200)


def _verify_signature(app_secret: str, raw_body: bytes, header: str) -> bool:
    """Constant-time X-Hub-Signature-256 check over the raw body bytes."""
    if not app_secret or not header:
        return False
    if not header.startswith("sha256="):
        return False
    expected_hex = header[len("sha256="):].strip()
    if not expected_hex:
        return False
    computed = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed.lower(), expected_hex.lower())


def verify(
    request: Any,
    raw_body: bytes,
    *,
    creds: dict,
) -> bool | VerificationResult:
    """Validate a WhatsApp webhook request.

    Parameters
    ----------
    request:
        Starlette-like request exposing ``method``, ``headers`` and
        ``query_params``.
    raw_body:
        Raw request body bytes.  The HMAC must be computed over these, never
        over a re-serialisation.
    creds:
        Credential dict; ``app_secret`` for POST, ``verify_token`` for GET.
    """
    if getattr(request, "method", "POST").upper() == "GET":
        return _verify_handshake(request, creds=creds)

    if len(raw_body) > _MAX_BODY_BYTES:
        logger.warning(
            "[whatsapp] rejecting %d-byte body (cap %d)",
            len(raw_body), _MAX_BODY_BYTES,
        )
        return False

    app_secret = (creds or {}).get("app_secret") or ""
    if not app_secret:
        logger.error(
            "[whatsapp] app_secret is not configured for this tenant — "
            "refusing the webhook",
        )
        return False

    header = request.headers.get("X-Hub-Signature-256", "")
    return _verify_signature(app_secret, raw_body, header)


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def _log_statuses(value: dict, *, identifier: str) -> None:
    """Log delivery statuses and inbound errors.

    These carry asynchronous send failures the ``POST /messages`` 200 did
    not report, so they must be visible.  They are *not* emitted as channel
    observations: that queue is the follow-mode memory firehose and needs a
    redis handle and an ``agent_id`` that ``parse`` does not receive.

    Wrapped by the caller in try/except — a logging failure must never turn
    a status webhook into a 400 and a Meta retry loop.
    """
    for status in value.get("statuses") or []:
        if not isinstance(status, dict):
            continue
        state = status.get("status")
        if state == "failed":
            logger.warning(
                "[whatsapp] delivery failed pnid=%s wamid=%s errors=%s",
                identifier, status.get("id"), status.get("errors"),
            )
        else:
            logger.debug(
                "[whatsapp] delivery %s pnid=%s wamid=%s",
                state, identifier, status.get("id"),
            )
    for error in value.get("errors") or []:
        logger.warning("[whatsapp] inbound error pnid=%s error=%s", identifier, error)


def _file_ref(raw_message: dict, msg_type: str) -> InboundFileRef | None:
    """Build an :class:`InboundFileRef` from a media message.

    ``url`` carries the Cloud API media id, not an HTTP URL: the framework
    passes it straight back to ``download_file``, which does the two Graph
    hops.  This mirrors how Telegram carries its ``file_id``.
    """
    block = raw_message.get(msg_type)
    if not isinstance(block, dict):
        return None
    media_id = block.get("id")
    if not media_id:
        return None
    mime_type = block.get("mime_type") or "application/octet-stream"
    filename = block.get("filename") or f"{media_id}{ext_for_mime(mime_type)}"
    return InboundFileRef(
        url=media_id,
        filename=filename,
        mime_type=mime_type,
        size=None,
        file_id=media_id,
    )


def parse(
    body: Any,
    *,
    creds: dict | None = None,
    identifier: str | None = None,
) -> InboundMessage | None:
    """Map a verified WhatsApp webhook envelope to an :class:`InboundMessage`.

    Returns ``None`` for every payload that is not a user message.  A
    ``phone_number_id`` in the body that does not match the path
    ``identifier`` is dropped: one Meta App can subscribe several numbers,
    and routing another number's message to this tenant would be a
    tenant-isolation failure.
    """
    if not isinstance(body, dict):
        return None
    if body.get("object") != "whatsapp_business_account":
        return None

    for entry in body.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue

            metadata = value.get("metadata") or {}
            body_pnid = metadata.get("phone_number_id")
            if identifier is not None and body_pnid != identifier:
                logger.warning(
                    "[whatsapp] dropping payload: body phone_number_id=%s "
                    "does not match route identifier=%s",
                    body_pnid, identifier,
                )
                return None

            try:
                _log_statuses(value, identifier=identifier or "")
            except Exception:  # noqa: BLE001
                pass

            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name", "")
                for c in value.get("contacts") or []
                if isinstance(c, dict)
            }

            raw_messages = [
                m for m in value.get("messages") or [] if isinstance(m, dict)
            ]
            for index, raw_message in enumerate(raw_messages):
                message = _build_message(raw_message, names=names, identifier=body_pnid)
                if message is not None:
                    dropped = len(raw_messages) - index - 1
                    if dropped:
                        # The dispatcher runs one pipeline pass per webhook.
                        # Meta rarely batches user messages (bursts, retry
                        # backlogs) — but the loss must be visible when it
                        # does.  Fan-out is a recorded v2 item.
                        logger.warning(
                            "[whatsapp] dropping %d additional batched "
                            "message(s) for pnid=%s (one message per webhook)",
                            dropped, body_pnid,
                        )
                    return message
    return None


def _build_message(
    raw_message: dict, *, names: dict, identifier: str | None,
) -> InboundMessage | None:
    """Build one :class:`InboundMessage`, or ``None`` if it is not a message."""
    # No default: an absent ``type`` is not a text message.  Defaulting would
    # re-introduce the reference bug where an unknown event starts an agent
    # turn with an empty prompt.  Meta always sends ``type``.
    msg_type = str(raw_message.get("type") or "").lower()
    if msg_type not in _MESSAGE_TYPES:
        logger.debug("[whatsapp] ignoring message type %r", msg_type)
        return None

    # E.164 digits, no leading "+".  identity.py builds the shadow-user email
    # with ``platform_user_id.lstrip("@")``, which does NOT strip a "+", so a
    # "+" here would end up inside the email local part.  Meta sends bare
    # digits today; normalise anyway so that stays true.
    wa_id = str(raw_message.get("from") or "").lstrip("+").strip()
    if not wa_id:
        # DM-only: without a resolvable 1:1 sender we cannot route a reply.
        # Log the raw type so we capture the real shape if a group payload
        # ever arrives.
        logger.warning(
            "[whatsapp] refusing message with no resolvable sender (type=%s, keys=%s)",
            msg_type, sorted(raw_message.keys()),
        )
        return None

    wamid = raw_message.get("id") or ""

    if msg_type == "text":
        text = ((raw_message.get("text") or {}).get("body")) or ""
        files: list[InboundFileRef] = []
    else:
        block = raw_message.get(msg_type) or {}
        text = block.get("caption") or ""
        ref = _file_ref(raw_message, msg_type)
        files = [ref] if ref is not None else []

    source: dict[str, Any] = {
        "phone_number_id": identifier,
        "wamid": wamid,
    }
    if msg_type == "audio":
        source["voice"] = bool((raw_message.get("audio") or {}).get("voice"))

    return InboundMessage(
        kind=msg_type,
        identifier=str(wa_id),
        thread_key=None,
        platform_user_id=str(wa_id),
        user_name=names.get(wa_id, "") or "",
        text=text,
        media_urls=[],
        media_types=[],
        is_dm=True,
        is_mention=False,
        ts=wamid,
        source=source,
        is_bot=False,
        visibility="dm",
        files=files,
    )


def _render_input_prompt(payload: dict) -> str:
    """Render an ``ask_user_question`` prompt as plain text.

    WhatsApp has no native buttons in this integration, so the choices are
    listed in the message body and a plain typed reply resolves the pending
    record through the shared ``resolve_text_answer`` path.

    The question shape is the one normalised by
    ``tools.builtin.ask_user_question._validate_questions`` and emitted
    verbatim into ``INBOX_INPUT_REQUIRED`` — ``prompt`` and ``choices``
    (each with a ``label``), NOT ``question``/``options``.

    Choices are bulleted rather than numbered, matching Telegram's text
    fallback: ``resolve_text_answer`` matches choice *labels*, so numbering
    would invite a "1" that the resolver records as a free-form answer.
    """
    lines: list[str] = ["❓ I need your input"]
    context = (payload.get("context") or "").strip()
    if context:
        lines.append(context)

    questions = [q for q in payload.get("questions") or [] if isinstance(q, dict)]
    for index, question in enumerate(questions):
        prompt = (question.get("prompt") or f"Question {index + 1}").strip()
        prefix = f"{index + 1}. " if len(questions) > 1 else ""
        lines.append(f"{prefix}{prompt}")
        for choice in question.get("choices") or []:
            label = choice.get("label") if isinstance(choice, dict) else str(choice)
            if label:
                lines.append(f"  • {label}")

    lines.append("Reply with your answer.")
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Platform object
# ---------------------------------------------------------------------------


class WhatsAppPlatform:
    """Webhook-based WhatsApp Cloud API channel platform strategy.

    Implements :class:`~surogates.channels.registry.ChannelPlatform`.

    Each tenant brings their own Meta App and WhatsApp Business Account.
    The tenant is identified by its ``phone_number_id``, which is the
    ``{phone_number_id}`` path parameter in the webhook URL.  Credentials
    (``access_token``, ``app_secret``, ``verify_token``) are resolved by the
    dispatcher and passed to every method that needs them.
    """

    kind = "whatsapp"
    topology = "webhook"

    #: Meta verifies the callback URL with an unsigned GET on the same path.
    #: The dispatcher mounts a GET route only for platforms declaring this.
    handshake_get = True

    descriptor = ChannelDescriptor(
        vault_refs=lambda identifier: {
            "access_token": "access_token",
            "app_secret": "app_secret",
            "verify_token": "verify_token",
            # Non-secrets, resolved through the same channel because creds
            # are the only payload every outbound surface receives: send,
            # send_files, download_file, send_private and post_input_nudge
            # get no routing object and session config never carries
            # routing config.
            "phone_number_id": "phone_number_id",
            "api_version": "api_version",
        },
        config_keys=(
            "require_mention",
            "allow_bots",
            "identity_policy",
            "waba_id",
            "api_version",
        ),
        # Meta has no setWebhook equivalent: the callback URL is configured
        # by hand in the App Dashboard.
        webhook_registration="manual",
    )

    def __init__(self) -> None:
        # Shared HTTP client reused for every Graph call.  The access token
        # is a per-tenant credential passed per request, not held on the
        # client, so one client per platform instance suffices.
        # WhatsAppPlatform instances are process-lifetime singletons; no
        # explicit close is needed.
        self._http = httpx.AsyncClient(timeout=30.0)

    # ------------------------------------------------------------------
    # Route path
    # ------------------------------------------------------------------

    def route_path(self, identifier: str | None = None) -> str:
        """Return the FastAPI path for this platform.

        Parameters
        ----------
        identifier:
            The tenant's ``phone_number_id``.  When ``None`` (template form
            used by ``build_app``), returns the parametrised path template.
        """
        if identifier is None:
            return "/whatsapp/{phone_number_id}"
        return f"/whatsapp/{identifier}"

    # ------------------------------------------------------------------
    # identifier_of / verify / parse — delegates to module functions
    # ------------------------------------------------------------------

    def identifier_of(self, request: Any, body: Any) -> str:
        return identifier_of(request, body)

    def verify(
        self, request: Any, raw_body: bytes, *, creds: dict,
    ) -> bool | VerificationResult:
        return verify(request, raw_body, creds=creds)

    def parse(
        self,
        body: Any,
        *,
        creds: dict | None = None,
        identifier: str | None = None,
    ) -> InboundMessage | None:
        return parse(body, creds=creds, identifier=identifier)

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        """Deliver one outbox row to WhatsApp.

        Long bodies are transcoded, split at 4096 characters, and posted as
        separate messages.  A mid-sequence failure reports ``success=True``
        with the last delivered id so a retry does not duplicate the chunks
        that already landed.
        """
        token: str = (creds or {}).get("access_token") or ""
        destination = item.destination or {}
        wa_id: str = destination.get("wa_id") or ""
        phone_number_id: str = destination.get("phone_number_id") or ""
        api_version: str = (creds or {}).get("api_version") or DEFAULT_API_VERSION

        if not token or not wa_id or not phone_number_id:
            return SendResult(
                success=False, error="missing whatsapp credentials or destination",
            )

        if item.payload.get("input_prompt"):
            text = _render_input_prompt(item.payload)
        else:
            text = render_whatsapp(item.payload.get("content", "") or "")

        if not text.strip():
            # Nothing to send.  ``success=True`` with no id is the correct
            # terminal state: ``_deliver_item`` has exactly two branches and
            # never reads ``SendResult.retryable``, so ``success=False``
            # would requeue an unsendable item every 30 s for 30 minutes.
            return SendResult(success=True, message_id=None)

        last_id: str | None = None
        for chunk in split_text(text, _MAX_MESSAGE_CHARS) or [text]:
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_id,
                "type": "text",
                "text": {"body": chunk, "preview_url": True},
            }
            wamid, error = await send_message(
                self._http,
                token=token,
                phone_number_id=phone_number_id,
                payload=payload,
                api_version=api_version,
            )
            if wamid is None:
                if last_id is not None:
                    # Part of the reply landed; report the delivered prefix
                    # instead of triggering a duplicate redelivery.
                    return SendResult(success=True, message_id=last_id)
                return SendResult(success=False, error=error or "send failed")
            last_id = wamid

        if last_id is None:
            return SendResult(success=False, error="send failed")
        return SendResult(success=True, message_id=last_id)

    # ------------------------------------------------------------------
    # send_files — upload workspace files referenced by MEDIA: markers
    # ------------------------------------------------------------------

    async def send_files(
        self, item: Any, *, creds: dict, files: list,
    ) -> list[str]:
        """Upload *files* and post each as a native WhatsApp attachment.

        Two Graph hops per file: ``POST /media`` yields a media id, then
        ``POST /messages`` sends it.  Returns the uploaded media ids (empty
        when nothing uploaded).  Best-effort per file: any error is logged
        and skipped, never raised — the same contract as ``download_file``.
        """
        token: str = (creds or {}).get("access_token") or ""
        destination = item.destination or {}
        wa_id: str = destination.get("wa_id") or ""
        phone_number_id: str = destination.get("phone_number_id") or ""
        api_version: str = (creds or {}).get("api_version") or DEFAULT_API_VERSION
        if not token or not wa_id or not phone_number_id or not files:
            return []

        # No caption.  ``_deliver_item`` posts any surrounding text as its own
        # message before calling us, so captioning the first attachment with
        # ``payload["content"]`` would repeat that sentence; on the
        # marker-only path the content is empty anyway.  Slack's send_files
        # omits its comment for the same reason.
        uploaded: list[str] = []
        for f in files:
            media_id, error = await upload_media(
                self._http,
                token=token,
                phone_number_id=phone_number_id,
                filename=f.filename,
                data=f.data,
                mime_type=f.mime_type,
                api_version=api_version,
            )
            if media_id is None:
                logger.warning(
                    "[whatsapp] media upload failed for %s (%s)", f.filename, error,
                )
                continue

            kind = media_kind_for_mime(f.mime_type)
            block: dict[str, Any] = {"id": media_id}
            if kind == "document":
                block["filename"] = f.filename

            _, send_error = await send_message(
                self._http,
                token=token,
                phone_number_id=phone_number_id,
                payload={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": wa_id,
                    "type": kind,
                    kind: block,
                },
                api_version=api_version,
            )
            if send_error:
                logger.warning(
                    "[whatsapp] sending media %s failed (%s)", f.filename, send_error,
                )
                continue
            uploaded.append(media_id)
        return uploaded

    # ------------------------------------------------------------------
    # ack_received — read receipt + typing indicator in one call
    # ------------------------------------------------------------------

    async def ack_received(self, msg: Any, *, creds: dict, config: dict) -> None:
        """Mark the inbound message read and show the typing pip.

        One request sets blue double-checkmarks *and* the typing indicator.
        This is the only progress signal WhatsApp offers: it cannot edit a
        sent message, so the dispatcher swallows every intermediate
        narration event and the agent would otherwise be silent for the
        whole tool-calling phase.

        Called only when the pipeline accepted the message, so filtered
        senders never receive a receipt.  Best-effort; never raises.
        """
        token: str = (creds or {}).get("access_token") or ""
        source = getattr(msg, "source", None) or {}
        wamid = source.get("wamid")
        phone_number_id = source.get("phone_number_id") or (creds or {}).get(
            "phone_number_id"
        )
        if not token or not wamid or not phone_number_id:
            return

        _, error = await send_message(
            self._http,
            token=token,
            phone_number_id=phone_number_id,
            payload={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": wamid,
                "typing_indicator": {"type": "text"},
            },
            api_version=(creds or {}).get("api_version") or DEFAULT_API_VERSION,
        )
        if error and "131009" in error:
            # wamid older than 30 days — common after a long-quiet
            # conversation, not worth a warning.
            logger.info("[whatsapp] read receipt skipped: %s", error)
        elif error:
            logger.warning("[whatsapp] read receipt failed (%s)", error)

    # ------------------------------------------------------------------
    # download_file — two-hop inbound media fetch
    # ------------------------------------------------------------------

    async def download_file(
        self, *, creds: dict, url: str, max_bytes: int,
    ) -> bytes | None:
        """Fetch inbound media.  ``url`` carries the Cloud API media id.

        Mirrors Telegram, which carries its ``file_id`` in the same slot.
        ``max_bytes`` is a hard cap, not a streaming contract — the caller
        buffers the whole body.  Best-effort; never raises.
        """
        token: str = (creds or {}).get("access_token") or ""
        if not token or not url:
            return None
        data, _mime = await download_media(
            self._http,
            token=token,
            media_id=url,
            max_bytes=max_bytes,
            api_version=(creds or {}).get("api_version") or DEFAULT_API_VERSION,
        )
        return data

    # ------------------------------------------------------------------
    # send_private / post_input_nudge — plain sends
    # ------------------------------------------------------------------

    async def _send_plain(
        self, *, creds: dict, wa_id: str, text: str,
    ) -> str | None:
        """Post one plain text message.  Returns the wamid, or ``None``."""
        token: str = (creds or {}).get("access_token") or ""
        phone_number_id: str = (creds or {}).get("phone_number_id") or ""
        if not token or not phone_number_id or not wa_id or not text:
            return None
        wamid, error = await send_message(
            self._http,
            token=token,
            phone_number_id=phone_number_id,
            payload={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": wa_id,
                "type": "text",
                "text": {"body": text, "preview_url": True},
            },
            api_version=(creds or {}).get("api_version") or DEFAULT_API_VERSION,
        )
        if error:
            logger.warning("[whatsapp] plain send failed (%s)", error)
        return wamid

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

        Every WhatsApp conversation is already a DM, so this is a plain
        send.  Used by the ``linked`` identity policy to deliver a pairing
        code without leaking it into a shared surface.
        """
        wamid = await self._send_plain(creds=creds, wa_id=sender_id, text=text)
        return wamid is not None

    async def post_input_nudge(
        self, *, creds: dict, channel: str, thread_ts: Any, text: str,
    ) -> str | None:
        """Deliver a status line to the conversation.

        Not interactive-only: this carries the ``/stop`` acknowledgement and
        the allowance/subscription block notice with its buy link.  WhatsApp
        has no threads, so ``thread_ts`` is ignored.  Best-effort; returns
        the message id or ``None`` — the same contract as Slack/Telegram.
        """
        return await self._send_plain(creds=creds, wa_id=channel, text=text)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def _register() -> None:
    """Register the singleton WhatsAppPlatform in the module-level registry.

    Called once at import time.  Guarded against double-registration so that
    test suites that reimport the module (e.g. via importlib.reload) do not
    raise a ValueError from the registry.
    """
    from surogates.channels.registry import registry

    if registry.get("whatsapp") is None:
        registry.register(WhatsAppPlatform())


_register()
