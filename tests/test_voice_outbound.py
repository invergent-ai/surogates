"""Outbound calls: who is dialled from which number, what a call that does not connect means,
and the conversation the call joins."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("livekit.agents", reason="the voice extra is not installed")

from livekit.api import ServerError, SipCallError  # noqa: E402

from surogates.voice import outbound as vo  # noqa: E402
from surogates.voice.outbound import Outbound, acknowledges, call_failure, deliver_voice  # noqa: E402


class Redis:
    def __init__(self):
        self.keys = {}

    async def set(self, key, value, ex=None):
        self.keys[key] = ex

    async def exists(self, key):
        return key in self.keys

    async def delete(self, key):
        self.keys.pop(key, None)


class Store:
    def __init__(self, key="agent:voice:dm:40712000103"):
        self.session = SimpleNamespace(id=uuid4(), channel="voice", status="completed", config={
            "channel_session_key": key, "channel_identifier": "+40300000001", "voice_channel_id": "40712000103"})

    async def get_session(self, sid):
        return self.session

    async def get_event_by_id(self, sid, eid):
        return SimpleNamespace(type="llm.response", data={"outreach_kind": "alert"} if eid == 9 else {})


class LiveKit:
    def __init__(self, fail=None):
        self.fail, self.dispatched, self.dialled, self.deleted = fail, [], [], []
        self.agent_dispatch = SimpleNamespace(create_dispatch=self._dispatch)
        self.sip = SimpleNamespace(create_sip_participant=self._dial)
        self.room = SimpleNamespace(delete_room=self._delete)

    async def _dispatch(self, req):
        self.dispatched.append(req)

    async def _dial(self, req):
        self.dialled.append(req)
        if self.fail:
            raise self.fail

    async def _delete(self, req):
        self.deleted.append(req.room)


def settings(trunk=""):
    return SimpleNamespace(voice=SimpleNamespace(outbound_trunk_id=trunk, ring_seconds=30, agent_name="surogate-voice"))


class Routing:
    def __init__(self, config=None):
        self.config = config

    async def get(self, key):
        if self.config is None or key != "voice:+40300000001":
            return None
        return {"org_id": str(uuid4()), "agent_id": "agent-1", "config": self.config}


def rows(*event_ids, text="Bună ziua, vă sun de la clinică."):
    sid = uuid4()
    return [SimpleNamespace(id=i, session_id=sid, event_id=e, payload={"content": f"{text} {i}"})
            for i, e in enumerate(event_ids, start=40)]


async def _call(store=None, livekit=None, routing=None, redis=None, group=None, trunk=""):
    return await deliver_voice(group or rows(5), session_store=store or Store(), redis=redis or Redis(),
                               routing=routing or Routing({"outbound_trunk_id": "ST_out"}),
                               livekit=livekit or LiveKit(), settings=settings(trunk))


async def test_the_person_is_dialled_from_the_agents_number_and_the_agent_told_what_to_say():
    lk, redis = LiveKit(), Redis()
    result = await _call(livekit=lk, redis=redis, group=rows(5, 9))
    assert result.success and result.message_id == "out-40"
    [dial], [dispatch] = lk.dialled, lk.dispatched
    assert (dial.sip_trunk_id, dial.sip_call_to, dial.sip_number) == ("ST_out", "+40712000103", "+40300000001")
    assert dial.wait_until_answered and dial.ringing_timeout.seconds == 30
    assert dial.room_name == dispatch.room == "call-out-40" and dispatch.agent_name == "surogate-voice"
    call = Outbound.from_metadata(dispatch.metadata)
    assert call.kind == "alert" and call.outbox_ids == (40, 41) and "40" in call.text and "41" in call.text
    assert call.provider_ids() == ["out-40#40", "out-40#41"]
    assert redis.keys  # no second dial while this one rings


@pytest.mark.parametrize("code, said", [(486, "busy"), (480, "no answer"), (603, "declined"), (404, "number not reachable")])
async def test_a_call_that_does_not_connect_is_final_and_the_room_is_cleared(code, said):
    lk, redis = LiveKit(fail=SipCallError("unavailable", "x", status=400, metadata={"sip_status_code": str(code)})), Redis()
    result = await _call(livekit=lk, redis=redis)
    assert not result.success and not result.retryable and result.error == f"call failed: {said}"
    assert lk.deleted == ["call-out-40"] and not redis.keys


def test_livekit_unreachable_is_retried_and_a_refused_request_is_not():
    assert call_failure(OSError("connection refused")).retryable
    assert not call_failure(ServerError("invalid_argument", "bad trunk", status=400)).retryable


async def test_a_number_without_an_outbound_trunk_cannot_call():
    result = await _call(routing=Routing({}))
    assert not result.success and "cannot place calls" in result.error
    assert (await _call(routing=Routing({}), trunk="ST_default")).success  # the platform's trunk
    result = await _call(routing=Routing(None))
    assert not result.success and "not this agent's line" in result.error


async def test_words_said_in_a_call_and_an_inbound_calls_words_never_dial():
    redis = Redis()
    store = Store()
    await vo.mark_live(redis, store.session.id, 60)
    lk = LiveKit()
    assert (await _call(store=store, livekit=lk, redis=redis)).success and not lk.dialled
    assert (await _call(store=Store("agent:voice:call:SCL_1"), livekit=lk)).success and not lk.dialled


async def test_the_call_waits_for_the_agent_to_finish_writing():
    store, lk = Store(), LiveKit()
    store.session.status = "active"
    result = await _call(store=store, livekit=lk)
    assert not result.success and result.retryable and not lk.dialled


def test_a_yes_acknowledges_an_alert():
    assert acknowledges("Da, am înțeles.") and acknowledges("OK") and acknowledges("yes please")
    assert not acknowledges("Nu acum, sun mai târziu") and not acknowledges("")


async def test_the_call_joins_the_conversation_it_was_placed_from(monkeypatch):
    from surogates.voice.sessions import VoiceSessions

    sid, user, configured = uuid4(), uuid4(), []

    class S:
        async def get_session(self, session_id):
            return SimpleNamespace(org_id=uuid4(), agent_id="agent-1", user_id=user)

        async def update_session_config_key(self, session_id, key, value):
            configured.append((key, value))

    call = await VoiceSessions(store=S(), redis=None, session_factory=None).join_call(
        sid, call_id="out-40", callee="+40712000103", said="Bună ziua, vă sun de la clinică.")
    assert (call.session_id, call.user_id, call.caller) == (sid, user, "40712000103")
    assert "You phoned this person" in call.note and "vă sun de la clinică" in call.note
    assert call.before_first_words is not None and configured == [("voice_call_id", "out-40")]


async def _alert_call(sf, *, severity="red", read=False, granted=("voice", "whatsapp")):
    from datetime import datetime, timezone

    from surogates.db.models import ChannelIdentity, DeliveryOutbox, Event, OutreachMessageRow
    from surogates.followup.store import update_settings

    org, member = uuid4(), uuid4()
    await update_settings(sf, org_id=org, agent_id="a1", senders={"voice": "+40300000001", "whatsapp": "pn1"},
                          team_channels={str(member): ["voice", "whatsapp"]})
    async with sf() as db:
        for platform in granted:
            db.add(ChannelIdentity(org_id=org, user_id=member, platform=platform, platform_user_id="40712000103",
                                   platform_meta={"contact_permission": {"status": "granted"}}))
        row = OutreachMessageRow(org_id=org, agent_id="a1", user_id=member, platform="voice",
                                 channel_identifier="+40300000001", kind="alert", text="Ion: durere în piept",
                                 severity=severity, read_at=datetime.now(timezone.utc) if read else None)
        db.add(row)
        await db.flush()
        event = Event(session_id=uuid4(), type="llm.response", data={"synthetic": "outreach", "outreach_id": str(row.id)})
        db.add(event)
        await db.flush()
        db.add(DeliveryOutbox(id=40, session_id=event.session_id, event_id=event.id, channel="voice",
                              destination={}, payload={}, dedupe_key="voice:1", status="claimed"))
        await db.commit()
    return org, member


async def _rows(sf, org):
    import sqlalchemy as sa

    from surogates.db.models import OutreachMessageRow

    async with sf() as db:
        return (await db.execute(sa.select(OutreachMessageRow).where(OutreachMessageRow.org_id == org)
                                 .order_by(OutreachMessageRow.created_at))).scalars().all()


async def test_an_unacknowledged_urgent_call_goes_on_to_the_members_next_alert_channel(sf):
    org, member = await _alert_call(sf)
    assert await vo.alert_elsewhere(sf, [40], why="call failed: no answer") == 1
    rows = await _rows(sf, org)
    again = [r for r in rows if r.platform == "whatsapp"]
    assert len(again) == 1 and again[0].text == "Ion: durere în piept" and again[0].severity == "red"
    assert again[0].user_id == member and again[0].channel_identifier == "pn1"
    assert await vo.alert_elsewhere(sf, [40], why="again") == 1  # one cause, one message
    assert len([r for r in await _rows(sf, org) if r.platform == "whatsapp"]) == 1


@pytest.mark.parametrize("kw", [{"read": True}, {"severity": "amber"}, {"granted": ("voice",)}])
async def test_a_read_a_normal_or_an_unreachable_alert_is_not_sent_again(sf, kw):
    org, _ = await _alert_call(sf, **kw)
    assert await vo.alert_elsewhere(sf, [40], why="x") == 0
    assert [r.platform for r in await _rows(sf, org)] == ["voice"]


async def test_an_urgent_call_that_cannot_connect_alerts_elsewhere_at_once(monkeypatch):
    sent = []

    async def elsewhere(sf, ids, *, why):
        sent.append((list(ids), why))

    monkeypatch.setattr(vo, "alert_elsewhere", elsewhere)
    lk = LiveKit(fail=SipCallError("unavailable", "x", status=400, metadata={"sip_status_code": "480"}))
    await deliver_voice(rows(9), session_store=Store(), redis=Redis(), routing=Routing({"outbound_trunk_id": "T"}),
                        livekit=lk, settings=settings(), session_factory=object())
    assert sent == [([40], "call failed: no answer")]
    sent.clear()
    await deliver_voice(rows(5), session_store=Store(), redis=Redis(), routing=Routing({"outbound_trunk_id": "T"}),
                        livekit=lk, settings=settings(), session_factory=object())
    assert sent == []  # a step call that fails just falls back as a step
