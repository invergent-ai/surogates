"""Hand a queued check-in opener to the outbox.

Three things have to exist before an outbox row can: a session for the user
on this channel, an event on it, and the row itself.  The session is resolved
with the **same key the inbound pipeline will compute for the reply**, so the
user's answer lands in the conversation that holds the opener and the agent
sees the question it is being answered to.

* WhatsApp keys a DM on the user's ``wa_id``: ``agent:whatsapp:dm:<wa_id>``.
* Slack keys a DM on the direct-message **channel** id, which only Slack
  knows: ``agent:slack:dm:<D…>``.  So the Slack opener first calls
  ``conversations.open`` for the member and keys the session on what comes
  back.  Skipping that call would put the opener and the reply in two
  different sessions.

The opener text is emitted as a synthetic user message first.  Without it the
transcript shows a reply to a question that was never asked, and
``delivery_outbox.event_id`` is NOT NULL regardless.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from surogates.channels.identity import get_or_create_channel_session
from surogates.channels.memory_boundary import boundary_token
from surogates.channels.source import SessionSource, build_session_key

logger = logging.getLogger(__name__)


class OpenerUndeliverable(Exception):
    """This opener cannot be handed over, and retrying will not change that.

    The send pass records it on the invitation as a failed delivery with
    ``reason`` as the error, so the operator sees one user missed rather than
    a row that stays queued and is retried every tick.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _opener_text(config: dict) -> str:
    if config.get("channel") == "slack":
        return str(config.get("opener_text") or "")
    name = config.get("template_name") or "check-in"
    return f"[Check-in opener sent: template '{name}']"


def _default_slack_client(bot_token: str):
    from slack_sdk.web.async_client import AsyncWebClient

    return AsyncWebClient(token=bot_token)


def make_opener_enqueue(
    *,
    session_store: Any,
    redis: Any,
    session_factory: Any,
    delivery_service: Any,
    storage: Any = None,
    settings: Any = None,
    config_for_program: Any,
    credentials_for: Any = None,
    slack_client_factory: Any = None,
):
    """Build the ``enqueue`` callable ``send_queued_openers`` needs in production.

    *config_for_program* is ``async (program_id) -> dict | None`` returning the
    Program's projected config.  *credentials_for* is
    ``async (kind, identifier, org_id) -> dict`` (the vault); Slack needs it
    for the bot token, WhatsApp does not (the dispatcher holds its creds).
    *slack_client_factory* is ``(bot_token) -> client`` with an async
    ``conversations_open(users=...)``; the default is slack_sdk's.

    Returns ``async (invitation) -> int`` — the outbox row id — or raises
    :class:`OpenerUndeliverable`.
    """
    slack_client_factory = slack_client_factory or _default_slack_client

    async def _session(invitation, platform, session_key, *, channel_id, sender) -> UUID:
        return await get_or_create_channel_session(
            session_store, redis,
            session_key=session_key,
            user_id=invitation.user_id,
            org_id=invitation.org_id,
            agent_id=invitation.agent_id,
            channel=platform,
            config={
                f"{platform}_channel_id": channel_id,
                f"{platform}_thread_key": None,
                "channel_identifier": sender,
                "memory_boundary": boundary_token(
                    platform=platform, channel_id=channel_id, visibility="dm",
                    source={"chat_type": "private"}, fallback_id=session_key,
                ),
                "multi_party": False,
            },
            session_factory=session_factory, storage=storage, settings=settings,
        )

    def _metadata(invitation, *, template_name):
        return {
            "program_id": str(invitation.program_id),
            "occurrence_id": str(invitation.occurrence_id),
            "template_name": template_name,
        }

    async def _enqueue_slack(invitation: Any, config: dict, sender: str) -> int:
        text = str(config.get("opener_text") or "").strip()
        if not text:
            raise OpenerUndeliverable("Program has no opener message")
        if credentials_for is None:
            raise OpenerUndeliverable("No credential vault in this process")
        creds = await credentials_for("slack", sender, str(invitation.org_id)) or {}
        bot_token = str(creds.get("bot_token") or "")
        if not bot_token:
            raise OpenerUndeliverable("Slack bot token is missing for this sender")

        member_id = invitation.platform_user_id
        try:
            opened = await slack_client_factory(bot_token).conversations_open(
                users=member_id,
            )
            dm_id = str((opened.get("channel") or {}).get("id") or "")
        except Exception as exc:  # noqa: BLE001 — Slack's error text is the reason
            raise OpenerUndeliverable(f"Slack: {exc}") from exc
        if not dm_id:
            raise OpenerUndeliverable("Slack opened no direct message for this member")

        source = SessionSource(
            platform="slack", chat_id=dm_id, chat_type="dm", user_id=member_id,
            user_name="", thread_id=None, chat_name=dm_id,
        )
        session_key = build_session_key(source)
        session_id = await _session(
            invitation, "slack", session_key, channel_id=dm_id, sender=sender,
        )
        event_id = await session_store.emit_synthetic_user_message(
            session_id, content=text, synthetic="checkin_opener",
            metadata=_metadata(invitation, template_name=None),
        )
        return await delivery_service.enqueue(
            session_id, event_id, "slack",
            {"channel_id": dm_id, "thread_ts": None, "channel_identifier": sender},
            {"content": text},
        )

    async def _enqueue(invitation: Any) -> int:
        config = await config_for_program(invitation.program_id) or {}
        platform = invitation.platform
        sender = str(config.get("channel_identifier") or "")

        if platform == "slack":
            return await _enqueue_slack(invitation, config, sender)

        template_name = config.get("template_name")
        template_language = config.get("template_language")
        if not template_name or not template_language:
            # No approved template means no lawful way to open the
            # conversation.  The activation gate refuses this; reaching it
            # means the projection drifted, and the user must not sit queued.
            raise OpenerUndeliverable("Program has no approved template to open with")

        wa_id = invitation.platform_user_id
        source = SessionSource(
            platform=platform, chat_id=wa_id, chat_type="dm", user_id=wa_id,
            user_name="", thread_id=None, chat_name=wa_id,
        )
        session_key = build_session_key(source)
        session_id = await _session(
            invitation, platform, session_key, channel_id=wa_id, sender=sender,
        )
        event_id = await session_store.emit_synthetic_user_message(
            session_id, content=_opener_text(config), synthetic="checkin_opener",
            metadata=_metadata(invitation, template_name=template_name),
        )
        # Same three keys the reply path writes; the dispatcher reads
        # ``channel_identifier`` first and fails the row without it.
        return await delivery_service.enqueue(
            session_id, event_id, platform,
            {"wa_id": wa_id, "phone_number_id": sender, "channel_identifier": sender},
            {"template": {"name": template_name, "language": template_language}},
        )

    return _enqueue
