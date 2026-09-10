"""Hand a queued check-in opener to the outbox.

Three things have to exist before an outbox row can: a session for the patient
on this channel, an event on it, and the row itself.  The session is resolved
with the **same key the inbound pipeline will compute for the reply** —
``agent:whatsapp:dm:<wa_id>`` — so the patient's answer lands in the
conversation that holds the opener, and the agent sees the question it is
being answered to.  For a DM that key does not depend on any routing flag,
which is what lets this run without the channel routing cache.

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


def _opener_text(config: dict) -> str:
    name = config.get("template_name") or "check-in"
    return f"[Check-in opener sent: template '{name}']"


def make_opener_enqueue(
    *,
    session_store: Any,
    redis: Any,
    session_factory: Any,
    delivery_service: Any,
    storage: Any = None,
    settings: Any = None,
    config_for_program: Any,
):
    """Build the ``enqueue`` callable ``send_queued_openers`` needs in production.

    *config_for_program* is ``async (program_id) -> dict | None`` returning the
    Program's projected config (the template name and language live there).
    Returns ``async (invitation) -> int | None`` — the outbox row id, which is
    the key the dispatcher later reports back through.
    """

    async def _enqueue(invitation: Any) -> int | None:
        config = await config_for_program(invitation.program_id) or {}
        template_name = config.get("template_name")
        template_language = config.get("template_language")
        if not template_name or not template_language:
            # No approved template means no lawful way to open the
            # conversation. Leave it queued; the activation gate should have
            # refused this Program, and the operator will see it stuck.
            logger.warning(
                "[programs] program %s has no template; opener for %s not sent",
                invitation.program_id, invitation.user_id,
            )
            return None

        platform = invitation.platform
        wa_id = invitation.platform_user_id
        sender = str(config.get("channel_identifier") or "")

        source = SessionSource(
            platform=platform,
            chat_id=wa_id,
            chat_type="dm",
            user_id=wa_id,
            user_name="",
            thread_id=None,
            chat_name=wa_id,
        )
        session_key = build_session_key(source)

        session_id: UUID = await get_or_create_channel_session(
            session_store,
            redis,
            session_key=session_key,
            user_id=invitation.user_id,
            org_id=invitation.org_id,
            agent_id=invitation.agent_id,
            channel=platform,
            config={
                f"{platform}_channel_id": wa_id,
                f"{platform}_thread_key": None,
                "channel_identifier": sender,
                "memory_boundary": boundary_token(
                    platform=platform,
                    channel_id=wa_id,
                    visibility="dm",
                    source={"chat_type": "private"},
                    fallback_id=session_key,
                ),
                "multi_party": False,
            },
            session_factory=session_factory,
            storage=storage,
            settings=settings,
        )

        event_id = await session_store.emit_synthetic_user_message(
            session_id,
            content=_opener_text(config),
            synthetic="checkin_opener",
            metadata={
                "program_id": str(invitation.program_id),
                "occurrence_id": str(invitation.occurrence_id),
                "template_name": template_name,
            },
        )

        # Same three keys the reply path writes (``session/store.py``).  The
        # dispatcher reads ``channel_identifier`` first and fails the row
        # without it, before any adapter sees ``phone_number_id``.
        return await delivery_service.enqueue(
            session_id,
            event_id,
            platform,
            {"wa_id": wa_id, "phone_number_id": sender, "channel_identifier": sender},
            {"template": {"name": template_name, "language": template_language}},
        )

    return _enqueue
