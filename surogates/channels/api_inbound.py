"""A person's reply from an integrator's app, as ops' public API hands it over.

``POST /api/v1/agents/{agent_id}/people/{person}/messages`` (ops) calls
:func:`receive`.  The reply lands in the person's own ``api`` conversation,
where the agent's messages to them were written, answers the step they were
sent if one waits, and wakes the agent like any inbound message.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa

logger = logging.getLogger(__name__)


class Refused(ValueError):
    """The message was not taken (no such person, or it reads as an injection)."""


async def receive(session_factory: Any, redis: Any, *, org_id: Any, agent_id: str, external_id: str,
                  text: str, form: dict | None = None, platform_client: Any = None,
                  runtime_payload: dict | None = None) -> dict:
    """Write the person's reply into their conversation and wake the agent.

    The same gates as any channel's message (:mod:`surogates.channels.inbound`):
    a bare STOP withdraws the api channel (never a team member's), the plan
    gate runs when *platform_client* and *runtime_payload* are given, and a
    pending question is answered or dismissed.  A form's answers are kept as
    ``source.flow_fields`` like a WhatsApp form's.

    Returns ``{"session_id": …}``, plus ``"refused"`` (the gate's code) and
    ``"notice"`` (the business's wording) when the plan gate refused the turn;
    the message is then kept, held, and the agent not woken.  Raises
    :class:`Refused` for a person with no ``api`` identity on the agent's org,
    or a message the prompt-injection screen blocks.  Without *redis* the
    reply is recorded but the agent is not woken until the person's next
    message.
    """
    from types import SimpleNamespace

    from surogates.channels.inbound import (
        ChannelInboundPipeline,
        InboundMessage,
        InboundOutcome,
        build_principal_stamp,
        gate_turn,
    )
    from surogates.channels.whatsapp_flows import form_reply_text
    from surogates.followup.people import honour_stop
    from surogates.session.interactive_input import pending_input_for_session
    from surogates.channels.dm import dm_target
    from surogates.channels.outbound import begin_step_reply
    from surogates.config import enqueue_session
    from surogates.db.models import ChannelIdentity
    from surogates.programs.opener import dm_session
    from surogates.session.attachment_ingest import get_injection_detector
    from surogates.session.events import EventType
    from surogates.session.store import SessionStore

    org_id = org_id if isinstance(org_id, uuid.UUID) else uuid.UUID(str(org_id))
    async with session_factory() as db:
        user_id = (await db.execute(
            sa.select(ChannelIdentity.user_id).where(ChannelIdentity.org_id == org_id)
            .where(ChannelIdentity.platform == "api").where(ChannelIdentity.platform_user_id == external_id)
        )).scalar_one_or_none()
    if user_id is None:
        raise Refused("No person with this api id")
    content = text.strip()
    source = {"platform": "api", "chat_id": external_id, "chat_type": "dm", "user_id": external_id,
              "user_name": external_id, "thread_id": None}
    if form:
        # Kept as a form's answers, as a WhatsApp form's are: the step views
        # and webhooks read them from here.
        content = "\n".join(p for p in (content, form_reply_text({}, form)) if p)
        source = {**source, "flow_reply": True, "flow_fields": form}
    verdict = get_injection_detector().detect(content, source="api_channel")
    if verdict.is_injection:
        raise Refused(f"Message blocked: {verdict.explanation}")

    store = SessionStore(session_factory, redis=redis)
    target = dm_target("api", external_id, agent_id)
    session_id = await dm_session(
        store, redis, session_factory=session_factory, storage=None, settings=None, org_id=org_id,
        agent_id=agent_id, user_id=user_id, platform="api", session_key=target.session_key,
        channel_id=target.channel_id, sender=agent_id,
    )
    if (await store.get_session(session_id)).status in ("completed", "paused", "failed"):
        await store.resume_session(session_id, source="api")
    await honour_stop(session_factory, org_id=org_id, agent_id=agent_id, user_id=user_id,
                      platform="api", platform_user_id=external_id, text=text)
    if platform_client is not None and runtime_payload is not None:
        refused = await gate_turn(
            platform_client=platform_client, runtime_payload=runtime_payload, session_store=store,
            session_factory=session_factory, session_id=session_id, org_id=org_id,
            agent_id=agent_id, user_id=user_id, content=content, channel="api", source=source,
        )
        if refused is not None:
            return {"session_id": str(session_id), "refused": refused[0], "notice": refused[1]}
    # A question the agent asked waits: this is its answer, or dismisses it,
    # exactly as on any other channel.
    message = InboundMessage(
        kind="text", identifier=external_id, thread_key=None, platform_user_id=external_id,
        user_name=external_id, text=content, media_urls=[], media_types=[], is_dm=True,
        is_mention=False, ts="", source=source, visibility="dm",
    )
    deps = SimpleNamespace(
        pending_input=lambda sid: pending_input_for_session(store, session_id=sid),
        session_store=store, keep_typing=None,
    )
    answered, dismissed = await ChannelInboundPipeline._intercept_pending_input(
        message, routing=SimpleNamespace(platform="api"), deps=deps, session_id=session_id,
        sender_user_id=user_id,
    )
    if answered is InboundOutcome.DROPPED:
        return {"session_id": str(session_id)}
    await begin_step_reply(session_factory, store, org_id=org_id, agent_id=agent_id, platform="api",
                           platform_user_id=external_id, session_id=session_id)
    event = {"content": content, "source": source, **build_principal_stamp(user_id=user_id)}
    if dismissed:
        from surogates.tools.builtin.ask_user_question import DISMISSES_QUESTION

        event[DISMISSES_QUESTION] = dismissed
    await store.emit_event(session_id, EventType.USER_MESSAGE, event)
    if redis is not None:
        await enqueue_session(redis, org_id=str(org_id), agent_id=agent_id, session_id=session_id)
    else:
        logger.warning("[api] reply for %s recorded without waking the agent: no Redis", session_id)
    return {"session_id": str(session_id)}
