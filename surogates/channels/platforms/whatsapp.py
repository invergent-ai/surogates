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

import asyncio
import dataclasses
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import PlainTextResponse, Response

from surogates.channels.base import SendResult
from surogates.channels.capabilities import ChannelCapabilities
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
from surogates.channels.whatsapp_flows import (
    BODY_CHARS,
    BUTTON_CHARS,
    MAX_LIST_ROWS,
    MAX_REPLY_BUTTONS,
    ROW_DESCRIPTION_CHARS,
    ROW_TITLE_CHARS,
    form_reply_text,
)
from surogates.programs.delivery import schedule_status_apply
from surogates.session.interactive_input import fold_label

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

#: Meta drops the typing indicator after 25 s, or as soon as any message is
#: sent, so a longer turn needs it re-sent.
_TYPING_EVERY_S = 20.0
#: A turn whose end we never observe (lost outbox row, crashed worker whose
#: lease has not expired yet) must not type forever.
_TYPING_CAP_S = 300.0


@dataclass
class _Typing:
    token: str
    phone_number_id: str
    wamid: str
    api_version: str
    turn_active: Callable[[], Awaitable[bool]]
    deadline: float
    task: asyncio.Task | None = None


#: Taps: a reply button or list row from an interactive message, a WhatsApp
#: Flow completion, or a quick-reply button on an approved template.
_TAP_TYPES = ("interactive", "button")
_MESSAGE_TYPES = ("text",) + _MEDIA_TYPES + _TAP_TYPES


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


#: The app-level callback. Meta refuses the per-number
#: ``override_callback_uri`` that ops registers until the Meta app has a
#: webhook of its own with the ``messages`` field subscribed, and the
#: per-number path cannot pass that first verification: it answers only for a
#: number that is already connected. This path belongs to no tenant.
APP_WEBHOOK_PATH = "/whatsapp"


def app_verify_token(encryption_key: str) -> str:
    """The verify token for :data:`APP_WEBHOOK_PATH`, derived from the vault key.

    surogate-ops derives the same value to show the operator, so the two
    sides share no extra setting. Both repositories pin the same test vector;
    change the label or the length on one side and verification breaks.

    The token is the same for every tenant of a deployment. That is harmless:
    it only lets a Meta app point its app-level webhook here, and events that
    arrive here are dropped.
    """
    return hmac.new(
        encryption_key.strip().encode("utf-8"),
        b"whatsapp-app-webhook",
        hashlib.sha256,
    ).hexdigest()[:32]


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


def _status_error_text(status: dict) -> str | None:
    """Meta's own wording for a failed delivery, if it gave any."""
    for error in status.get("errors") or []:
        if isinstance(error, dict):
            text = error.get("title") or error.get("message")
            if text:
                return str(text)
    return None


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
        # A check-in opener's fate lives on its invitation, not only in this
        # log line: without recording it, a user we never reached would be
        # marked a non-responder when the deadline sweep runs.  Fire-and-forget
        # because parsing is synchronous and a raise here would answer Meta
        # non-200 and start a retry loop.
        schedule_status_apply(
            provider_message_id=str(status.get("id") or ""),
            status=str(state or ""),
            reason=_status_error_text(status),
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


def _flow_reply(reply: dict) -> tuple[str, dict]:
    """A Flow completion's text, and its submitted fields with the token the
    flow was sent with taken out of them.

    The text is the fields as ``label: value`` lines under their keys; the
    pipeline relabels them once it has checked the token against the
    conversation, then drops the token.
    """
    raw = reply.get("response_json")
    try:
        fields = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return str(raw or ""), {}
    if not isinstance(fields, dict):
        return str(fields or ""), {}
    token = fields.pop("flow_token", None)
    return form_reply_text({}, fields), {
        "flow_fields": fields, **({"flow_token": str(token)} if token else {}),
    }


def _tap_text(raw_message: dict, msg_type: str) -> str:
    """The text a tap stands for.

    A button or row title is the choice label verbatim, so the shared
    ``resolve_text_answer`` matches it to the pending question; a list row's
    description is left out for the same reason.
    """
    if msg_type == "button":
        return str((raw_message.get("button") or {}).get("text") or "")
    interactive = raw_message.get("interactive") or {}
    kind = interactive.get("type")
    reply = interactive.get(kind) if isinstance(kind, str) else None
    if not isinstance(reply, dict):
        return ""
    if kind in ("button_reply", "list_reply"):
        return str(reply.get("title") or "")
    return ""


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
            built = [
                m for m in (
                    _build_message(raw, names=names, identifier=body_pnid)
                    for raw in raw_messages
                ) if m is not None
            ]
            if built:
                return _merge_batch(built, pnid=body_pnid)
    return None


def _merge_batch(messages: list[InboundMessage], *, pnid: str | None) -> InboundMessage:
    """One message for a webhook that batched several from one person.

    The dispatcher runs one pipeline pass per webhook, and Meta batches a
    burst ("Bună ziua" / "am o comandă" / "nu a ajuns azi").  Keeping only
    the first dropped the complaint.  Same-sender messages are joined in
    order, with every attachment; ``ts`` stays the first wamid so a retried
    batch still deduplicates, while ``source.wamid`` is the last one, the
    message a read receipt and the typing indicator should point at.
    """
    first = messages[0]
    same = [m for m in messages if m.platform_user_id == first.platform_user_id]
    if len(same) < len(messages):
        logger.warning(
            "[whatsapp] dropping %d batched message(s) from other senders for pnid=%s",
            len(messages) - len(same), pnid,
        )
    if len(same) == 1:
        return first
    files = [f for m in same for f in (m.files or [])]
    kind = next((m.kind for m in same if m.files), first.kind)
    source = dict(first.source or {})
    source["wamid"] = (same[-1].source or {}).get("wamid") or source.get("wamid")
    for m in same[1:]:
        for flag in ("flow_reply", "voice"):
            if (m.source or {}).get(flag):
                source[flag] = True
    return dataclasses.replace(
        first,
        kind=kind,
        text="\n".join(m.text for m in same if m.text),
        files=files,
        source=source,
    )


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

    files: list[InboundFileRef] = []
    interactive = raw_message.get("interactive") or {}
    # A Flow's form fields are never a pending question's answer.
    flow_reply = msg_type == "interactive" and interactive.get("type") == "nfm_reply"
    flow_text, flow_source = _flow_reply(interactive.get("nfm_reply") or {}) if flow_reply else ("", {})
    if msg_type == "text":
        text = ((raw_message.get("text") or {}).get("body")) or ""
    elif msg_type in _TAP_TYPES:
        text = (flow_text if flow_reply else _tap_text(raw_message, msg_type)).strip()
        if not text:
            logger.debug("[whatsapp] ignoring a tap with no text: %s", raw_message.get(msg_type))
            return None
        msg_type = "text"
    else:
        block = raw_message.get(msg_type) or {}
        text = block.get("caption") or ""
        ref = _file_ref(raw_message, msg_type)
        files = [ref] if ref is not None else []

    source: dict[str, Any] = {
        "phone_number_id": identifier,
        "wamid": wamid,
    }
    if flow_reply:
        source["flow_reply"] = True
        source.update(flow_source)
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

    The fallback for prompts :func:`_interactive_prompt` cannot carry
    (several questions, open-ended ones, labels over Meta's limits): the
    choices are listed in the message body.  A typed reply naming a choice,
    or any reply to a single open question, answers it; anything else,
    including any reply to several questions, reaches the agent as the
    person's message and dismisses the question.

    The question shape is the one normalised by
    ``tools.builtin.ask_user_question._validate_questions`` and emitted
    verbatim into ``INBOX_INPUT_REQUIRED`` — ``prompt`` and ``choices``
    (each with a ``label``), NOT ``question``/``options``.

    Choices are bulleted rather than numbered, matching Telegram's text
    fallback: ``resolve_text_answer`` matches choice *labels*, so numbering
    would invite a "1" that the resolver records as a free-form answer.

    No header or footer of our own: the people on the other end are
    end users writing in their own language.
    """
    lines: list[str] = []
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

    return "\n".join(line for line in lines if line)


#: The list message's button text when the agent gives none.  A glyph, not a
#: word: people read the prompt in their own language and an English
#: "Choose" would be the only foreign text on the screen.
_DEFAULT_LIST_BUTTON = "☰"

#: Graph errors for an interactive message Meta will not accept as built
#: (131009: parameter value is not valid; 100: invalid parameter).
_INTERACTIVE_REFUSED = re.compile(r"graph error (131009|100) ")


def _template_object(template: dict) -> dict:
    """The Cloud API ``template`` object, with a FLOW button's token when one is set.

    Only the FLOW button takes a parameter: quick replies on the same
    template have fixed payloads and need no component.  ``index`` is the
    button's position among all the template's buttons.
    """
    out: dict[str, Any] = {"name": template["name"], "language": {"code": template["language"]}}
    button = template.get("flow_button")
    if isinstance(button, dict) and button.get("flow_token"):
        action: dict[str, Any] = {"flow_token": button["flow_token"]}
        if button.get("flow_action_data"):
            action["flow_action_data"] = button["flow_action_data"]
        out["components"] = [{
            "type": "button",
            "sub_type": "flow",
            "index": str(button.get("index") or "0"),
            "parameters": [{"type": "action", "action": action}],
        }]
    return out


def _interactive_prompt(payload: dict) -> dict | None:
    """The ``interactive`` object for a single choice question, or ``None``.

    Up to three short labels without descriptions become reply buttons;
    otherwise up to ten rows become a list.  Either way the tap comes back
    carrying the label as its title, which ``resolve_text_answer`` matches.
    A prompt over any Cloud API limit goes out as text instead: a truncated
    label would no longer match the choice it stands for.
    """
    questions = [q for q in payload.get("questions") or [] if isinstance(q, dict)]
    if len(questions) != 1:
        return None
    question = questions[0]
    choices = [c for c in question.get("choices") or [] if isinstance(c, dict) and c.get("label")]
    if not choices or len(choices) > MAX_LIST_ROWS:
        return None
    # Meta refuses repeated button or row titles, and a tap carries only the
    # title, so two equal labels could not tell their choices apart anyway.
    if len({fold_label(c["label"]) for c in choices}) != len(choices):
        return None

    body = "\n".join(
        part for part in (
            (payload.get("context") or "").strip(),
            (question.get("prompt") or "").strip(),
        ) if part
    )
    if not body or len(body) > BODY_CHARS:
        return None

    if (
        len(choices) <= MAX_REPLY_BUTTONS
        and not any(c.get("description") for c in choices)
        and all(len(c["label"]) <= BUTTON_CHARS for c in choices)
    ):
        return {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"choice:{i}", "title": c["label"]}}
                    for i, c in enumerate(choices)
                ],
            },
        }

    if any(
        len(c["label"]) > ROW_TITLE_CHARS
        or len(c.get("description") or "") > ROW_DESCRIPTION_CHARS
        for c in choices
    ):
        return None
    rows = []
    for i, c in enumerate(choices):
        row = {"id": f"choice:{i}", "title": c["label"]}
        if c.get("description"):
            row["description"] = c["description"]
        rows.append(row)
    return {
        "type": "list",
        "body": {"text": body},
        "action": {
            "button": question.get("button_label") or _DEFAULT_LIST_BUTTON,
            "sections": [{"rows": rows}],
        },
    }


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
    capabilities = ChannelCapabilities(
        can_initiate=True, opener="template", window_hours=24, buttons=True,
        lists=True, forms=True, identity="phone",
        receipts=frozenset({"delivered", "read"}),
    )

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
        # No ``require_mention`` or ``allow_bots``: both are unreachable on
        # this platform, so shipping them would be a switch that cannot fire.
        # ``parse`` always sets ``is_dm=True`` (Cloud API is 1:1), and
        # ``_evaluate_mention_gate`` returns True immediately for a DM; it
        # also always sets ``is_bot=False``, because Cloud API never marks a
        # sender as a bot, so the ``allow_bots`` gate never runs either.
        config_keys=(
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
        # (phone_number_id, wa_id) -> the typing keep-alive of the turn under way.
        self._typing: dict[tuple[str, str], _Typing] = {}

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

    def app_routes(self, settings: Any) -> list[tuple[str, list[str], Any]]:
        """Routes for the app-level callback, mounted by the dispatcher as-is.

        GET answers Meta's verification with :func:`app_verify_token`. POST
        acknowledges and drops: an event lands here only for an account that
        has no per-number override yet, and without a tenant there is no app
        secret to check its signature against.
        """
        key = (settings.encryption_key or "").strip()
        expected = app_verify_token(key) if key else ""

        async def handshake(request: Request) -> Response:
            result = _verify_handshake(request, creds={"verify_token": expected})
            if not result.accepted:
                return Response(status_code=401)
            return PlainTextResponse(result.response_body)

        async def event(request: Request) -> Response:
            logger.info(
                "[whatsapp] dropped an event sent to the app-level webhook — "
                "its business account has no per-number callback override",
            )
            return Response(status_code=200)

        return [
            (APP_WEBHOOK_PATH, ["GET"], handshake),
            (APP_WEBHOOK_PATH, ["POST"], event),
        ]

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

        template = item.payload.get("template")
        if template:
            # A scheduled opener is business-initiated: outside the 24-hour
            # service window only an approved template may open the
            # conversation.  Free-form text here comes back 131047, which is
            # permanent, so the opener would be dropped with nothing surfaced
            # to the operator.
            if not isinstance(template, dict) or not template.get("name") or not template.get("language"):
                # Nothing to retry: the payload can never become sendable.
                # The wording is in the permanent-error list so the dispatcher
                # drops it now instead of posting sixty times over half an
                # hour and then reporting only "'language'".
                return SendResult(
                    success=False,
                    error="template payload incomplete: name and language required",
                )
            wamid, error = await send_message(
                self._http,
                token=token,
                phone_number_id=phone_number_id,
                payload={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": wa_id,
                    "type": "template",
                    "template": _template_object(template),
                },
                api_version=api_version,
            )
            if wamid is None:
                return SendResult(success=False, error=error or "send failed")
            return SendResult(success=True, message_id=wamid)

        flow = item.payload.get("flow")
        if flow or item.payload.get("input_prompt"):
            interactive = flow or _interactive_prompt(item.payload)
            if interactive is not None:
                wamid, error = await send_message(
                    self._http,
                    token=token,
                    phone_number_id=phone_number_id,
                    payload={
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": wa_id,
                        "type": "interactive",
                        "interactive": interactive,
                    },
                    api_version=api_version,
                )
                if wamid is not None:
                    self._stop_typing(phone_number_id, wa_id)
                    return SendResult(success=True, message_id=wamid)
                # A refused interactive question would dead-letter while
                # ask_user_question waits on an answer the person never saw.
                if flow or not _INTERACTIVE_REFUSED.match(error or ""):
                    return SendResult(success=False, error=error or "send failed")
                logger.warning("[whatsapp] interactive prompt refused, sending it as text: %s", error)
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
        # Text beside a tool call or a coding run's heartbeat is the turn still
        # going; an answer or a question ends what the person waits for.
        if item.payload.get("intermediate") or item.payload.get("code_run"):
            self._rearm_typing(phone_number_id, wa_id)
        else:
            self._stop_typing(phone_number_id, wa_id)
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
    # keep_typing — read receipt + typing indicator, held for the turn
    # ------------------------------------------------------------------

    def keep_typing(
        self,
        msg: Any,
        *,
        creds: dict,
        turn_active: Callable[[], Awaitable[bool]],
    ) -> None:
        """Mark *msg* read and keep the typing pip up while its turn runs.

        This is the only progress signal WhatsApp offers.  Meta shows the pip for at most 25 s and drops it
        on any outbound message, so it is re-sent every 20 s, and right
        after a message that does not end the turn, until a question or an
        answer goes out, *turn_active* says the turn is over, or the cap.

        Called only for messages that reach the agent, so filtered senders
        never receive a receipt.  Returns at once; never raises.
        """
        token: str = (creds or {}).get("access_token") or ""
        source = getattr(msg, "source", None) or {}
        wamid = source.get("wamid")
        phone_number_id = source.get("phone_number_id") or (creds or {}).get(
            "phone_number_id"
        )
        wa_id = getattr(msg, "identifier", "") or ""
        if not token or not wamid or not phone_number_id or not wa_id:
            return
        key = (phone_number_id, wa_id)
        self._stop_typing(*key)
        state = _Typing(
            token=token,
            phone_number_id=phone_number_id,
            wamid=wamid,
            api_version=(creds or {}).get("api_version") or DEFAULT_API_VERSION,
            turn_active=turn_active,
            deadline=asyncio.get_running_loop().time() + _TYPING_CAP_S,
        )
        self._typing[key] = state
        self._start_typing(key, state)

    def _start_typing(self, key: tuple[str, str], state: _Typing) -> None:
        state.task = asyncio.get_running_loop().create_task(
            self._typing_loop(key, state)
        )

    def _rearm_typing(self, phone_number_id: str, wa_id: str) -> None:
        state = self._typing.get((phone_number_id, wa_id))
        if state is None:
            return
        if state.task is not None:
            state.task.cancel()
        self._start_typing((phone_number_id, wa_id), state)

    def _stop_typing(self, phone_number_id: str, wa_id: str) -> None:
        state = self._typing.pop((phone_number_id, wa_id), None)
        if state is not None and state.task is not None:
            state.task.cancel()

    async def _typing_loop(self, key: tuple[str, str], state: _Typing) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                await self._mark_read_typing(state)
                await asyncio.sleep(_TYPING_EVERY_S)
                if loop.time() >= state.deadline or not await state.turn_active():
                    break
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("[whatsapp] typing keep-alive failed", exc_info=True)
        if self._typing.get(key) is state:
            del self._typing[key]

    async def _mark_read_typing(self, state: _Typing) -> None:
        """One request sets blue double-checkmarks *and* the typing pip."""
        _, error = await send_message(
            self._http,
            token=state.token,
            phone_number_id=state.phone_number_id,
            payload={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": state.wamid,
                "typing_indicator": {"type": "text"},
            },
            api_version=state.api_version,
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
