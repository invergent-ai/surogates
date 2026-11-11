"""The AI plays a person: it writes first, waits for the agent, answers, takes over and hands back."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import respx

from surogates.simulation import actor, store
from tests.test_simulation_api import Settings, _client
from tests.test_simulation_models import rsf  # noqa: F401
from tests.test_simulation_store import line


class _LLM:
    async def close(self):
        pass


@pytest.mark.asyncio
async def test_ai_person_writes_waits_answers_and_hands_over(rsf, monkeypatch):  # noqa: F811
    org_id, client = await _client(rsf)
    org = {"org_id": str(org_id)}
    r = await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1",
        "batch_id": str(uuid4()), "people": [line("Ana", autopilot=True,
                        about="Ana, 60, back pain for a week, wants an appointment next week.")],
    })
    assert r.status_code == 201
    run_json = r.json()
    person_json = run_json["people"][0]
    assert person_json["driver"] == "ai" and person_json["ai_state"] == "talking"

    moves = [actor.Move(action="write", text="Bună ziua, am dureri de spate"),
             actor.Move(action="done", note="Got an appointment")]
    prompts = []

    async def fake_generate(**kw):
        prompts.append(kw["messages"])
        return moves.pop(0)

    monkeypatch.setattr(actor, "generate_structured", fake_generate)
    monkeypatch.setattr(actor, "_client", lambda settings: (_LLM(), "m"))
    ai = actor.Actor(SimpleNamespace(session_factory=rsf, settings=Settings, http=httpx.AsyncClient()))

    [(run, person)] = await store.ai_people(rsf)
    with respx.mock(assert_all_called=True) as mock:
        hook = mock.post(f"http://channels:8003/whatsapp/{run.phone_number_id}").mock(
            return_value=httpx.Response(200))
        await ai._move(run, person)
    assert "dureri de spate" in hook.calls[0].request.content.decode()
    assert "back pain" in prompts[0][0]["content"]

    # Its turn is over until the agent answers.
    [(run, person)] = await store.ai_people(rsf)
    await ai._move(run, person)
    assert len(prompts) == 1

    # The agent answers; once it has been quiet a moment the person moves again and is done.
    await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="out", kind="text",
                            body={"type": "text", "text": {"body": "Vă pot programa marți."}},
                            wamid="wamid.out1")
    monkeypatch.setattr(actor, "QUIET", timedelta(0))
    [(run, person)] = await store.ai_people(rsf)
    await ai._move(run, person)
    assert "Vă pot programa marți" in prompts[1][1]["content"]
    assert await store.ai_people(rsf) == []
    [p] = await store.people(rsf, run.id)
    assert p.ai_state == "done" and p.ai_note == "Got an appointment"

    # Taking over hands the chat to the user; handing back restarts the AI.
    base = f"/internal/runs/{run.id}/people/{p.id}/driver"
    r = await client.put(base, params=org, json={"driver": "human"})
    assert r.json()["people"][0]["driver"] == "human"
    r = await client.put(base, params=org, json={"driver": "ai"})
    assert r.json()["people"][0]["ai_state"] == "talking"
    assert len(await store.ai_people(rsf)) == 1


def test_silent_agent_ends_the_part():
    now = datetime.now(timezone.utc)
    person = SimpleNamespace(ai_acted_at=now - timedelta(minutes=4), agent_typing_at=None)
    assert actor._waiting(person, [], now, turn_ended_at=now - timedelta(minutes=3, seconds=30)) == "no_reply"
    person.ai_acted_at = now - timedelta(seconds=30)
    assert actor._waiting(person, [], now) == "agent"
    person.ai_acted_at = None
    assert actor._waiting(person, [], now) is None


def test_silence_counts_from_the_end_of_the_agents_turn():
    """A cold worker takes minutes to pick a message up: that is no silence."""
    now = datetime.now(timezone.utc)
    person = SimpleNamespace(ai_acted_at=now - timedelta(minutes=5), agent_typing_at=None)
    assert actor._waiting(person, [], now, turn_ended_at=now - timedelta(seconds=30)) == "agent"
    assert actor._waiting(person, [], now, turn_ended_at=now - timedelta(minutes=4)) == "no_reply"
    # The turn hasn't ended: PATIENCE governs, not SILENCE.
    assert actor._waiting(person, [], now, turn_ended_at=None) == "agent"
    # A turn that ended before the person's move is the previous one.
    assert actor._waiting(person, [], now, turn_ended_at=now - timedelta(minutes=6)) == "agent"


async def test_when_the_agent_last_finished_a_turn(rsf):  # noqa: F811
    import uuid

    from surogates.db.models import Event, Org, Session, User

    org_id, user_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with rsf() as db:
        db.add(Org(id=org_id, name="Org"))
        await db.flush()
        db.add(User(id=user_id, org_id=org_id, email="a@x", display_name="Ana"))
        db.add(Session(id=session_id, org_id=org_id, agent_id="a1", channel="whatsapp",
                       user_id=user_id, config={}))
        await db.commit()
    person = SimpleNamespace(user_id=user_id)
    assert await store.agent_turn_ended_at(rsf, person) is None
    ended = datetime(2026, 11, 11, 10, 0, tzinfo=timezone.utc)
    async with rsf() as db:
        db.add(Event(session_id=session_id, type="session.complete", data={}, created_at=ended))
        db.add(Event(session_id=session_id, type="session.complete", data={},
                     created_at=ended - timedelta(minutes=5)))
        db.add(Event(session_id=session_id, type="user.message", data={},
                     created_at=ended + timedelta(minutes=1)))
        await db.commit()
    assert await store.agent_turn_ended_at(rsf, person) == ended
    # A sub-agent finishing doesn't end the agent's turn.
    child = uuid.uuid4()
    async with rsf() as db:
        db.add(Session(id=child, org_id=org_id, agent_id="a1", channel="whatsapp",
                       user_id=user_id, config={}, parent_id=session_id))
        await db.flush()
        db.add(Event(session_id=child, type="session.complete", data={},
                     created_at=ended + timedelta(minutes=2)))
        await db.commit()
    assert await store.agent_turn_ended_at(rsf, person) == ended
    assert await store.agent_turn_ended_at(rsf, SimpleNamespace(user_id=None)) is None


@pytest.mark.asyncio
async def test_a_person_waits_while_the_agent_is_still_working(rsf, monkeypatch):  # noqa: F811
    stopped = []
    ai = actor.Actor(SimpleNamespace(session_factory=rsf, settings=Settings, http=None))

    async def stop(person, state, note):
        stopped.append(state)

    async def thread(sf, person_id):
        return []

    monkeypatch.setattr(ai, "_stop", stop)
    monkeypatch.setattr(store, "thread", thread)
    now = datetime.now(timezone.utc)
    run = SimpleNamespace(created_at=now, channel="whatsapp", flows=None)
    person = SimpleNamespace(id=1, stands_for=None, task=None, agent_typing_at=None,
                             ai_acted_at=now - timedelta(minutes=4))

    async def working(sf, p):
        return True

    async def answered(sf, p):
        return False

    async def not_ended(sf, p):
        return None

    monkeypatch.setattr(store, "agent_working", working)
    monkeypatch.setattr(store, "left_unanswered", answered)
    monkeypatch.setattr(store, "agent_turn_ended_at", not_ended)
    await ai._move(run, person)
    assert stopped == []
    person.ai_acted_at = now - timedelta(minutes=11)
    await ai._move(run, person)
    assert stopped == ["no_reply"]

    # Handed to Autopilot after a stuck turn: the wait still has a ceiling.
    moved = []

    async def generate(**kw):
        moved.append(1)
        return None

    monkeypatch.setattr(actor, "generate_structured", generate)
    monkeypatch.setattr(actor, "_client", lambda settings: (_LLM(), "m"))
    stuck = SimpleNamespace(id=2, name="Ana", persona="Ana", stands_for=None, task=None,
                            agent_typing_at=None, ai_acted_at=None)
    run.created_at = now - timedelta(minutes=11)
    with pytest.raises(RuntimeError):
        await ai._move(run, stuck)
    assert moved


def test_a_form_is_filled_in_not_tapped():
    from surogates.simulation.api import PersonMessage

    form = SimpleNamespace(direction="out", body={"interactive": {
        "type": "flow", "action": {"parameters": {"flow_token": "inrolare:t1"}}}})
    asked = SimpleNamespace(direction="out", body={"interactive": {
        "type": "button", "action": {"buttons": [{"reply": {"id": "choice:0", "title": "Da"}}]}}})
    thread = [asked, form]
    assert actor._form_token(thread) == "inrolare:t1"
    assert "form" in actor._wrong_tap(thread, PersonMessage(button_reply={"id": "flow", "title": "Open"}))
    assert actor._wrong_tap(thread, PersonMessage(button_reply={"id": "choice:0", "title": "Da"})) is None
    assert "empty" in actor._wrong_tap(thread, PersonMessage(form_reply={"flow_token": "inrolare:t1"}))
    assert actor._form_token([SimpleNamespace(direction="out", body={"interactive": {"type": "flow"}})]) is None
    assert actor._wrong_tap(thread, PersonMessage(form_reply={"nume": "Ana", "flow_token": "x"})) is None
    listed = [SimpleNamespace(direction="out", body={"type": "text", "text": {"body": "1. Da 2. Nu"}})]
    assert "as text" in actor._wrong_tap(listed, PersonMessage(button_reply={"id": "choice:0", "title": "Da"}))


@pytest.mark.asyncio
async def test_the_ai_only_writes_on_chat_channels(rsf):  # noqa: F811
    org_id, client = await _client(rsf)
    r = await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1",
        "batch_id": str(uuid4()), "people": [line("Ana", autopilot=True)],
        "channel": "voice",
    })
    assert r.json()["people"][0]["driver"] == "human"
    assert not await store.ai_people(rsf)


def test_a_sent_form_is_described_and_a_refusal_is_fed_back():
    flow = {"id": "7", "name": "signup", "json": {"screens": [{"layout": {"children": [
        {"type": "TextBody", "text": "Hi"},
        {"name": "who", "type": "RadioButtonsGroup", "label": "Who", "required": True,
         "data-source": [{"id": "me"}, {"id": "family"}]},
        {"name": "ok", "type": "OptIn", "label": "Agree"},
    ]}}]}}
    sent = SimpleNamespace(direction="out", wamid="w1", kind="interactive", status="sent", body={
        "interactive": {"type": "flow", "action": {"parameters": {"flow_id": "7", "flow_token": "t"}}}})
    person = SimpleNamespace(name="Ana", persona="You are Ana.")
    system = actor._prompt(person, "whatsapp", [sent], [flow], "Write something")[0]["content"]
    assert "who (RadioButtonsGroup, Who, required, one of me / family)" in system
    assert "ok (OptIn, Agree)" in system
    assert "choose action \"form\"" in system
    assert "refused (Write something)" in system
    assert "Form" not in actor._prompt(person, "whatsapp", [], [flow])[0]["content"]


@pytest.mark.asyncio
async def test_the_agent_owes_an_answer_until_its_turn_ends(rsf):  # noqa: F811
    import uuid

    from surogates.db.models import Event, Org, Session, User

    org_id, user_id, session_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with rsf() as db:
        db.add(Org(id=org_id, name="Org"))
        await db.flush()
        db.add(User(id=user_id, org_id=org_id, email="a@x", display_name="Ana"))
        db.add(Session(id=session_id, org_id=org_id, agent_id="a1", channel="whatsapp",
                       user_id=user_id, config={}))
        await db.commit()
    person = SimpleNamespace(user_id=user_id)

    async def say(event_type, **data):
        async with rsf() as db:
            db.add(Event(session_id=session_id, type=event_type, data=data))
            await db.commit()

    assert not await store.agent_working(rsf, person)
    await say("user.message", synthetic="outreach_opener")
    assert not await store.agent_working(rsf, person)  # The business wrote first: nobody waits.
    await say("user.message")
    assert await store.agent_working(rsf, person)  # Not taken up yet: a disclosure is no answer.
    await say("inbox.input_required")
    assert not await store.agent_working(rsf, person)  # It asked back: the person's turn.
    await say("user.message")
    await say("session.complete")
    assert not await store.agent_working(rsf, person)
    await say("user.message")
    await say("session.fail")  # A failed turn is over too: silence decides then.
    assert not await store.agent_working(rsf, person)

    from datetime import datetime, timedelta, timezone

    from surogates.db.models import SessionLease
    async with rsf() as db:
        db.add(SessionLease(session_id=session_id, owner_id="w", lease_token=uuid.uuid4(),
                            expires_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
        await db.commit()
    assert await store.agent_working(rsf, person)  # A turn is running.
    await say("inbox.input_required")
    assert not await store.agent_working(rsf, person)  # Its question waits on the person.
    await say("ask_user_question.response")
    assert await store.agent_working(rsf, person)  # Answered: the turn goes on.


    # A completed turn with no word back: silence chosen, not missed.
    await say("user.message")
    assert not await store.left_unanswered(rsf, person)
    await say("session.complete")
    assert await store.left_unanswered(rsf, person)
    await say("user.message")
    await say("session.fail")
    assert not await store.left_unanswered(rsf, person)


@pytest.mark.asyncio
async def test_an_edited_placeholder_is_the_agents_answer(rsf):  # noqa: F811
    """Slack's "Thinking…" edited into the answer: the person's turn comes, not a timeout."""
    org_id, client = await _client(rsf)
    r = await client.post("/internal/runs", json={
        "org_id": str(org_id), "agent_id": "a1", "created_by": "u1",
        "batch_id": str(uuid4()), "people": [line("Ana", autopilot=True)],
        "channel": "slack",
    })
    import uuid

    run_id, person_id = uuid.UUID(r.json()["id"]), uuid.UUID(r.json()["people"][0]["id"])
    await store.log_message(rsf, run_id=run_id, person_id=person_id, direction="out", kind="message",
                            body={"text": "_Thinking…_"}, wamid="1.1")
    acted = datetime.now(timezone.utc)
    person = SimpleNamespace(ai_acted_at=acted, agent_typing_at=None, channel="slack")
    later = acted + actor.QUIET + timedelta(seconds=1)
    assert actor._waiting(person, await store.thread(rsf, person_id), later) == "agent"

    assert await store.replace_body(rsf, run_id, "1.1", {"text": "Vă pot programa marți."})
    later = datetime.now(timezone.utc) + actor.QUIET + timedelta(seconds=1)
    assert actor._waiting(person, await store.thread(rsf, person_id), later) is None


@pytest.mark.asyncio
async def test_a_person_handed_back_after_failing_starts_clean(monkeypatch):
    ai = actor.Actor(SimpleNamespace(session_factory=None, settings=Settings, http=None))
    stopped = []

    async def refuse(run, person):
        raise RuntimeError("the move was refused")

    async def stop(person, state, note):
        stopped.append(state)

    monkeypatch.setattr(ai, "_move", refuse)
    monkeypatch.setattr(ai, "_stop", stop)
    person = SimpleNamespace(id=1, name="Ana")
    for _ in range(actor.STRIKES):
        await ai._turn(None, person)
    assert stopped == ["failed"]
    await ai._turn(None, person)  # Handed back: one refusal is a first strike, not a failure.
    assert stopped == ["failed"]


@pytest.mark.asyncio
async def test_a_stand_in_the_agent_never_wrote_to_ends_done_not_out_of_time(monkeypatch):
    ai = actor.Actor(SimpleNamespace(session_factory=None, settings=Settings, http=None))
    stopped = []

    async def stop(person, state, note):
        stopped.append((state, note))

    async def thread(sf, person_id):
        return []

    monkeypatch.setattr(ai, "_stop", stop)
    monkeypatch.setattr(store, "thread", thread)
    run = SimpleNamespace(created_at=datetime.now(timezone.utc) - actor.RUN_CAP - timedelta(minutes=1),
                          channel="whatsapp")
    await ai._move(run, SimpleNamespace(id=1, stands_for=uuid4(), task=None))
    assert stopped == [("done", "The agent never wrote to them")]


@pytest.mark.asyncio
async def test_a_tap_in_the_wrong_shape_is_refused_with_the_right_one(monkeypatch):
    ai = actor.Actor(SimpleNamespace(session_factory=None, settings=Settings, http=None))

    async def thread(sf, person_id):
        return []

    async def idle(sf, person):
        return False

    async def generate(**kw):
        return actor.Move(action="tap", tap={"id": "yes", "title": "Yes"})

    monkeypatch.setattr(store, "thread", thread)
    monkeypatch.setattr(store, "agent_working", idle)
    monkeypatch.setattr(actor, "generate_structured", generate)
    monkeypatch.setattr(actor, "_client", lambda settings: (_LLM(), "m"))
    run = SimpleNamespace(created_at=datetime.now(timezone.utc), channel="whatsapp", flows=None)
    person = SimpleNamespace(id=1, name="Ana", persona="Ana", stands_for=None, task=None,
                             agent_typing_at=None, ai_acted_at=None)
    with pytest.raises(RuntimeError, match='is not a tap; use a reply button: {"button_reply"'):
        await ai._move(run, person)


async def test_a_run_ends_once_everyone_is_done(rsf):  # noqa: F811
    from tests.test_simulation_store import FakeVault, make_run

    vault = FakeVault()
    done, talking, by_hand = [await make_run(rsf, vault) for _ in range(3)]
    for run, driver, state in ((done, "ai", "gave_up"), (talking, "ai", "talking"),
                               (by_hand, "human", "done")):
        [person] = await store.people(rsf, run.id)
        await store.update_person(rsf, person.id, driver=driver, ai_state=state)
    ai = actor.Actor(SimpleNamespace(session_factory=rsf, vault=vault, settings=Settings))
    await ai._finish_done_runs()
    assert (await store.get_run(rsf, done.id)).status == "ended"
    assert (await store.get_run(rsf, talking.id)).status == "running"
    assert (await store.get_run(rsf, by_hand.id)).status == "running"


async def test_a_run_ends_only_with_people_and_after_a_quiet_moment(rsf):  # noqa: F811
    from surogates.db.models import SimulationRun
    from tests.test_simulation_store import make_org, make_run

    now = datetime.now(timezone.utc)
    # Committed, its people not yet added: a run being created.
    empty = SimulationRun(org_id=await make_org(rsf), agent_id="a1", created_by="u1",
                          phone_number_id="9990000000000077", access_token="t", app_secret="s",
                          routing_config={})
    async with rsf() as db:
        db.add(empty)
        await db.commit()
    run = await make_run(rsf)
    [person] = await store.people(rsf, run.id)
    await store.update_person(rsf, person.id, driver="ai", ai_state="done")
    # The agent just wrote: it may be relaying to its team.
    await store.log_message(rsf, run_id=run.id, person_id=person.id, direction="out", kind="text",
                            body={}, wamid="wamid.relay")
    assert await store.done_runs(rsf, now) == []
    assert await store.done_runs(rsf, now + store.GRACE + timedelta(seconds=1)) == [run.id]


def test_a_bare_tap_on_a_one_tap_channel_is_wrapped_and_ids_become_text():
    assert actor._shape("telegram", {"message_id": 7, "data": "si:1"}) == {
        "callback": {"message_id": "7", "data": "si:1"}}
    assert actor._shape("telegram", {"callback": {"message_id": 7, "data": "x"}}) == {
        "callback": {"message_id": "7", "data": "x"}}
    assert actor._shape("slack", {"ts": 1.5, "action_id": "a"}) == {"action": {"ts": "1.5", "action_id": "a"}}
    assert actor._shape("whatsapp", {"id": "yes"}) == {"id": "yes"}
    assert actor._shape("slack", {"view": {"id": "v", "values": {"b": 1}}}) == {"view": {"id": "v", "values": {"b": 1}}}


def test_the_person_knows_today_so_their_dates_are_plausible():
    person = SimpleNamespace(name="Ana", persona="Had surgery two weeks ago")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert today in actor._prompt(person, "whatsapp", [], None)[0]["content"]
