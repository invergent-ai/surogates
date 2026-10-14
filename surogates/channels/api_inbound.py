"""A person's reply from an integrator's app, as ops' public API hands it over.

``POST /api/v1/agents/{agent_id}/people/{person}/messages`` (ops) calls
:func:`receive`.  The reply lands in the person's own ``api`` conversation,
where the agent's messages to them were written, answers the step they were
sent if one waits, and wakes the agent like any inbound message.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import sqlalchemy as sa

logger = logging.getLogger(__name__)


class Refused(ValueError):
    """The message was not taken (no such person, or it reads as an injection)."""


async def receive(session_factory: Any, redis: Any, *, org_id: Any, agent_id: str, external_id: str,
                  text: str, form: dict | None = None) -> dict:
    """Write the person's reply into their conversation and wake the agent.

    Returns ``{"session_id": …}``.  Raises :class:`Refused` for a person with no
    ``api`` identity on the agent's org, or a message the prompt-injection
    screen blocks.  Without *redis* the reply is recorded but the agent is not
    woken until the person's next message.
    """
    from surogates.channels.inbound import build_principal_stamp
    from surogates.channels.outbound import begin_step_reply, dm_target
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
    if form:
        content = f"{content}\n[Form answers: {json.dumps(form, ensure_ascii=False)}]".strip()
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
    await begin_step_reply(session_factory, store, org_id=org_id, agent_id=agent_id, platform="api",
                           platform_user_id=external_id, session_id=session_id)
    await store.emit_event(session_id, EventType.USER_MESSAGE, {
        "content": content,
        "source": {"platform": "api", "chat_id": external_id, "chat_type": "dm", "user_id": external_id,
                   "user_name": external_id, "thread_id": None},
        **build_principal_stamp(user_id=user_id),
    })
    if redis is not None:
        await enqueue_session(redis, org_id=str(org_id), agent_id=agent_id, session_id=session_id)
    else:
        logger.warning("[api] reply for %s recorded without waking the agent: no Redis", session_id)
    return {"session_id": str(session_id)}
