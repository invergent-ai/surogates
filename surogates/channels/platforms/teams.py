"""Microsoft Teams channel platform strategy (Bot Framework, webhook).

Each tenant brings their own Azure Bot (single-tenant): its Microsoft App ID
is the routing identifier and the ``{app_id}`` of the messaging endpoint
``/teams/{app_id}`` the customer pastes into the Azure portal by hand.  The
client secret (``app_password``) and the Entra tenant id (``tenant_id``) live
in the vault.

Inbound
-------
Bot Framework signs no body: it sends a bearer JWT that proves the request
comes from the Bot Framework service and is meant for *this* bot.  ``verify``
checks it against the OpenID metadata's JWKS (RS256, issuer
``https://api.botframework.com``, audience = the App ID from the path, 5 min
skew) and requires the token's ``serviceUrl`` claim to equal the activity's
``serviceUrl`` — the URL replies are later POSTed to with our own token, so an
activity naming any other host is refused here rather than trusted there.

Outbound
--------
A client-credentials token from Entra (``{login}/{tenant}/oauth2/v2.0/token``,
scope ``https://api.botframework.com/.default``), cached per App ID until a
minute before it expires, authorises ``POST {serviceUrl}/v3/conversations/
{id}/activities`` (send, typing) and ``PUT …/activities/{activityId}`` (edit).

Conversation references
-----------------------
Teams cannot address a person or a channel by id alone: every write needs the
conversation id *and* the regional ``serviceUrl`` it came from.  Every
verified activity therefore records its conversation reference in Redis for
90 days, keyed by conversation and, for a personal chat, by the person — the
only way a later proactive message (an opener, an outreach text, a team
alert) can find where to write.  Replies need nothing from Redis: the
``serviceUrl`` and tenant ride in session config (``teams_service_url``,
``teams_tenant_id``, ``teams_conversation_id``).

Both base URLs are overridable so a local stack runs against
``scripts/fake_teams.py``; production never sets them.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import mimetypes
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from jose import jwt

from surogates.channels.base import SendResult
from surogates.channels.capabilities import ChannelCapabilities
from surogates.channels.inbound import InboundFileRef, InboundMessage
from surogates.channels.registry import ChannelDescriptor
from surogates.channels.text_split import split_text

__all__ = [
    "BOT_FRAMEWORK_ISSUER",
    "TeamsPlatform",
    "conversation_ref",
    "parse",
]

logger = logging.getLogger(__name__)

#: Entra ID token endpoint base.  Overridable only for the local fake.
TEAMS_LOGIN_BASE = (
    os.environ.get("SUROGATES_TEAMS_LOGIN_BASE") or "https://login.microsoftonline.com"
).rstrip("/")

#: Bot Framework OpenID metadata (its ``jwks_uri`` signs inbound tokens).
#: Overridable only for the local fake.
TEAMS_OPENID_URL = (
    os.environ.get("SUROGATES_TEAMS_OPENID_URL")
    or "https://login.botframework.com/v1/.well-known/openidconfiguration"
)

BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
_SCOPE = "https://api.botframework.com/.default"

#: Bot Framework allows 5 minutes of clock skew on inbound tokens.
_LEEWAY_S = 300
#: Signing keys are re-read daily, and sooner when a token names an unknown
#: kid (a key rotation) — but at most once per this many seconds, so a stream
#: of garbage kids cannot turn every request into a fetch from Microsoft.
_JWKS_TTL_S = 24 * 3600
_KID_REFETCH_S = 60
#: Refresh the outbound token this long before Entra says it expires.
_TOKEN_MARGIN_S = 60

#: How long a conversation reference is kept for proactive messages.
_REF_TTL_S = 90 * 24 * 3600

#: Teams typing lasts about three seconds; refreshed until the turn ends,
#: capped so a stuck turn cannot type forever.
_TYPING_EVERY_S = 3.0
_TYPING_CAP_S = 600.0

#: Inline images ride as data URIs; Teams refuses large ones.
_INLINE_IMAGE_MAX = 1024 * 1024

_AT_TAG = re.compile(r"<at>(.*?)</at>", re.IGNORECASE | re.DOTALL)
_THREAD_MARK = ";messageid="


# ---------------------------------------------------------------------------
# Conversation references
# ---------------------------------------------------------------------------


def _ref_key(app_id: str, *, conversation_id: str | None = None, user: str | None = None) -> str:
    if user is not None:
        return f"teams:personal:{app_id}:{user}"
    return f"teams:conversation:{app_id}:{conversation_id}"


def _user_key(sender: dict) -> str:
    """The person's id: ``aadObjectId`` is the same for every bot, ``id`` is not."""
    return str(sender.get("aadObjectId") or sender.get("id") or "")


def _tenant_of(activity: dict) -> str:
    conversation = activity.get("conversation") or {}
    return str(
        conversation.get("tenantId")
        or ((activity.get("channelData") or {}).get("tenant") or {}).get("id")
        or ""
    )


def _split_conversation(conversation_id: str) -> tuple[str, str | None]:
    """``19:x@thread.tacv2;messageid=42`` → (``19:x@thread.tacv2``, ``42``).

    A channel message carries its thread's root in the conversation id; the
    base id is the channel, the root is the thread.
    """
    base, sep, root = conversation_id.partition(_THREAD_MARK)
    return base, (root or None) if sep else None


async def _remember_conversation(redis: Any, app_id: str, activity: dict) -> None:
    conversation = activity.get("conversation") or {}
    service_url = activity.get("serviceUrl") or ""
    if redis is None or not app_id or not conversation.get("id") or not service_url:
        return
    base, _root = _split_conversation(str(conversation["id"]))
    kind = str(conversation.get("conversationType") or "")
    ref = json.dumps({
        "conversation_id": base,
        "service_url": service_url,
        "tenant_id": _tenant_of(activity),
        "conversation_type": kind,
    })
    await redis.set(_ref_key(app_id, conversation_id=base), ref, ex=_REF_TTL_S)
    user = _user_key(activity.get("from") or {})
    if kind == "personal" and user:
        await redis.set(_ref_key(app_id, user=user), ref, ex=_REF_TTL_S)


async def conversation_ref(
    redis: Any, app_id: str, *, conversation_id: str | None = None, user: str | None = None,
) -> dict | None:
    """The recorded reference of a conversation, or of a person's personal chat.

    Returns ``{conversation_id, service_url, tenant_id, conversation_type}`` or
    ``None`` when the bot has never heard from it (or 90 days have passed).
    """
    raw = await redis.get(_ref_key(app_id, conversation_id=conversation_id, user=user))
    if not raw:
        return None
    try:
        ref = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return ref if isinstance(ref, dict) else None


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def parse(body: dict, *, app_id: str) -> InboundMessage | None:
    """Map a Bot Framework ``message`` activity to an :class:`InboundMessage`.

    ``None`` for every other activity type, for the bot's own messages and for
    a message with neither text nor a downloadable file.  Never raises.
    """
    try:
        return _parse(body, app_id=app_id)
    except Exception:
        logger.debug("parse: unexpected error parsing Teams activity", exc_info=True)
        return None


def _parse(body: dict, *, app_id: str) -> InboundMessage | None:
    if body.get("type") != "message":
        return None
    sender = body.get("from") or {}
    bot_id = str((body.get("recipient") or {}).get("id") or "")
    # Loop safety: the bot's own message never comes back as a user's.
    if not sender.get("id") or sender.get("id") == bot_id:
        return None

    conversation = body.get("conversation") or {}
    kind = str(conversation.get("conversationType") or "")
    base, thread_root = _split_conversation(str(conversation.get("id") or ""))
    is_dm = kind == "personal"

    is_mention = False
    text = str(body.get("text") or "")
    for entity in body.get("entities") or []:
        if entity.get("type") != "mention":
            continue
        if bot_id and (entity.get("mentioned") or {}).get("id") == bot_id:
            is_mention = True
            if entity.get("text"):
                text = text.replace(entity["text"], "")
    # Other people's mentions stay as their plain names.
    text = html.unescape(_AT_TAG.sub(r"\1", text)).strip()

    files = _extract_files(body.get("attachments") or [])
    if not text and not files:
        return None

    msg_kind = "text"
    if files:
        mime = files[0].mime_type
        msg_kind = "image" if mime.startswith("image/") else (
            "audio" if mime.startswith("audio/") else "document"
        )

    user = _user_key(sender)
    tenant = _tenant_of(body)
    return InboundMessage(
        kind=msg_kind,
        identifier=base,
        thread_key=thread_root,
        platform_user_id=user,
        user_name=str(sender.get("name") or user),
        text=text,
        media_urls=[],
        media_types=[],
        is_dm=is_dm,
        is_mention=is_mention,
        ts=f"teams:{body.get('id')}",
        source={
            "platform": "teams",
            "chat_type": kind,
            "activity_id": body.get("id"),
            "app_id": app_id,
            # ``teams_*`` keys are copied into session config by the inbound
            # pipeline: delivery reads them to reach this conversation.
            "teams_conversation_id": base,
            "teams_service_url": body.get("serviceUrl") or "",
            "teams_tenant_id": tenant,
        },
        visibility="dm" if is_dm else "private",
        files=files,
    )


def _extract_files(attachments: list) -> list[InboundFileRef]:
    """Files a person shared: Teams hands the bot a pre-authorised ``downloadUrl``."""
    files: list[InboundFileRef] = []
    for attachment in attachments:
        content = attachment.get("content")
        if not isinstance(content, dict) or not content.get("downloadUrl"):
            continue
        name = str(attachment.get("name") or "file")
        files.append(InboundFileRef(
            url=content["downloadUrl"],
            filename=name,
            mime_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            size=None,
        ))
    return files


# ---------------------------------------------------------------------------
# TeamsPlatform
# ---------------------------------------------------------------------------


@dataclass
class _Typing:
    task: asyncio.Task | None = None


class TeamsPlatform:
    """Webhook-based Microsoft Teams channel (one customer Azure Bot per routing)."""

    kind = "teams"
    topology = "webhook"
    supports_edit = True
    #: A bot can write only into a conversation it was installed in, which
    #: it learns of when someone writes to it (or installs it).
    capabilities = ChannelCapabilities(
        can_initiate=True, needs_prior_contact=True, opener="text",
        identity="member", label="Microsoft Teams",
    )

    #: Teams rejects activities over ~28 KB (MessageSizeTooBig); split under it.
    _MAX_MESSAGE_CHARS = 24000

    descriptor = ChannelDescriptor(
        vault_refs=lambda identifier: {
            "app_password": "app_password",
            "tenant_id": "tenant_id",
        },
        config_keys=("require_mention", "identity_policy", "multi_session"),
        # The messaging endpoint is pasted into the Azure Bot by hand.
        webhook_registration="manual",
    )

    def __init__(self) -> None:
        # One client for every tenant: the bearer token is per request.
        self._http = httpx.AsyncClient(timeout=30)
        self._jwks: dict[str, dict] = {}
        self._jwks_at = 0.0
        self._jwks_lock = asyncio.Lock()
        # (app_id, tenant) -> (access token, monotonic expiry)
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}
        # conversation id -> the typing keep-alive of the turn under way.
        self._typing: dict[str, _Typing] = {}

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def route_path(self, identifier: str | None = None) -> str:
        if identifier is None:
            return "/teams/{app_id}"
        return f"/teams/{identifier}"

    def identifier_of(self, request: Any, body: Any) -> str:
        return request.path_params["app_id"]

    # ------------------------------------------------------------------
    # verify — Bot Framework JWT
    # ------------------------------------------------------------------

    async def _signing_key(self, kid: str) -> dict | None:
        async with self._jwks_lock:
            age = time.monotonic() - self._jwks_at
            stale = not self._jwks or age > _JWKS_TTL_S
            rotated = kid not in self._jwks and age >= _KID_REFETCH_S
            if stale or rotated:
                meta = (await self._http.get(TEAMS_OPENID_URL)).raise_for_status().json()
                keys = (await self._http.get(meta["jwks_uri"])).raise_for_status().json()
                self._jwks = {k["kid"]: k for k in keys.get("keys") or [] if k.get("kid")}
                self._jwks_at = time.monotonic()
            return self._jwks.get(kid)

    async def verify(self, request: Any, raw_body: bytes, *, creds: dict) -> bool:
        """Accept only a Bot Framework token minted for this routing's App ID."""
        auth = str(request.headers.get("Authorization") or request.headers.get("authorization") or "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        app_id = self.identifier_of(request, None)
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                return False
            key = await self._signing_key(str(header.get("kid") or ""))
            if key is None:
                logger.info("[teams] token signed by unknown kid %r", header.get("kid"))
                return False
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=app_id,
                issuer=BOT_FRAMEWORK_ISSUER,
                options={"leeway": _LEEWAY_S, "require_exp": True},
            )
            activity = json.loads(raw_body)
        except Exception as exc:
            logger.info("[teams] rejected inbound token for %s: %s", app_id, exc)
            return False
        if not isinstance(activity, dict):
            return False
        # Replies are POSTed (with our token) to the activity's serviceUrl, so
        # it must be the one Microsoft signed.
        if claims.get("serviceUrl") != activity.get("serviceUrl"):
            logger.warning("[teams] serviceUrl claim does not match the activity for %s", app_id)
            return False
        endorsements = key.get("endorsements")
        if endorsements and activity.get("channelId") not in endorsements:
            return False
        return True

    # ------------------------------------------------------------------
    # parse / non-message activities
    # ------------------------------------------------------------------

    def parse(
        self, body: Any, *, creds: dict | None = None, identifier: str | None = None,
    ) -> InboundMessage | None:
        return parse(body, app_id=identifier or "")

    async def handle_non_message_update(
        self, body: Any, *, routing: Any, creds: dict, deps: Any,
    ) -> bool:
        """Record the conversation reference of every activity; consume all but messages.

        ``conversationUpdate`` (the bot was installed in a chat or team) is
        what lets the agent write there before anyone has spoken; a message
        refreshes the reference and goes on to ``parse``.
        """
        try:
            await _remember_conversation(
                getattr(deps, "redis", None), getattr(routing, "identifier", ""), body,
            )
        except Exception:
            logger.warning("[teams] recording the conversation reference failed", exc_info=True)
        return body.get("type") != "message"

    # ------------------------------------------------------------------
    # Outbound transport
    # ------------------------------------------------------------------

    async def _token(self, app_id: str, creds: dict) -> tuple[str | None, str]:
        """A Bot Framework access token for *app_id*, cached.  Never raises."""
        tenant = str((creds or {}).get("tenant_id") or "")
        secret = str((creds or {}).get("app_password") or "")
        if not app_id or not tenant or not secret:
            return None, "teams credentials missing (app id, tenant or secret)"
        cached = self._tokens.get((app_id, tenant))
        if cached and cached[1] > time.monotonic():
            return cached[0], ""
        try:
            resp = await self._http.post(
                f"{TEAMS_LOGIN_BASE}/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials", "client_id": app_id,
                    "client_secret": secret, "scope": _SCOPE,
                },
            )
            data = resp.json()
        except Exception as exc:
            return None, f"teams token request failed: {exc}"
        token = data.get("access_token") if isinstance(data, dict) else None
        if resp.status_code != 200 or not token:
            error = data.get("error", "") if isinstance(data, dict) else ""
            desc = data.get("error_description", "") if isinstance(data, dict) else ""
            return None, f"teams token error {resp.status_code} {error}: {str(desc)[:200]}"
        expires = time.monotonic() + float(data.get("expires_in") or 0) - _TOKEN_MARGIN_S
        self._tokens[(app_id, tenant)] = (token, expires)
        return token, ""

    async def _call(
        self, method: str, url: str, token: str, body: dict,
    ) -> tuple[dict | None, str]:
        """One connector call.  ``(response json, "")`` or ``(None, error)``; never raises."""
        try:
            resp = await self._http.request(
                method, url, json=body, headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            return None, f"teams request failed: {exc}"
        if 200 <= resp.status_code < 300:
            try:
                return resp.json() or {}, ""
            except ValueError:
                return {}, ""
        try:
            err = resp.json().get("error") or {}
            code, message = err.get("code", ""), err.get("message", "")
        except Exception:
            code, message = "", resp.text
        # "teams error 404 " is matched as a permanent failure: keep the form.
        return None, f"teams error {resp.status_code} {code}: {str(message)[:200]}"

    @staticmethod
    def _activities_url(destination: dict) -> str | None:
        service_url = str(destination.get("service_url") or "")
        conversation = str(destination.get("conversation_id") or "")
        if not service_url or not conversation:
            return None
        root = destination.get("reply_to_id")
        if root:
            conversation = f"{conversation}{_THREAD_MARK}{root}"
        quoted = urllib.parse.quote(conversation, safe="")
        return f"{service_url.rstrip('/')}/v3/conversations/{quoted}/activities"

    async def _prepare(self, destination: dict, creds: dict) -> tuple[str | None, str | None, str]:
        """``(activities url, token, error)`` for a destination."""
        url = self._activities_url(destination)
        if url is None:
            return None, None, "no teams conversation reference for this destination"
        token, error = await self._token(str(destination.get("channel_identifier") or ""), creds)
        return url, token, error

    # ------------------------------------------------------------------
    # send
    # ------------------------------------------------------------------

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        """Post an outbox item as markdown, editing the progress message when asked.

        A reply over the size cap is split; when a later chunk fails, the
        delivered prefix is reported as success so a retry cannot repeat it.
        """
        destination = item.destination
        url, token, error = await self._prepare(destination, creds)
        if url is None or token is None:
            return SendResult(success=False, error=error)

        text: str = item.payload.get("content", "")
        chunks = split_text(text, self._MAX_MESSAGE_CHARS) or [text]
        root = destination.get("reply_to_id")

        last_id: str | None = None
        update_id = destination.get("update_ts")
        if update_id:
            edited, error = await self._call("PUT", f"{url}/{update_id}", token, {
                "type": "message", "id": update_id, "text": chunks[0], "textFormat": "markdown",
            })
            if edited is not None:
                last_id = str(edited.get("id") or update_id)
                chunks = chunks[1:]
            else:
                logger.warning("[teams] edit failed (%s); posting a fresh message", error)

        for chunk in chunks:
            activity: dict[str, Any] = {"type": "message", "text": chunk, "textFormat": "markdown"}
            if root:
                activity["replyToId"] = root
            posted, error = await self._call("POST", url, token, activity)
            if posted is None:
                if last_id is not None:
                    return SendResult(success=True, message_id=last_id)
                return SendResult(success=False, error=error)
            last_id = str(posted.get("id") or "") or last_id
        return SendResult(success=True, message_id=last_id)

    async def send_files(self, item: Any, *, creds: dict, files: list) -> list[str]:
        """Post each image as an inline attachment; returns the activity ids.

        A bot can only attach a file it can link to (Teams has no upload API
        for bots outside SharePoint/Graph), so only images small enough for a
        data URI go; anything else is skipped — and a reply made of nothing
        else gets the dispatcher's "couldn't upload" note.  Never raises.
        """
        url, token, error = await self._prepare(item.destination, creds)
        if url is None or token is None:
            logger.warning("[teams] send_files: %s", error)
            return []
        sent: list[str] = []
        for f in files:
            if not (f.mime_type or "").startswith("image/") or len(f.data) > _INLINE_IMAGE_MAX:
                logger.info("[teams] %s cannot be attached on Teams; skipped", f.filename)
                continue
            data_uri = f"data:{f.mime_type};base64,{base64.b64encode(f.data).decode()}"
            activity: dict[str, Any] = {
                "type": "message",
                "attachments": [{"contentType": f.mime_type, "name": f.filename, "contentUrl": data_uri}],
            }
            if item.destination.get("reply_to_id"):
                activity["replyToId"] = item.destination["reply_to_id"]
            posted, error = await self._call("POST", url, token, activity)
            if posted is None:
                logger.warning("[teams] attaching %s failed: %s", f.filename, error)
                continue
            sent.append(str(posted.get("id") or f.filename))
        return sent

    async def download_file(self, *, creds: dict, url: str, max_bytes: int) -> bytes | None:
        """Fetch a shared file from its pre-authorised ``downloadUrl``.  Never raises.

        No credential is sent: the URL is its own authorisation.  Only https
        is followed (plain http only to a loopback host, for the local fake).
        """
        parsed = urllib.parse.urlparse(url or "")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
        ):
            return None
        try:
            async with self._http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                body = bytearray()
                async for part in resp.aiter_bytes():
                    body.extend(part)
                    if len(body) > max_bytes:
                        return None
                return bytes(body)
        except Exception:
            logger.warning("[teams] download failed", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # keep_typing
    # ------------------------------------------------------------------

    def keep_typing(
        self, msg: Any, *, creds: dict, turn_active: Callable[[], Awaitable[bool]],
    ) -> None:
        """Show "typing…" in *msg*'s conversation while its turn runs.  Returns at once."""
        source = getattr(msg, "source", None) or {}
        destination = {
            "conversation_id": source.get("teams_conversation_id"),
            "service_url": source.get("teams_service_url"),
            "reply_to_id": getattr(msg, "thread_key", None),
            "channel_identifier": source.get("app_id"),
        }
        key = str(destination["conversation_id"] or "")
        if not key or not destination["service_url"]:
            return
        previous = self._typing.pop(key, None)
        if previous is not None and previous.task is not None:
            previous.task.cancel()
        state = _Typing()
        self._typing[key] = state
        state.task = asyncio.get_running_loop().create_task(
            self._typing_loop(key, state, destination, creds, turn_active),
        )

    async def _typing_loop(
        self, key: str, state: _Typing, destination: dict, creds: dict,
        turn_active: Callable[[], Awaitable[bool]],
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TYPING_CAP_S
        try:
            while True:
                url, token, _error = await self._prepare(destination, creds)
                if url is None or token is None:
                    break
                await self._call("POST", url, token, {"type": "typing"})
                await asyncio.sleep(_TYPING_EVERY_S)
                if loop.time() >= deadline or not await turn_active():
                    break
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("[teams] typing keep-alive failed", exc_info=True)
        finally:
            if self._typing.get(key) is state:
                self._typing.pop(key, None)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


def _register() -> None:
    from surogates.channels.registry import registry

    if registry.get("teams") is None:
        registry.register(TeamsPlatform())


_register()
