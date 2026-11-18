"""The ``post_to_channel`` builtin tool.

Lets an agent post on its own initiative (a scheduled report, an alert) into
a channel of a workspace it is connected to, not only reply where it was
spoken to.  Slack only for now.

The agent's Slack app is the session's own when the session is a Slack
conversation, else the one ops routes to this agent (``by-kind/slack``).
The bot token comes from the vault in the worker and never leaves it, as for
the channel adapters; the governance gate already ran in the tool router.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from types import SimpleNamespace
from typing import Any

from surogates.channels.credentials import resolve_channel_credentials
from surogates.channels.platform_resolve import effective_channel_platform
from surogates.runtime.entitlements import dimension_allowlist
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# Slack's own limits: chat.postMessage takes 50 blocks and ~40k chars of text;
# a section's text holds 3000.
MAX_BLOCKS = 50
MAX_TEXT = 40_000
_SECTION_TEXT = 3000

_CHANNEL_ID = re.compile(r"^[CGDUW][A-Z0-9]{6,}$")  # U/W: a person; Slack posts it in the bot's DM with them
_CHANNEL_MENTION = re.compile(r"^<#([CG][A-Z0-9]+)(?:\|[^>]*)?>$")

POST_TO_CHANNEL_SCHEMA = ToolSchema(
    name="post_to_channel",
    description=(
        "Post a new message into a channel of a workspace this agent is "
        "connected to (Slack), e.g. a scheduled report into #growth. Not for "
        "replying in the current conversation: your reply already goes there. "
        "'text' is required (the notification and fallback text). For rich "
        "layout pass Slack Block Kit 'blocks' (header, section with mrkdwn or "
        "fields, divider, context; put tables in a ``` code block inside a "
        "section); 'image_url' (https) is appended as an image block. Returns "
        "the message permalink."
    ),
    parameters={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["slack"], "description": "Channel kind (default 'slack')."},
            "channel": {
                "type": "string",
                "description": "Channel name like '#growth' or a channel id like 'C0123ABCD'; or one person, privately (a DM): '@Their Full Name' or their Slack id 'U0123ABCD'.",
            },
            "text": {"type": "string", "description": "Plain message text (fallback when blocks are given)."},
            "blocks": {
                "type": "array",
                "items": {"type": "object"},
                "description": f"Optional Slack Block Kit blocks (max {MAX_BLOCKS}).",
            },
            "image_url": {"type": "string", "description": "Optional public https image URL, shown below the message."},
            "thread_ts": {"type": "string", "description": "Optional: post as a reply in this thread (message ts)."},
        },
        "required": ["channel", "text"],
    },
    annotations={"readOnlyHint": False},
)


class _PostError(Exception):
    """A refusal the model reads as is."""


def _fail(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


def _build_blocks(text: str, blocks: Any, image_url: str | None) -> list[dict] | None:
    if isinstance(blocks, str) and blocks.strip():
        try:
            blocks = json.loads(blocks)
        except ValueError:
            raise _PostError("'blocks' is not valid JSON: pass a list of Block Kit blocks.")
    if blocks in (None, "", []):
        blocks = None
    elif not (isinstance(blocks, list) and all(isinstance(b, dict) and b.get("type") for b in blocks)):
        raise _PostError("'blocks' must be a list of Block Kit objects, each with a 'type'.")
    if image_url:
        if not image_url.startswith("https://"):
            raise _PostError("'image_url' must be a public https:// URL.")
        if blocks is None:
            # Blocks replace the text on screen, so the text goes in first.
            blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text[i:i + _SECTION_TEXT]}}
                for i in range(0, len(text), _SECTION_TEXT)
            ]
        blocks = [*blocks, {"type": "image", "image_url": image_url, "alt_text": "image"}]
    if blocks is not None and len(blocks) > MAX_BLOCKS:
        raise _PostError(f"Too many blocks ({len(blocks)}); Slack takes at most {MAX_BLOCKS}.")
    return blocks


async def _slack_app_id(session: Any, platform_client: Any) -> str | None:
    """The agent's Slack app: this conversation's, else the one ops routes to it."""
    cfg = getattr(session, "config", None) or {}
    if effective_channel_platform(session) == "slack" and cfg.get("channel_identifier"):
        return str(cfg["channel_identifier"])
    if platform_client is None:
        return None
    for routing in await platform_client.list_channel_routings("slack"):
        if (str(routing.get("agent_id")) == str(session.agent_id)
                and str(routing.get("org_id")) == str(session.org_id)
                and routing.get("channel_identifier")):
            return str(routing["channel_identifier"])
    return None


async def _resolve_channel(client: Any, channel: str) -> str:
    mention = _CHANNEL_MENTION.match(channel)
    if mention:
        return mention.group(1)
    if _CHANNEL_ID.match(channel):
        return channel
    if channel.startswith("@"):  # a person, by name: Slack posts it in the bot's DM with them
        return await _resolve_person(client, channel[1:].strip())
    name = channel.lstrip("#").lower()
    cursor = None
    while True:
        resp = await client.conversations_list(
            types="public_channel,private_channel", exclude_archived=True, limit=1000, cursor=cursor,
        )
        for ch in resp.get("channels") or []:
            if (ch.get("name") or "").lower() == name:
                return ch["id"]
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    raise _PostError(
        f"No channel #{name} that the bot can see. Check the name, or, for a "
        f"private channel, invite the bot to it first."
    )


def _fold(name: str) -> str:
    """"Mădălin Tătarciuc" and "madalin tatarciuc" are the same person."""
    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c)).casefold().strip()


async def _resolve_person(client: Any, name: str) -> str:
    want, cursor, hits = _fold(name), None, {}
    while True:
        resp = await client.users_list(limit=500, cursor=cursor)
        for u in resp.get("members") or []:
            if u.get("deleted") or u.get("is_bot"):
                continue
            p = u.get("profile") or {}
            if want in {_fold(n) for n in (u.get("name"), u.get("real_name"), p.get("real_name"), p.get("display_name")) if n}:
                hits[u["id"]] = p.get("real_name") or u.get("name")
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    if len(hits) == 1:
        return next(iter(hits))
    if hits:
        raise _PostError(f"More than one person in Slack is called '{name}': {', '.join(hits.values())}. Use their Slack id.")
    raise _PostError(f"No one in Slack is called '{name}'. Use their full name as Slack shows it, or their Slack id (U…).")


async def _explain(client: Any, code: str, channel: str) -> str:
    """Slack's error code, as what to do about it."""
    if code in ("not_in_channel", "channel_not_found"):
        try:
            bot = (await client.auth_test()).get("user") or "the bot"
        except Exception:
            bot = "the bot"
        if code == "not_in_channel":
            return f"The bot is not in {channel}. Invite it: /invite @{bot} in {channel}, then post again."
        return (f"Slack cannot find {channel} for the bot. If it is private, invite the bot: "
                f"/invite @{bot} in {channel}.")
    if code == "is_archived":
        return f"{channel} is archived; pick another channel."
    if code in ("invalid_blocks", "invalid_blocks_format"):
        return "Slack rejected the blocks as invalid Block Kit. Fix them (or send text only) and try again."
    if code == "msg_too_long":
        return "The message is too long for Slack; shorten it."
    if code in ("ratelimited", "rate_limited"):
        return "Slack is rate limiting the bot; try again in a minute."
    return f"Slack refused the post ({code or 'unknown error'})."


async def _post_slack(
    *, bot_token: str, channel: str, text: str, blocks: list[dict] | None, thread_ts: str | None,
) -> dict:
    from slack_sdk.errors import SlackApiError

    from surogates.channels.platforms.slack import _slack_error_code, web_client

    client = web_client(bot_token)
    try:
        channel_id = await _resolve_channel(client, channel)
        # No link previews: an update with five links became five cards longer than the message itself.
        kwargs: dict[str, Any] = {"channel": channel_id, "text": text, "unfurl_links": False, "unfurl_media": False}
        if blocks is not None:
            kwargs["blocks"] = blocks
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        posted = await client.chat_postMessage(**kwargs)
    except SlackApiError as exc:
        raise _PostError(await _explain(client, _slack_error_code(exc), channel))
    ts = posted.get("ts")
    try:
        permalink = (await client.chat_getPermalink(channel=channel_id, message_ts=ts)).get("permalink")
    except SlackApiError:
        permalink = None  # posted; only the link is missing
    return {"success": True, "channel": channel_id, "ts": ts, "permalink": permalink}


async def _post_to_channel_handler(arguments: dict[str, Any], **kwargs: Any) -> str:
    # The runtime passes the session's parts, not the session (tool_exec dispatch).
    tenant = kwargs.get("tenant")
    if tenant is None or not kwargs.get("agent_id"):
        return _fail("post_to_channel needs an agent's session.")
    session = SimpleNamespace(agent_id=kwargs["agent_id"], org_id=tenant.org_id,
                              channel=kwargs.get("channel"), config=kwargs.get("session_config") or {})
    kind = str(arguments.get("kind") or "slack").lower()
    if kind != "slack":
        return _fail(f"Posting to '{kind}' is not supported; only 'slack'.")
    allowed = dimension_allowlist(getattr(session, "config", None), "channels")
    if allowed is not None and kind not in allowed:
        return _fail("Slack is not included in this conversation's package.")

    channel = str(arguments.get("channel") or "").strip()
    text = str(arguments.get("text") or "")
    if not channel:
        return _fail("'channel' is required, e.g. '#growth'.")
    if not text.strip():
        return _fail("'text' is required (the plain fallback of the message).")
    if len(text) > MAX_TEXT:
        return _fail(f"'text' is {len(text)} characters; Slack takes at most {MAX_TEXT}.")
    try:
        blocks = _build_blocks(text, arguments.get("blocks"), arguments.get("image_url") or None)
    except _PostError as exc:
        return _fail(str(exc))

    try:
        app_id = await _slack_app_id(session, kwargs.get("platform_client"))
    except Exception:
        logger.warning("post_to_channel: could not list slack routings", exc_info=True)
        return _fail("Could not look up this agent's Slack connection right now; try again later.")
    if not app_id:
        return _fail("This agent is not connected to Slack. Connect a Slack app to it in Studio first.")
    vault = kwargs.get("credential_vault")
    creds = {}
    if vault is not None:
        creds = await resolve_channel_credentials(
            vault=vault, kind="slack", identifier=app_id,
            org_id=str(session.org_id), refs={"bot_token": "bot_token"},
        )
    if not creds.get("bot_token"):
        return _fail("This agent's Slack connection has no bot token; reconnect Slack in Studio.")

    try:
        result = await _post_slack(
            bot_token=creds["bot_token"], channel=channel, text=text, blocks=blocks,
            thread_ts=(str(arguments.get("thread_ts") or "") or None),
        )
    except _PostError as exc:
        return _fail(str(exc))
    except Exception:
        logger.warning("post_to_channel: Slack post to %s failed", channel, exc_info=True)
        return _fail("Could not reach Slack right now; try again later.")
    return json.dumps(result, ensure_ascii=False)


def register(registry: ToolRegistry) -> None:
    """Register the post_to_channel tool."""
    registry.register(
        name="post_to_channel",
        schema=POST_TO_CHANNEL_SCHEMA,
        handler=_post_to_channel_handler,
        toolset="channels",
    )
