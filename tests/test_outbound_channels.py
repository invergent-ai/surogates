"""Writing first on voice, web and api: where the person's conversation is, how each
channel delivers into it, and what comes back."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa

from surogates.channels import outbound
from surogates.channels.base import SendResult
from surogates.channels.constants import outbound_dm
from surogates.programs.opener import OpenerUndeliverable, resolve_dm
from surogates.session.events import EventType


# ---------------------------------------------------------------------------
# The person's own conversation
# ---------------------------------------------------------------------------


async def test_each_channel_writes_into_the_conversation_its_inbound_side_uses():
    voice = await resolve_dm("voice", "40712000103", "+40300000001")
    assert voice.session_key == "agent:voice:dm:40712000103"
    assert voice.destination == {"to": "+40712000103", "channel_identifier": "+40300000001"}
    web = await resolve_dm("web", "u-1", "")
    assert web.session_key == "agent:web:dm:u-1" and web.destination["user_id"] == "u-1"
    api = await resolve_dm("api", "crm-42", "agent-1")
    assert api.session_key == "agent:api:dm:crm-42" and api.destination["external_id"] == "crm-42"


async def test_no_number_no_call_and_no_webhook_no_api_delivery():
    with pytest.raises(OpenerUndeliverable, match="phone number"):
        await resolve_dm("voice", "40712000103", "")
    with pytest.raises(OpenerUndeliverable, match="webhook"):
        await resolve_dm("api", "crm-42", "")
    with pytest.raises(OpenerUndeliverable, match="cannot write first"):
        await resolve_dm("website", "x", "y")


async def test_teams_writes_into_the_personal_chat_its_bot_recorded():
    from surogates.channels.platforms.teams import _remember_conversation

    class _Redis(dict):
        async def set(self, key, value, ex=None):
            self[key] = value

        async def get(self, key):
            return dict.get(self, key)

    redis = _Redis()
    with pytest.raises(OpenerUndeliverable, match="Teams chat"):
        await resolve_dm("teams", "aad-1", "app-1", redis=redis)
    await _remember_conversation(redis, "app-1", {
        "type": "conversationUpdate", "serviceUrl": "https://smba.example.test/emea/",
        "from": {"id": "29:x", "aadObjectId": "aad-1"},
        "conversation": {"id": "a:conv-1", "conversationType": "personal", "tenantId": "t"},
    })
    target = await resolve_dm("teams", "aad-1", "app-1", redis=redis)
    # Keyed on the conversation id, as inbound keys the personal chat.
    assert target.session_key == "agent:teams:dm:a:conv-1"
    assert target.destination == {
        "conversation_id": "a:conv-1", "service_url": "https://smba.example.test/emea/",
        "reply_to_id": None, "channel_identifier": "app-1",
    }


def test_only_the_persons_own_conversation_gets_outbox_rows():
    assert outbound_dm("voice", {"channel_session_key": "agent:voice:dm:40712"})
    assert outbound_dm("web", {"channel_session_key": "agent:web:dm:u-1"})
    assert not outbound_dm("voice", {"channel_session_key": "agent:voice:call:SCL_1"})  # a live inbound call
    assert not outbound_dm("web", {})  # an ordinary web chat
    assert not outbound_dm("api", None)  # an integrator's own API session


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, *, channel="web", key="agent:web:dm:u-1", event=None, viewer=False, config=None):
        self.session = SimpleNamespace(
            id=uuid4(), channel=channel, agent_id="agent-1", org_id=uuid4(), user_id=uuid4(),
            config={"channel_session_key": key, **(config or {})},
        )
        self.event, self.viewer, self.emitted = event, viewer, []

    async def get_session(self, session_id):
        return self.session

    async def get_event_by_id(self, session_id, event_id):
        return self.event

    async def emit_event(self, session_id, etype, data, **kw):
        self.emitted.append((etype, data))
        return 99

    async def _session_has_live_viewer(self, session_id):
        return self.viewer


def item(content="Bună ziua! Cum vă simțiți azi?", **kw):
    return SimpleNamespace(id=kw.pop("id", 7), session_id=uuid4(), event_id=5, payload={"content": content},
                           created_at=kw.pop("created_at", datetime.now(timezone.utc)), **kw)


def opener_event():
    return SimpleNamespace(type="user.message", data={"synthetic": "checkin_opener", "program_id": "p-1",
                                                      "occurrence_id": "o-1"})


def reply_event(**data):
    return SimpleNamespace(type="llm.response", data=data)


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------


async def test_an_opener_waits_in_the_web_chat_as_the_agents_message():
    store, pushed = Store(event=opener_event()), []
    result = await outbound.deliver_web([item()], session_store=store, push=lambda s, t: pushed.append(t))
    assert result.success and result.message_id == "web-99"
    [(etype, data)] = store.emitted
    assert etype == EventType.LLM_RESPONSE and data["message"]["content"].startswith("Bună ziua")
    assert pushed == []  # the message it wrote gets its own row, which pushes


async def test_the_agents_message_is_pushed_unless_the_person_is_watching():
    pushed = []

    async def push(session, text):
        pushed.append(text)

    away = Store(event=reply_event(message={"content": "x"}))
    assert (await outbound.deliver_web([item("Ai uitat pastila?")], session_store=away, push=push)).success
    watching = Store(event=reply_event(message={"content": "x"}), viewer=True)
    await outbound.deliver_web([item("Altceva")], session_store=watching, push=push)
    assert pushed == ["Ai uitat pastila?"]


async def test_a_failed_push_still_delivers_and_an_ordinary_chat_is_left_alone():
    async def broken(session, text):
        raise RuntimeError("push service down")

    store = Store(event=reply_event())
    assert (await outbound.deliver_web([item()], session_store=store, push=broken)).success
    plain = Store(key="", event=reply_event())
    assert (await outbound.deliver_web([item()], session_store=plain, push=broken)).message_id is None


# ---------------------------------------------------------------------------
# api
# ---------------------------------------------------------------------------


class Ops:
    def __init__(self, status=202, exc=None):
        self.status, self.exc, self.posts = status, exc, []

    async def post(self, url, *, json, timeout):
        self.posts.append((url, json))
        if self.exc:
            raise self.exc
        return SimpleNamespace(status_code=self.status, json=lambda: {"message_id": "d-uuid"})


def api_store():
    return Store(channel="api", key="agent:api:dm:crm-42", event=opener_event(),
                 config={"channel_identifier": "agent-1", "api_channel_id": "crm-42"})


async def test_api_delivery_is_handed_to_ops_for_the_agents_webhook():
    ops = Ops()
    result = await outbound.deliver_api([item(id=12)], session_store=api_store(), ops=ops)
    assert result.success and result.message_id == "d-uuid"  # the id the integrator reports against
    [(url, body)] = ops.posts
    assert url == "/api/agents/agent-1/runtime/api-deliveries"
    assert body["outbox_id"] == 12 and body["external_id"] == "crm-42" and body["kind"] == "step"
    assert body["text"].startswith("Bună ziua")


async def test_the_step_the_agent_wrote_is_named_in_the_hand_off(sf):
    from datetime import datetime, timezone

    from tests import test_program_agent_steps as steps

    program = await steps._program(sf, channel="api", channel_identifier="agent-1", name="Day 3 check")
    inv = await steps._invitation(sf, program, await steps._person(sf), platform="api",
                                  platform_user_id="crm-42", response_state="awaiting_reply",
                                  session_id=steps.SESSION, woken_at=datetime.now(timezone.utc))
    store = api_store()
    store.event = reply_event(message={"content": "x"})  # the agent's own message, no step marker
    row = item("Cum vă simțiți în a treia zi?", id=13)
    row.session_id = steps.SESSION
    ops = Ops()
    assert (await outbound.deliver_api([row], session_store=store, ops=ops, session_factory=sf)).success
    body = ops.posts[0][1]
    assert body["step"] == {"id": str(program.program_id), "name": "Day 3 check", "journey": "recovery"}
    assert body["invitation_id"] == str(inv.id) and body["kind"] == "step"


@pytest.mark.parametrize("status, exc, retryable", [
    (500, None, True), (429, None, True), (None, OSError("refused"), True), (409, None, False), (400, None, False),
])
async def test_the_hand_off_is_retried_only_when_retrying_can_help(status, exc, retryable):
    result = await outbound.deliver_api([item()], session_store=api_store(), ops=Ops(status or 0, exc))
    assert not result.success and result.retryable is retryable


async def test_an_integrators_reply_reaches_the_agent_in_the_persons_conversation(monkeypatch):
    from surogates.channels import api_inbound

    with pytest.raises(api_inbound.Refused):
        class Empty:
            async def execute(self, q):
                return SimpleNamespace(scalar_one_or_none=lambda: None)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        await api_inbound.receive(lambda: Empty(), None, org_id=uuid4(), agent_id="agent-1",
                                  external_id="nobody", text="hi")


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


class Delivery:
    def __init__(self, items):
        self.items, self.calls = items, []

    async def claim_batch(self, kind, worker, *, limit):
        items, self.items = self.items, []
        return items

    async def mark_delivered(self, oid, *, provider_message_id=None, payload=None):
        self.calls.append(("delivered", oid, provider_message_id))

    async def mark_failed(self, oid, error):
        self.calls.append(("failed", oid, error))

    async def mark_dead(self, oid, error, *, payload=None):
        self.calls.append(("dead", oid, error))


async def test_a_conversations_rows_go_out_together_each_with_its_own_provider_id():
    sid = uuid4()
    rows = [SimpleNamespace(id=i, session_id=sid, payload={}, created_at=datetime.now(timezone.utc)) for i in (1, 2)]
    delivery, seen = Delivery(rows), []

    async def handler(group):
        seen.append([r.id for r in group])
        return SendResult(success=True, message_id="out-1")

    assert await outbound.deliver_once(delivery, None, "voice", handler, by_session=True) == 2
    assert seen == [[1, 2]]
    assert delivery.calls == [("delivered", 1, "out-1#1"), ("delivered", 2, "out-1#2")]
    alone = Delivery([SimpleNamespace(id=3, session_id=sid, payload={}, created_at=datetime.now(timezone.utc))])
    await outbound.deliver_once(alone, None, "api", handler)
    assert alone.calls == [("delivered", 3, "out-1")]  # sent alone: the send's own id


async def test_a_final_failure_is_dead_at_once_and_a_raise_is_retried():
    rows = [SimpleNamespace(id=1, session_id=uuid4(), payload={}, created_at=datetime.now(timezone.utc))]
    delivery = Delivery(list(rows))
    await outbound.deliver_once(delivery, None, "voice", lambda g: _result(False, "call failed: busy"))
    assert delivery.calls == [("dead", 1, "call failed: busy")]

    async def boom(group):
        raise RuntimeError("livekit down")

    delivery = Delivery(list(rows))
    await outbound.deliver_once(delivery, None, "voice", boom)
    assert delivery.calls == [("failed", 1, "livekit down")]


async def _result(ok, error=None, retryable=False):
    return SendResult(success=ok, error=error, retryable=retryable)


async def test_webhook_retries_back_off_with_age_and_give_up(sf):
    from surogates.db.models import DeliveryOutbox

    created = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with sf() as db:
        db.add(DeliveryOutbox(id=1, session_id=uuid4(), event_id=1, channel="api", destination={}, payload={},
                              dedupe_key="api:1", status="claimed", created_at=created))
        await db.commit()
    row = SimpleNamespace(id=1, session_id=uuid4(), payload={}, created_at=created)
    delivery = Delivery([])
    await outbound.settle(delivery, sf, [row], SendResult(success=False, error="webhook answered 503",
                                                          retryable=True), max_age=outbound.API_MAX_AGE)
    async with sf() as db:
        saved = await db.get(DeliveryOutbox, 1)
    wait = saved.available_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert saved.status == "pending" and timedelta(minutes=9) < wait < timedelta(minutes=11)
    assert delivery.calls == []

    old = SimpleNamespace(id=1, session_id=uuid4(), payload={}, created_at=created - outbound.API_MAX_AGE)
    await outbound.settle(delivery, sf, [old], SendResult(success=False, error="503", retryable=True),
                          max_age=outbound.API_MAX_AGE)
    assert delivery.calls == [("dead", 1, "503")]


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


async def test_an_acknowledgement_reaches_the_step_the_call_was_placed_for(sf, make_invitation, reload_invitation):
    inv = await make_invitation(delivery_state="accepted", response_state="awaiting_reply", platform="voice",
                                platform_user_id="40712000103", provider_message_id="out-12")
    assert await outbound.record_status(sf, provider_message_id="out-12", status="read")
    saved = await reload_invitation(inv.id)
    assert saved.delivery_state == "delivered" and saved.read_at is not None
    assert not await outbound.record_status(sf, provider_message_id="out-99", status="read")


async def test_an_answer_on_a_channel_without_inbound_binds_the_step(sf, make_invitation, reload_invitation,
                                                                    monkeypatch):
    step_context = pytest.importorskip("surogates.programs.step_context")

    async def context(sf_, invitation, **kw):
        return f"[Step] {invitation.id} {kw['event']}"

    monkeypatch.setattr(step_context, "step_context", context)
    from surogates.db.models import Base

    if "followup_contacts" in Base.metadata.tables:  # a step answered for the person and their contacts
        async with sf() as db:
            await db.run_sync(lambda s: Base.metadata.tables["followup_contacts"].create(s.connection(),
                                                                                        checkfirst=True))
    inv = await make_invitation(delivery_state="delivered", response_state="awaiting_reply", platform="voice",
                                platform_user_id="40712000103")
    notes, sid = [], uuid4()

    class Notes:
        async def emit_synthetic_user_message(self, session_id, *, content, synthetic, metadata):
            notes.append((session_id, content, synthetic, metadata["invitation_id"]))

    assert await outbound.begin_step_reply(sf, Notes(), org_id=inv.org_id, agent_id="a1", platform="voice",
                                           platform_user_id="40712000103", session_id=sid)
    saved = await reload_invitation(inv.id)
    assert saved.response_state == "replied" and saved.session_id == sid
    assert notes == [(sid, f"[Step] {inv.id} reply", "checkin", str(inv.id))]
    # a second sentence is ordinary conversation
    assert not await outbound.begin_step_reply(sf, Notes(), org_id=inv.org_id, agent_id="a1", platform="voice",
                                               platform_user_id="40712000103", session_id=sid)


# ---------------------------------------------------------------------------
# Same number, same person
# ---------------------------------------------------------------------------


async def _person(sf, org_id, *identities, provider="whatsapp"):
    from surogates.db.models import ChannelIdentity, Org, User

    async with sf() as db:
        if await db.get(Org, org_id) is None:
            db.add(Org(id=org_id, name="clinic"))
        user = User(id=uuid4(), org_id=org_id, email=f"{uuid4()}@x", display_name="Ion", auth_provider=provider)
        db.add(user)
        await db.flush()
        for platform, puid in identities:
            db.add(ChannelIdentity(org_id=org_id, user_id=user.id, platform=platform, platform_user_id=puid))
        await db.commit()
    return user.id


async def _voice_owner(sf, org_id, number):
    from surogates.db.models import ChannelIdentity

    async with sf() as db:
        return (await db.execute(sa.select(ChannelIdentity.user_id).where(ChannelIdentity.org_id == org_id)
                                 .where(ChannelIdentity.platform == "voice")
                                 .where(ChannelIdentity.platform_user_id == number))).scalar_one_or_none()


async def test_a_call_from_a_whatsapp_number_is_that_person(sf):
    from surogates.voice.sessions import same_person

    org = uuid4()
    ion = await _person(sf, org, ("whatsapp", "40712000103"))
    assert await same_person(sf, org, "40712000103") == ion
    assert await _voice_owner(sf, org, "40712000103") == ion  # the next call finds them directly
    assert await same_person(sf, org, "40712000999") is None  # exact match only
    assert await same_person(sf, uuid4(), "40712000103") is None  # never across orgs


async def test_an_earlier_callers_account_gives_way_to_the_whatsapp_person(sf):
    from surogates.voice.sessions import same_person

    org = uuid4()
    await _person(sf, org, ("voice", "40712000103"), provider="voice")  # called before they enrolled
    ion = await _person(sf, org, ("whatsapp", "40712000103"))
    assert await same_person(sf, org, "40712000103") == ion
    assert await _voice_owner(sf, org, "40712000103") == ion


async def test_a_number_linked_to_a_real_account_stays_with_it(sf):
    from surogates.voice.sessions import same_person

    org = uuid4()
    maria = await _person(sf, org, ("voice", "40712000103"), provider="database")
    await _person(sf, org, ("whatsapp", "40712000103"))
    assert await same_person(sf, org, "40712000103") == maria


# ---------------------------------------------------------------------------
# The agent writes the step on voice, web and api too
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform, pid, sender, key", [
    ("voice", "40712000103", "+40300000001", "agent:voice:dm:40712000103"),
    ("web", "u-1", "web", "agent:web:dm:u-1"),
    ("api", "crm-42", "a1", "agent:api:dm:crm-42"),
])
async def test_a_due_step_wakes_the_agent_in_the_persons_own_conversation(sf, monkeypatch, platform, pid, sender,
                                                                          key):
    """What the agent then writes there is the call, the pending message and push, or the webhook post."""
    pytest.importorskip("surogates.programs.step_context")
    from tests import test_program_agent_steps as steps
    from surogates.programs import opener as opener_mod

    keys = []

    async def dm_session(*args, session_key, **kw):
        keys.append(session_key)
        return steps.SESSION

    w = steps._wiring(sf, monkeypatch)
    monkeypatch.setattr(opener_mod, "dm_session", dm_session)
    program = await steps._program(sf, channel=platform, channel_identifier=sender)
    inv = await steps._invitation(sf, program, await steps._person(sf), platform=platform, platform_user_id=pid)
    inv.channel_identifier = sender

    result = await w.enqueue(inv)
    assert isinstance(result, steps.Woken) and w.delivery.calls == []  # no word-for-word opener
    assert keys == [key] and w.woke == [steps.SESSION]
    assert w.store.emitted[0][1]["synthetic"] == "checkin"
