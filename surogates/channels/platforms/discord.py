"""Discord channel platform strategy (socket topology).

Discord has no webhook for ordinary messages: a bot receives them over the
Gateway, a websocket it keeps open itself.  So this platform mounts no route.
:mod:`surogates.channels.gateway` holds one connection per bot and hands each
``MESSAGE_CREATE`` to the dispatcher's shared ``ingest()``, wrapped as::

    {"t": "MESSAGE_CREATE", "d": <message>, "bot_user_id": "<our bot's user id>"}

The bot user id rides along because only the connection knows it (READY
carries it); :meth:`DiscordPlatform.parse` needs it to drop the bot's own
messages and to see a mention.

Everything outbound is plain REST with ``Authorization: Bot <token>``:
messages (2 000-character cap, edited in place for progress), typing, files
as multipart, and opening a DM channel for writing first.

The identifier is the customer's application id; the one credential is
``bot_token``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable

import httpx

from surogates.channels.base import SendResult
from surogates.channels.capabilities import ChannelCapabilities
from surogates.channels.inbound import InboundFileRef, InboundMessage
from surogates.channels.registry import ChannelDescriptor
from surogates.channels.text_split import split_text

__all__ = ["DISCORD_API_BASE", "DiscordPlatform", "open_dm", "parse"]

logger = logging.getLogger(__name__)

#: The REST API.  Overridable only so a local stack can run end to end against
#: ``scripts/fake_discord.py``; production never sets it.
DISCORD_API_BASE = (
    os.environ.get("SUROGATES_DISCORD_API_BASE") or "https://discord.com/api/v10"
).rstrip("/")

#: Message types a person writes: DEFAULT and REPLY.  Everything else (joins,
#: pins, boosts, thread-created notices, …) is a system message.
_USER_MESSAGE_TYPES = frozenset({0, 19})

#: Never ping anyone: the agent's text may echo ``@everyone`` or a user id it
#: read, and a reply must not notify the author a second time either.
_NO_MENTIONS = {"parse": [], "replied_user": False}

#: How long a typing indicator lasts (~10 s) minus a margin, and how long one
#: turn may keep it up at most.
_TYPING_EVERY_S = 8.0
_TYPING_CAP_S = 300.0


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {token}"}


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def parse(body: dict, *, bot_user_id: str) -> InboundMessage | None:
    """Map a ``MESSAGE_CREATE`` dispatch to an :class:`InboundMessage`, or None.

    ``None`` for anything that is not a person's message, for the bot's own
    messages, and whenever ``bot_user_id`` is unknown (fail closed: without
    it the bot cannot tell its own messages apart and would answer itself).
    """
    try:
        return _parse(body, bot_user_id=bot_user_id)
    except Exception:
        logger.debug("parse: unexpected error parsing a Discord message", exc_info=True)
        return None


def _parse(body: dict, *, bot_user_id: str) -> InboundMessage | None:
    if body.get("t") != "MESSAGE_CREATE":
        return None
    d: dict = body.get("d") or {}
    if not bot_user_id or d.get("type", 0) not in _USER_MESSAGE_TYPES:
        return None

    author: dict = d.get("author") or {}
    author_id = str(author.get("id") or "")
    if not author_id or author_id == bot_user_id:
        return None
    # Webhook posts carry a synthetic author; treat them as another bot so
    # the pipeline's ``allow_bots`` gate decides.
    is_bot = bool(author.get("bot")) or bool(d.get("webhook_id"))

    channel_id = str(d.get("channel_id") or "")
    is_dm = not d.get("guild_id")
    mentioned = any(str(m.get("id")) == bot_user_id for m in d.get("mentions") or [])
    replied = str(
        ((d.get("referenced_message") or {}).get("author") or {}).get("id") or ""
    ) == bot_user_id

    text = re.sub(rf"<@!?{re.escape(bot_user_id)}>", "", d.get("content") or "")
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    files = [
        InboundFileRef(
            url=a.get("url") or "",
            filename=a.get("filename") or f"attachment-{a.get('id')}",
            mime_type=a.get("content_type") or "application/octet-stream",
            size=a.get("size"),
            file_id=str(a.get("id") or "") or None,
        )
        for a in d.get("attachments") or []
        if a.get("url")
    ]
    if not text and not files:
        # In a server without the Message Content intent, Discord blanks the
        # content of every message that does not mention the bot.
        return None

    member: dict = d.get("member") or {}
    user_name = (
        member.get("nick") or author.get("global_name") or author.get("username") or author_id
    )
    kind = "text"
    if files:
        mime = files[0].mime_type
        kind = "image" if mime.startswith("image/") else "audio" if mime.startswith("audio/") else "document"

    source: dict = {
        "platform": "discord",
        "chat_type": "dm" if is_dm else "guild",
        "chat_id": channel_id,
        "message_id": str(d.get("id") or ""),
    }
    if d.get("guild_id"):
        source["guild_id"] = str(d["guild_id"])

    return InboundMessage(
        kind=kind,
        identifier=channel_id,
        # A Discord thread is a channel of its own, so it needs no thread key.
        thread_key=None,
        platform_user_id=author_id,
        user_name=str(user_name),
        text=text,
        media_urls=[],
        media_types=[],
        is_dm=is_dm,
        is_mention=mentioned or replied,
        ts=f"discord:{d.get('id')}",
        source=source,
        is_bot=is_bot,
        # Fail closed: no Discord surface collapses to shared memory.
        visibility="dm" if is_dm else "private",
        files=files,
    )


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


def _error(resp: httpx.Response) -> str:
    """``discord error <code> (<message>)`` — the prefix the permanent-error
    list matches on, so only Discord's own JSON codes can mark a row dead."""
    try:
        data = resp.json()
    except Exception:
        return f"discord http {resp.status_code}"
    code = data.get("code") if isinstance(data, dict) else None
    message = (data.get("message") if isinstance(data, dict) else None) or resp.reason_phrase
    if code:
        return f"discord error {code} ({message})"
    return f"discord http {resp.status_code} ({message})"


async def _request(
    http: httpx.AsyncClient, method: str, path: str, *, token: str, **kwargs: Any,
) -> tuple[dict | None, str, bool]:
    """One REST call.  Never raises.

    Returns ``(body, "", False)`` on success (``{}`` for a 204 or a body
    that is not a JSON object), ``(None, error, retryable)`` otherwise.  A 429
    is handed back as retryable at once: this runs inside the shared delivery
    loop, so waiting out ``retry_after`` here would hold up every other
    channel's rows; the outbox retries it later.
    """
    url = f"{DISCORD_API_BASE}{path}"
    try:
        resp = await http.request(method, url, headers=_headers(token), **kwargs)
    except httpx.HTTPError as exc:
        logger.warning("[discord] %s %s failed: %s", method, path, exc)
        return None, f"discord transport error: {exc}", True
    if resp.status_code < 300:
        try:
            body = resp.json() if resp.status_code != 204 and resp.content else {}
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}, "", False
    return None, _error(resp), resp.status_code == 429 or resp.status_code >= 500


async def open_dm(http: httpx.AsyncClient, token: str, user_id: str) -> tuple[str | None, str]:
    """Open (or find) the DM channel with *user_id*; ``(channel_id, error)``."""
    data, error, _ = await _request(
        http, "POST", "/users/@me/channels", token=token, json={"recipient_id": user_id},
    )
    if data is None:
        return None, error
    return str(data.get("id") or "") or None, ""


# ---------------------------------------------------------------------------
# DiscordPlatform
# ---------------------------------------------------------------------------


class DiscordPlatform:
    """Gateway-fed Discord platform; implements
    :class:`~surogates.channels.registry.ChannelPlatform`."""

    kind = "discord"
    topology = "socket"
    supports_edit = True
    #: A bot can DM someone it shares a server with; ``needs_prior_contact``
    #: because we only know people who have written to it.
    capabilities = ChannelCapabilities(
        can_initiate=True, needs_prior_contact=True, opener="text",
        identity="user", label="Discord",
    )
    descriptor = ChannelDescriptor(
        vault_refs=lambda identifier: {"bot_token": "bot_token"},
        config_keys=(
            "require_mention",
            "free_response_channels",
            "allow_bots",
            "message_content",
            "multi_session",
        ),
        # No webhook at all: a routing change wakes the Gateway supervisor.
        webhook_registration="manual",
    )

    _MAX_MESSAGE_CHARS = 2000

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=30.0)
        # (bot token, channel id) -> (task, turn_active).  Keyed on the bot as
        # well: two bots in one channel each hold their own indicator.
        self._typing: dict[tuple[str, str], tuple[asyncio.Task, Callable[[], Awaitable[bool]]]] = {}

    # Socket platform: these exist for the protocol; nothing routes to them.
    def route_path(self, identifier: str | None = None) -> str:
        return f"/discord/{identifier or '{app_id}'}"

    def identifier_of(self, request: Any, body: Any) -> str:
        return request.path_params["app_id"]

    def verify(self, request: Any, raw_body: bytes, *, creds: dict) -> bool:
        return False  # never reached: no Discord webhook is mounted

    async def parse(
        self, body: Any, *, creds: dict | None = None, identifier: str | None = None,
    ) -> InboundMessage | None:
        return parse(body, bot_user_id=str((body or {}).get("bot_user_id") or ""))

    # ------------------------------------------------------------------
    # send / edit
    # ------------------------------------------------------------------

    async def _post(self, token: str, channel_id: str, body: dict) -> tuple[str | None, str, bool]:
        data, error, retryable = await _request(
            self._http, "POST", f"/channels/{channel_id}/messages", token=token,
            json={**body, "allowed_mentions": _NO_MENTIONS},
        )
        return (str(data.get("id")) if data is not None else None), error, retryable

    async def send(self, item: Any, *, creds: dict) -> SendResult:
        """Post (or, with ``update_ts``, edit) an outbox item; never raises.

        Split at 2 000 characters; the first chunk replies to
        ``reply_to_message_id`` when the session carries one.  When a later
        chunk fails, the delivered prefix is reported as success so a retry
        cannot repeat it.
        """
        token: str = (creds or {}).get("bot_token") or ""
        destination = item.destination or {}
        channel_id = str(destination.get("channel_id") or "")
        if not token or not channel_id:
            return SendResult(success=False, error="discord: no bot token or channel")
        text: str = item.payload.get("content") or ""
        chunks = split_text(text, self._MAX_MESSAGE_CHARS) or [text]

        last_id: str | None = None
        update_ts = destination.get("update_ts")
        if update_ts:
            data, error, _ = await _request(
                self._http, "PATCH", f"/channels/{channel_id}/messages/{update_ts}",
                token=token, json={"content": chunks[0], "allowed_mentions": _NO_MENTIONS},
            )
            if data is not None:
                last_id = str(data.get("id") or update_ts)
                chunks = chunks[1:]
            else:
                logger.warning("[discord] edit failed (%s); posting a fresh message", error)

        reply_to = destination.get("reply_to_message_id")
        for index, chunk in enumerate(chunks):
            body: dict[str, Any] = {"content": chunk}
            if reply_to and index == 0 and last_id is None:
                # fail_if_not_exists: a deleted target degrades to a plain post.
                body["message_reference"] = {
                    "message_id": str(reply_to), "fail_if_not_exists": False,
                }
            msg_id, error, retryable = await self._post(token, channel_id, body)
            if msg_id is None:
                if last_id is not None:
                    return SendResult(success=True, message_id=last_id)
                return SendResult(success=False, error=error, retryable=retryable)
            last_id = msg_id

        # A message clears Discord's typing indicator: text beside a tool call
        # means the turn goes on, so put it back; an answer ends it.
        if item.payload.get("intermediate") or item.payload.get("code_run"):
            self._rearm_typing(token, channel_id)
        else:
            self._stop_typing(token, channel_id)
        return SendResult(success=True, message_id=last_id)

    async def delete_message(self, *, creds: dict, channel: str, ts: str) -> None:
        """Delete a message (placeholder cleanup).  Best-effort; never raises."""
        token = (creds or {}).get("bot_token") or ""
        if token and channel and ts:
            _, error, _ = await _request(
                self._http, "DELETE", f"/channels/{channel}/messages/{ts}", token=token,
            )
            if error:
                logger.warning("[discord] delete %s/%s failed: %s", channel, ts, error)

    async def send_files(self, item: Any, *, creds: dict, files: list) -> list[str]:
        """Post each file as its own message (multipart); returns message ids.

        Best-effort per file, never raises.  No caption: the delivery loop
        posts the surrounding text itself.
        """
        token = (creds or {}).get("bot_token") or ""
        channel_id = str((item.destination or {}).get("channel_id") or "")
        sent: list[str] = []
        if not token or not channel_id:
            return sent
        for f in files:
            payload = {
                "attachments": [{"id": 0, "filename": f.filename}],
                "allowed_mentions": _NO_MENTIONS,
            }
            data, error, _ = await _request(
                self._http, "POST", f"/channels/{channel_id}/messages", token=token,
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": (f.filename, f.data, f.mime_type or "application/octet-stream")},
            )
            if data is None:
                logger.warning("[discord] upload of %s failed: %s", f.filename, error)
                continue
            sent.append(str(data.get("id")))
        return sent

    async def post_input_nudge(
        self, *, creds: dict, channel: str, thread_ts: Any, text: str,
    ) -> str | None:
        """Post a short status line (``/stop`` ack); its id, or None."""
        token = (creds or {}).get("bot_token") or ""
        if not token or not channel:
            return None
        msg_id, _, _ = await self._post(token, channel, {"content": text})
        return msg_id

    async def send_private(
        self, creds: dict, *, sender_id: str, chat_id: str, is_dm: bool, text: str,
        buttons: list[str] | None = None,
    ) -> bool:
        """DM *sender_id* (a link prompt, never into a server channel)."""
        token = (creds or {}).get("bot_token") or ""
        if not token or not sender_id:
            return False
        dm_id = chat_id if is_dm else None
        if dm_id is None:
            dm_id, error = await open_dm(self._http, token, sender_id)
            if dm_id is None:
                logger.warning("[discord] cannot DM %s: %s", sender_id, error)
                return False
        msg_id, _, _ = await self._post(token, dm_id, {"content": text})
        return msg_id is not None

    async def download_file(self, *, creds: dict, url: str, max_bytes: int) -> bytes | None:
        """Fetch an attachment from Discord's CDN (signed URL, no auth header).

        Streamed, so a file over *max_bytes* is dropped without being read
        whole.  Never raises.
        """
        try:
            async with self._http.stream("GET", url) as resp:
                if resp.status_code >= 300:
                    return None
                body = bytearray()
                async for part in resp.aiter_bytes():
                    body.extend(part)
                    if len(body) > max_bytes:
                        return None
                return bytes(body)
        except Exception as exc:  # noqa: BLE001 — a bad URL is no file, not a crash
            logger.warning("[discord] attachment download failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # keep_typing — "Surogate is typing…" held for the turn
    # ------------------------------------------------------------------

    def keep_typing(
        self, msg: Any, *, creds: dict, turn_active: Callable[[], Awaitable[bool]],
    ) -> None:
        """Show typing in *msg*'s channel every 8 s while the turn runs.

        Stops on an answer (see :meth:`send`), when *turn_active* says the turn
        is over, or at the cap.  Returns at once; never raises.
        """
        token = (creds or {}).get("bot_token") or ""
        channel_id = getattr(msg, "identifier", "") or ""
        if token and channel_id:
            self._start_typing(token, channel_id, turn_active)

    def _start_typing(
        self, token: str, channel_id: str, turn_active: Callable[[], Awaitable[bool]],
    ) -> None:
        self._stop_typing(token, channel_id)
        task = asyncio.get_running_loop().create_task(
            self._typing_loop(token, channel_id, turn_active),
        )
        self._typing[(token, channel_id)] = (task, turn_active)

    def _rearm_typing(self, token: str, channel_id: str) -> None:
        held = self._typing.get((token, channel_id))
        if held is not None:
            self._start_typing(token, channel_id, held[1])

    def _stop_typing(self, token: str, channel_id: str) -> None:
        held = self._typing.pop((token, channel_id), None)
        if held is not None:
            held[0].cancel()

    async def _typing_loop(
        self, token: str, channel_id: str, turn_active: Callable[[], Awaitable[bool]],
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _TYPING_CAP_S
        try:
            while loop.time() < deadline:
                await _request(self._http, "POST", f"/channels/{channel_id}/typing", token=token)
                await asyncio.sleep(_TYPING_EVERY_S)
                if not await turn_active():
                    break
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("[discord] typing keep-alive failed", exc_info=True)
        held = self._typing.get((token, channel_id))
        if held is not None and held[0] is asyncio.current_task():
            del self._typing[(token, channel_id)]


def _register() -> None:
    from surogates.channels.registry import registry

    if registry.get("discord") is None:
        registry.register(DiscordPlatform())


_register()
