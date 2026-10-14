"""Outbound calls: the agent phones a person from its number.

What lands in a person's own voice conversation (``agent:voice:dm:<digits>``),
a step the agent wrote or an alert for a team member, gets an outbox row
(``channels.constants.outbound_dm``).  The ``surogates voice`` process drains
them (:func:`dial_forever`): it dispatches the voice agent to a fresh room with
the call's details in the job metadata, then dials the person through the
number's outbound SIP trunk and waits for them to answer.

* answered: the rows are delivered; the worker speaks their text as it
  connects and the conversation goes on in the same session, so a reply is a
  step's reply like on any channel (:func:`surogates.channels.outbound.begin_step_reply`);
* busy, no answer, declined: the rows fail, final, and a step goes on to the
  person's next channel (the invitation's fallback);
* an alert call asks for an acknowledgement ("da", "yes", or 1 on the keypad),
  recorded as the alert read.

A conversation with a call in it (or just ended) is never called again for
the agent's words in it: those are spoken in the call (:func:`is_live`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from surogates.channels.base import SendResult
from surogates.channels.constants import outbound_dm
from surogates.channels.outbound import message_of
from surogates.voice.text import fold, words

log = logging.getLogger("surogates.voice")

LIVE = "surogates:voice:live:"
#: After a call ends, the agent's last words of it may still be on their way: no second call for them.
LIVE_GRACE_SECONDS = 300
YES = frozenset({"da", "yes", "yeah", "yep", "ok", "okay", "confirm", "confirmed", "confirmat",
                 "inteles", "sigur", "ja", "oui", "si"})


async def mark_live(redis: Any, session_id: Any, seconds: float) -> None:
    await redis.set(f"{LIVE}{session_id}", "1", ex=max(int(seconds), 1))


async def is_live(redis: Any, session_id: Any) -> bool:
    return bool(await redis.exists(f"{LIVE}{session_id}"))


def acknowledges(text: str) -> bool:
    """The callee confirmed: a yes among their first words."""
    return any(w in YES for w in words(fold(text))[:4])


@dataclass(frozen=True)
class Outbound:
    """An outbound call, as the voice worker reads it from its job's metadata."""

    session_id: str
    call_id: str
    number: str  # ours, E.164
    to: str  # theirs, E.164
    text: str  # said as the call connects
    kind: str  # step | alert | message
    outbox_ids: tuple[int, ...] = ()

    def provider_ids(self) -> list[str]:
        from surogates.channels.outbound import provider_ids

        return provider_ids(self.call_id, self.outbox_ids)

    def metadata(self) -> str:
        return json.dumps({"outbound": {**self.__dict__, "outbox_ids": list(self.outbox_ids)}})

    @classmethod
    def from_metadata(cls, raw: str | None) -> Outbound | None:
        try:
            data = (json.loads(raw) if raw else {}).get("outbound")
            return cls(**{**data, "outbox_ids": tuple(data.get("outbox_ids") or ())}) if data else None
        except (ValueError, TypeError, AttributeError):
            return None


#: SIP answers, as the step's failed delivery reads them.
_SIP = {486: "busy", 600: "busy", 480: "no answer", 408: "no answer", 487: "no answer",
        603: "declined", 404: "number not reachable", 484: "number not reachable", 410: "number not reachable"}


def call_failure(error: Exception) -> SendResult:
    """A dial that did not connect.  The person not answering is final for this
    message (the step goes on to their next channel); LiveKit unreachable is retried."""
    from livekit.api import ServerError

    code = getattr(error, "sip_status_code", None)
    if code is not None:
        return SendResult(success=False, error=f"call failed: {_SIP.get(code, f'SIP {code}')}", retryable=False)
    if isinstance(error, ServerError) and error.status < 500:
        return SendResult(success=False, error=f"call failed: {error.message}", retryable=False)
    return SendResult(success=False, error=f"call not placed: {error}", retryable=True)


async def deliver_voice(group: list[Any], *, session_store: Any, redis: Any, routing: Any, livekit: Any,
                        settings: Any, session_factory: Any = None) -> SendResult:
    """Phone the person whose conversation *group* belongs to and wait for an answer.

    An urgent alert that cannot be phoned goes out at once on the member's next alert channel
    (with *session_factory*)."""
    from livekit import api

    from surogates.channels.resolve import resolve_tenant

    item = group[0]
    session = await session_store.get_session(item.session_id)
    if not outbound_dm(session.channel, session.config) or await is_live(redis, session.id):
        return SendResult(success=True)  # an inbound call's words, or spoken in this call already
    if session.status == "active":  # the agent is still writing: call once, with all it says
        return SendResult(success=False, error="the agent's turn is still running", retryable=True)
    messages = [m for m in [await message_of(session_store, it) for it in group] if m is not None]
    if not messages:
        return SendResult(success=True)

    async def failed(result: SendResult) -> SendResult:
        if session_factory is not None and any(m.kind == "alert" for m in messages):
            await alert_elsewhere(session_factory, [i.id for i in group], why=result.error or "failed")
        return result

    config, vs = session.config or {}, settings.voice
    number, digits = str(config.get("channel_identifier") or ""), str(config.get("voice_channel_id") or "")
    tenant = await resolve_tenant(routing, "voice", number) if number else None
    if tenant is None:
        return await failed(SendResult(success=False, retryable=False,
                                       error=f"call failed: {number or 'no number'} is not this agent's line"))
    trunk = str((tenant.get("config") or {}).get("outbound_trunk_id") or vs.outbound_trunk_id)
    if not trunk:
        return await failed(SendResult(success=False, error=f"call failed: {number} cannot place calls",
                                       retryable=False))
    kinds = {m.kind for m in messages}
    call = Outbound(session_id=str(session.id), call_id=f"out-{item.id}", number=number, to=f"+{digits}",
                    text="\n\n".join(m.text for m in messages),
                    kind="alert" if "alert" in kinds else "step" if "step" in kinds else "message",
                    outbox_ids=tuple(i.id for i in group))
    room = f"call-{call.call_id}"
    await mark_live(redis, session.id, vs.ring_seconds + 60)  # no second dial while this one rings
    try:
        await livekit.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name=vs.agent_name, room=room, metadata=call.metadata()))
        await livekit.sip.create_sip_participant(api.CreateSIPParticipantRequest(
            sip_trunk_id=trunk, sip_call_to=call.to, sip_number=number, room_name=room,
            participant_identity=f"callee-{digits}", participant_name=call.to,
            wait_until_answered=True, ringing_timeout={"seconds": int(vs.ring_seconds)}))
    except Exception as exc:  # noqa: BLE001 — every failure is a verdict on this message
        await redis.delete(f"{LIVE}{session.id}")
        try:
            await livekit.room.delete_room(api.DeleteRoomRequest(room=room))
        except Exception:  # noqa: BLE001 — the room empties and closes on its own
            log.debug("could not delete room %s", room, exc_info=True)
        log.info("call %s to %s not connected: %s", call.call_id, call.to, exc)
        result = call_failure(exc)
        return await failed(result) if not result.retryable else result
    log.info("call %s to %s answered (session %s)", call.call_id, call.to, session.id)
    return SendResult(success=True, message_id=call.call_id)


async def dial_forever(settings: Any) -> None:
    """The ``surogates voice`` process's outbound loop.  Runs until cancelled."""
    import aiohttp
    from livekit import api

    from surogates.channels.delivery import DeliveryService
    from surogates.channels.outbound import run_forever
    from surogates.voice.worker import Runtime

    vs = settings.voice
    rt = await Runtime.open(settings)
    livekit = api.LiveKitAPI(url=vs.livekit_url, api_key=vs.livekit_api_key, api_secret=vs.livekit_api_secret,
                             timeout=aiohttp.ClientTimeout(total=vs.ring_seconds + 30))
    store = rt.sessions._store  # noqa: SLF001 — the worker's own session store
    lines = asyncio.Semaphore(vs.dial_concurrency)

    async def handler(group):
        async with lines:
            return await deliver_voice(group, session_store=store, redis=rt.redis, routing=rt.routing,
                                       livekit=livekit, settings=settings,
                                       session_factory=rt.sessions._sf)  # noqa: SLF001

    try:
        await run_forever(DeliveryService(rt.sessions._sf, rt.redis), rt.sessions._sf,  # noqa: SLF001
                          "voice", handler, by_session=True)
    finally:
        await livekit.aclose()
        await rt.aclose()


async def alert_elsewhere(session_factory: Any, outbox_ids: Any, *, why: str) -> int:
    """An urgent alert call nobody acknowledged (unanswered, busy, or answered without a yes): send the
    same alert at once on the member's next alert channel. 2B's red-alert reminder stays the backstop.

    Returns how many alerts went to another channel.
    """
    import uuid

    import sqlalchemy as sa

    from surogates.channels.capabilities import capabilities_for
    from surogates.db.models import DeliveryOutbox, Event, OutreachMessageRow
    from surogates.followup import store as followup_store

    async with session_factory() as db:
        ids = (await db.execute(
            sa.select(Event.data["outreach_id"].astext)
            .join(DeliveryOutbox, DeliveryOutbox.event_id == Event.id)
            .where(DeliveryOutbox.id.in_(list(outbox_ids)))
            .where(Event.data["synthetic"].astext == "outreach")
        )).scalars().all()
        rows = [r for r in [await db.get(OutreachMessageRow, uuid.UUID(i)) for i in ids if i]
                if r is not None and r.kind == "alert" and r.severity == "red" and r.read_at is None]
    moved = 0
    for row in rows:
        settings = await followup_store.get_settings(session_factory, org_id=row.org_id, agent_id=row.agent_id)
        senders = {p: s for p, s in followup_store.senders_of(settings).items()
                   if p != "voice" and capabilities_for(p).can_initiate}
        reachable = (await followup_store.granted_platforms(
            session_factory, org_id=row.org_id, user_ids=[row.user_id])).get(row.user_id, set())
        platform = followup_store.team_channel_for(settings, row.user_id, senders, reachable)
        if platform is None:
            log.info("alert %s: unanswered call, and no other channel reaches the member", row.id)
            continue
        template = capabilities_for(platform).opener == "template" and settings is not None
        await followup_store.queue_outreach(
            session_factory, org_id=row.org_id, agent_id=row.agent_id, user_id=row.user_id,
            platform=platform, channel_identifier=senders[platform], kind="alert", text=row.text,
            template_name=settings.alert_template_name if template else None,
            template_language=settings.alert_template_language if template else None,
            subject_user_id=row.subject_user_id, created_by=row.created_by,
            dedupe_key=f"call-unanswered:{row.id}", severity=row.severity, alert_reason=row.alert_reason,
        )
        log.info("alert %s: call %s, sent on %s", row.id, why, platform)
        moved += 1
    return moved
