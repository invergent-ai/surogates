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
* Teams keys a DM on the personal conversation id, which only the
  conversation reference the bot recorded when the person wrote (or installed
  it) knows, along with the serviceUrl to write to.

:func:`resolve_dm` is that knowledge in one place, shared with outreach.

The opener text is emitted as a synthetic user message first.  Without it the
transcript shows a reply to a question that was never asked, and
``delivery_outbox.event_id`` is NOT NULL regardless.

Where the channel lets the platform write now (a text channel, or WhatsApp
inside its service window), no opener is sent at all: the agent is woken in
the person's conversation with the step context and writes the step itself
(:func:`make_step_waker`).  The approved template goes first only where the
channel demands one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from surogates.channels.capabilities import capabilities_for
from surogates.channels.dm import DmTarget, OpenerUndeliverable, dm_key, dm_target
from surogates.channels.identity import get_or_create_channel_session
from surogates.channels.memory_boundary import boundary_token
from surogates.channels.whatsapp_flows import new_flow_token
from surogates.db.models import ProgramInvitationRow, Session as SessionRow
from surogates.followup.store import last_inbound, shared_memory_boundary
from surogates.session.events import CHECKIN_OPENER_MARKER
from surogates.session.models import REUSABLE_SESSION_STATUSES

logger = logging.getLogger(__name__)


def _opener_text(config: dict) -> str:
    name = config.get("template_name") or "step"
    return f"[Step message sent: template '{name}']"


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
            # The followed person's record, shared with their contacts.
            "memory_boundary": await shared_memory_boundary(
                session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id,
            ) or boundary_token(
                platform=platform, channel_id=channel_id, visibility="dm",
                source={"chat_type": "private"}, fallback_id=session_key,
            ),
            "multi_party": False,
        },
        session_factory=session_factory, storage=storage, settings=settings,
        # Writing first is no work for the agent: the reply resumes it.
        resume=False,
    )


async def resolve_dm(
    platform: str,
    identity: Any,
    sender: str,
    *,
    credentials_for: Any = None,
    slack_client_factory: Any = None,
    org_id: Any = None,
    redis: Any = None,
) -> DmTarget:
    """The DM a first message to *identity* goes into, on *platform*.

    *identity* is the person's channel identity or its ``platform_user_id``.
    *credentials_for* (``async (kind, identifier, org_id) -> dict``) and
    *org_id* are needed only on Slack, where the DM must be opened first;
    *redis* only on Teams, where the conversation reference lives.
    Raises :class:`OpenerUndeliverable` with an operator-readable reason.
    """
    user = str(getattr(identity, "platform_user_id", identity) or "")
    if not user:
        raise OpenerUndeliverable(f"No {platform} identity for this person")
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
            dm_key("slack", dm_id, user), dm_id,
            {"channel_id": dm_id, "thread_ts": None, "channel_identifier": sender},
        )
    if platform == "teams":
        from surogates.channels.platforms.teams import conversation_ref

        ref = await conversation_ref(redis, sender, user=user) if redis is not None else None
        if not ref:
            raise OpenerUndeliverable(
                "This person has no Teams chat with the agent yet: they must "
                "install it or write to it first",
            )
        conversation = str(ref.get("conversation_id") or "")
        return DmTarget(
            dm_key("teams", conversation, user), conversation,
            {
                "conversation_id": conversation, "service_url": ref.get("service_url", ""),
                "reply_to_id": None, "channel_identifier": sender,
            },
        )
    if (target := dm_target(platform, user, sender)) is not None:
        return target
    raise OpenerUndeliverable(f"The platform cannot write first on {platform}")


@dataclass(frozen=True)
class Woken:
    """What the send pass gets back when the agent writes the step itself."""

    session_id: UUID


def make_step_waker(
    *,
    session_store: Any,
    redis: Any,
    session_factory: Any,
    wake_session: Any,
    config_for_program: Any,
    storage: Any = None,
    settings: Any = None,
    credentials_for: Any = None,
    slack_client_factory: Any = None,
):
    """Build ``async (invitation, *, event, config=None) -> session id``.

    Wakes the agent in the person's one-to-one conversation for a step:
    writes the step context block (see :mod:`surogates.programs.step_context`)
    as a synthetic message and queues the session.  *wake_session* is
    ``async (org_id, agent_id, session_id) -> None`` (the shared work queue).

    For a due step the invitation learns its conversation and ``woken_at``
    **before** the session is queued: the agent's message is matched back to
    the step through them, and the dispatcher can post it as soon as the
    turn runs.  The caller has already claimed the invitation; this never
    claims anything itself.
    """
    from surogates.programs.step_context import DUE, STEP_MARKER, step_context

    async def _status(session_id: UUID) -> str | None:
        async with session_factory() as db:
            return (
                await db.execute(sa.select(SessionRow.status).where(SessionRow.id == session_id))
            ).scalar_one_or_none()

    async def _wake(invitation: Any, *, event: str, config: dict | None = None) -> UUID:
        config = config if config is not None else await config_for_program(invitation.program_id) or {}
        platform = invitation.platform
        sender = str(
            invitation.channel_identifier
            or config.get("channel_identifier") or ""
        )
        now = datetime.now(timezone.utc)
        session_id = invitation.session_id if event != DUE else None
        status = await _status(session_id) if session_id is not None else None
        if status not in REUSABLE_SESSION_STATUSES:
            session_id = None
        if session_id is None:
            target = await resolve_dm(
                platform, invitation.platform_user_id, sender,
                credentials_for=credentials_for,
                slack_client_factory=slack_client_factory, org_id=invitation.org_id,
                redis=redis,
            )
            session_id = await dm_session(
                session_store, redis,
                session_factory=session_factory, storage=storage, settings=settings,
                org_id=invitation.org_id, agent_id=invitation.agent_id,
                user_id=invitation.user_id, platform=platform,
                session_key=target.session_key, channel_id=target.channel_id, sender=sender,
            )
            status = await _status(session_id)
        # An idle conversation must be revived like an inbound message
        # revives it, or the harness drops the wake ("status is 'completed',
        # skipping wake") and the step waits for nothing until its deadline.
        if status in ("completed", "paused"):
            await session_store.resume_session(session_id, source="program_step")
        can_write = True
        if capabilities_for(platform).window_hours:
            from surogates.followup.outreach import window_open

            can_write = window_open(await last_inbound(
                session_factory, org_id=invitation.org_id, platform=platform,
                platform_user_id=invitation.platform_user_id, channel_identifier=sender,
            ), now)
        content = await step_context(
            session_factory, invitation, event=event, now=now, config=config,
            session_id=session_id, can_write=can_write,
        )
        await session_store.emit_synthetic_user_message(
            session_id, content=content, synthetic=STEP_MARKER,
            metadata={
                "program_id": str(invitation.program_id),
                "occurrence_id": str(invitation.occurrence_id),
                "invitation_id": str(invitation.id),
                "skill_ref": str(config.get("skill_ref") or ""),
                "step_event": event,
            },
        )
        async with session_factory() as db:
            if event == DUE:
                await db.execute(
                    sa.update(ProgramInvitationRow)
                    .where(ProgramInvitationRow.id == invitation.id)
                    .where(ProgramInvitationRow.outbox_id.is_(None))
                    .values(session_id=session_id, woken_at=now)
                )
            else:
                # Where the agent decides, so what it then sends (a nudge)
                # can be told apart from silence.
                await db.execute(
                    sa.update(ProgramInvitationRow)
                    .where(ProgramInvitationRow.id == invitation.id)
                    .where(ProgramInvitationRow.session_id.is_(None))
                    .values(session_id=session_id)
                )
            await db.commit()
        await wake_session(invitation.org_id, invitation.agent_id, session_id)
        return session_id

    return _wake


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
    waker: Any = None,
):
    """Build the ``enqueue`` callable ``send_queued_openers`` needs in production.

    *config_for_program* is ``async (program_id) -> dict | None`` returning the
    Program's projected config.  *credentials_for* is
    ``async (kind, identifier, org_id) -> dict`` (the vault); Slack needs it
    for the bot token, WhatsApp does not (the dispatcher holds its creds).
    *slack_client_factory* is ``(bot_token) -> client`` with an async
    ``conversations_open(users=...)``; the default is slack_sdk's.

    *waker* (see :func:`make_step_waker`) lets the agent write the step:
    then, wherever the channel allows a message now, the agent is woken and
    :class:`Woken` comes back instead of an outbox id.  Without one the
    opener text is sent word for word.

    Returns ``async (invitation) -> int | Woken`` — the outbox row id — or
    raises :class:`OpenerUndeliverable`.
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
            redis=redis,
        )
        session_id = await _session(
            invitation, platform, target.session_key,
            channel_id=target.channel_id, sender=sender,
        )
        # The transcript event is a user-role message; the marker keeps the
        # model from reading its own invitation as something the person said.
        event_id = await session_store.emit_synthetic_user_message(
            session_id, content=f"[Step message sent: {text}]",
            synthetic=CHECKIN_OPENER_MARKER,
            metadata=_metadata(invitation, template_name=None),
        )
        return await delivery_service.enqueue(
            session_id, event_id, platform, target.destination, {"content": text},
        )

    async def _agent_writes(invitation: Any, config: dict, sender: str) -> bool:
        """Whether the agent may write this step now, rather than a template."""
        caps = capabilities_for(invitation.platform)
        if waker is None or not caps.can_initiate:
            return False
        last = await last_inbound(
            session_factory, org_id=invitation.org_id, platform=invitation.platform,
            platform_user_id=invitation.platform_user_id, channel_identifier=sender,
        )
        if caps.needs_prior_contact and last is None:
            raise OpenerUndeliverable(
                f"The person has not written to this {invitation.platform} sender yet, "
                "so it cannot write to them first"
            )
        if caps.opener == "text":
            return True
        from surogates.followup.outreach import window_open

        return bool(caps.window_hours) and window_open(last, datetime.now(timezone.utc))

    async def _enqueue(invitation: Any) -> int | Woken:
        config = await config_for_program(invitation.program_id) or {}
        platform = invitation.platform
        sender = str(
            invitation.channel_identifier
            or config.get("channel_identifier") or ""
        )
        if await _agent_writes(invitation, config, sender):
            return Woken(await waker(invitation, event="due", config=config))
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
