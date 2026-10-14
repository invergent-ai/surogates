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
* Telegram keys a DM on the private chat id, which is the user's own id.

:func:`resolve_dm` is that knowledge in one place, shared with outreach.

The opener text is emitted as a synthetic user message first.  Without it the
transcript shows a reply to a question that was never asked, and
``delivery_outbox.event_id`` is NOT NULL regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from surogates.channels.capabilities import capabilities_for
from surogates.channels.identity import get_or_create_channel_session
from surogates.channels.memory_boundary import boundary_token
from surogates.channels.source import SessionSource, build_session_key
from surogates.channels.whatsapp_flows import new_flow_token
from surogates.followup.store import last_inbound
from surogates.session.events import CHECKIN_OPENER_MARKER

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
    name = config.get("template_name") or "check-in"
    return f"[Check-in opener sent: template '{name}']"


def _default_slack_client(bot_token: str):
    from surogates.channels.platforms.slack import web_client

    return web_client(bot_token)


async def dm_session(
    session_store: Any,
    redis: Any,
    *,
    session_factory: Any,
    storage: Any,
    settings: Any,
    org_id: UUID,
    agent_id: str,
    user_id: UUID,
    platform: str,
    session_key: str,
    channel_id: str,
    sender: str,
) -> UUID:
    """The one-to-one conversation a person's reply will land in.

    Shared by check-in openers and outreach: both must write into the session
    the inbound pipeline resolves for that person's next message, or the
    reply arrives in a conversation that never asked anything.
    """
    return await get_or_create_channel_session(
        session_store, redis,
        session_key=session_key,
        user_id=user_id,
        org_id=org_id,
        agent_id=agent_id,
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
        # Writing first is no work for the agent: the reply resumes it.
        resume=False,
    )


def whatsapp_session_key(wa_id: str) -> str:
    """The session key the inbound pipeline computes for a WhatsApp DM."""
    return build_session_key(SessionSource(
        platform="whatsapp", chat_id=wa_id, chat_type="dm", user_id=wa_id,
        user_name="", thread_id=None, chat_name=wa_id,
    ))


@dataclass(frozen=True)
class DmTarget:
    """Where a person's one-to-one conversation is, as the inbound path keys it."""

    session_key: str
    channel_id: str
    destination: dict


def _dm_key(platform: str, chat_id: str, user_id: str) -> str:
    return build_session_key(SessionSource(
        platform=platform, chat_id=chat_id, chat_type="dm", user_id=user_id,
        user_name="", thread_id=None, chat_name=chat_id,
    ))


async def resolve_dm(
    platform: str,
    identity: Any,
    sender: str,
    *,
    credentials_for: Any = None,
    slack_client_factory: Any = None,
    org_id: Any = None,
) -> DmTarget:
    """The DM a first message to *identity* goes into, on *platform*.

    *identity* is the person's channel identity or its ``platform_user_id``.
    *credentials_for* (``async (kind, identifier, org_id) -> dict``) and
    *org_id* are needed only on Slack, where the DM must be opened first.
    Raises :class:`OpenerUndeliverable` with an operator-readable reason.
    """
    user = str(getattr(identity, "platform_user_id", identity) or "")
    if not user:
        raise OpenerUndeliverable(f"No {platform} identity for this person")
    if platform == "whatsapp":
        return DmTarget(
            whatsapp_session_key(user), user,
            {"wa_id": user, "phone_number_id": sender, "channel_identifier": sender},
        )
    if platform == "telegram":
        # A private chat's id is the user's id.
        return DmTarget(
            _dm_key("telegram", user, user), user,
            {"chat_id": user, "channel_identifier": sender},
        )
    if platform == "slack":
        if credentials_for is None:
            raise OpenerUndeliverable("No credential vault in this process")
        creds = await credentials_for("slack", sender, str(org_id)) or {}
        bot_token = str(creds.get("bot_token") or "")
        if not bot_token:
            raise OpenerUndeliverable("Slack bot token is missing for this sender")
        try:
            opened = await (slack_client_factory or _default_slack_client)(
                bot_token,
            ).conversations_open(users=user)
            dm_id = str((opened.get("channel") or {}).get("id") or "")
        except Exception as exc:  # noqa: BLE001 — Slack's error text is the reason
            raise OpenerUndeliverable(f"Slack: {exc}") from exc
        if not dm_id:
            raise OpenerUndeliverable("Slack opened no direct message for this member")
        return DmTarget(
            _dm_key("slack", dm_id, user), dm_id,
            {"channel_id": dm_id, "thread_ts": None, "channel_identifier": sender},
        )
    from surogates.channels.outbound import dm_target

    if (target := dm_target(platform, user, sender)) is not None:
        return target
    raise OpenerUndeliverable(f"The platform cannot write first on {platform}")


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
    async def _session(invitation, platform, session_key, *, channel_id, sender) -> UUID:
        return await dm_session(
            session_store, redis,
            session_factory=session_factory, storage=storage, settings=settings,
            org_id=invitation.org_id, agent_id=invitation.agent_id,
            user_id=invitation.user_id, platform=platform,
            session_key=session_key, channel_id=channel_id, sender=sender,
        )

    def _metadata(invitation, *, template_name):
        return {
            "program_id": str(invitation.program_id),
            "occurrence_id": str(invitation.occurrence_id),
            "template_name": template_name,
        }

    async def _enqueue_text(invitation: Any, config: dict, sender: str) -> int:
        platform = invitation.platform
        text = str(config.get("opener_text") or "").strip()
        if not text:
            raise OpenerUndeliverable("Program has no opener message")
        if capabilities_for(platform).needs_prior_contact and await last_inbound(
            session_factory, org_id=invitation.org_id, platform=platform,
            platform_user_id=invitation.platform_user_id, channel_identifier=sender,
        ) is None:
            raise OpenerUndeliverable(
                f"The person has not written to this {platform} sender yet, "
                "so it cannot write to them first"
            )
        target = await resolve_dm(
            platform, invitation.platform_user_id, sender,
            credentials_for=credentials_for,
            slack_client_factory=slack_client_factory, org_id=invitation.org_id,
        )
        session_id = await _session(
            invitation, platform, target.session_key,
            channel_id=target.channel_id, sender=sender,
        )
        # The transcript event is a user-role message; the marker keeps the
        # model from reading its own invitation as something the person said.
        event_id = await session_store.emit_synthetic_user_message(
            session_id, content=f"[Check-in opener sent: {text}]",
            synthetic=CHECKIN_OPENER_MARKER,
            metadata=_metadata(invitation, template_name=None),
        )
        return await delivery_service.enqueue(
            session_id, event_id, platform, target.destination, {"content": text},
        )

    async def _enqueue(invitation: Any) -> int:
        config = await config_for_program(invitation.program_id) or {}
        platform = invitation.platform
        sender = str(
            getattr(invitation, "channel_identifier", None)
            or config.get("channel_identifier") or ""
        )
        opener = capabilities_for(platform).opener
        if opener == "text":
            return await _enqueue_text(invitation, config, sender)
        if opener != "template":
            raise OpenerUndeliverable(f"The platform cannot write first on {platform}")

        template_name = config.get("template_name")
        template_language = config.get("template_language")
        if not template_name or not template_language:
            # No approved template means no lawful way to open the
            # conversation.  The activation gate refuses this; reaching it
            # means the projection drifted, and the user must not sit queued.
            raise OpenerUndeliverable("Program has no approved template to open with")

        target = await resolve_dm(platform, invitation.platform_user_id, sender)
        session_id = await _session(
            invitation, platform, target.session_key,
            channel_id=target.channel_id, sender=sender,
        )
        template = {"name": template_name, "language": template_language}
        metadata = _metadata(invitation, template_name=template_name)
        # A FLOW button on the template opens the form in one tap; its token
        # rides on the opener event so the reply is recognised like any
        # form this conversation sent.
        button = config.get("template_flow_button")
        if isinstance(button, dict):
            ref = button.get("form") or button.get("flow_id") or "form"
            metadata["flow_token"] = new_flow_token(str(ref))
            template["flow_button"] = {
                "index": str(button.get("index") or 0), "flow_token": metadata["flow_token"],
            }
            # One generic form serves several Programs by reading which one
            # opened it from ``${data.*}``.
            form_data = config.get("form_data")
            if isinstance(form_data, dict) and form_data:
                template["flow_button"]["flow_action_data"] = form_data
        event_id = await session_store.emit_synthetic_user_message(
            session_id, content=_opener_text(config), synthetic=CHECKIN_OPENER_MARKER,
            metadata=metadata,
        )
        # Same three keys the reply path writes; the dispatcher reads
        # ``channel_identifier`` first and fails the row without it.
        return await delivery_service.enqueue(
            session_id, event_id, platform, target.destination, {"template": template},
        )

    return _enqueue
