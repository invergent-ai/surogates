"""A call drives its agent session directly: post, stream, stop, and keep the history true after a barge-in.

Real Postgres and Redis: the session store's SQL is Postgres-only, and the turn is read back
through the session's pub/sub nudges.
"""
from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from surogates.config import SHARED_WORK_QUEUE_KEY
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.voice.sessions import HEARD_NONE, CallSession
from tests.integration.conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def call(session_factory, redis_client):
    store = SessionStore(session_factory, redis=redis_client)
    org = await create_org(session_factory)
    user = await create_user(session_factory, org)
    s = await store.create_session(user_id=user, org_id=org, agent_id="agent-1", channel="voice")
    await redis_client.delete(SHARED_WORK_QUEUE_KEY)
    return CallSession(store=store, redis=redis_client, session_id=s.id, org_id=org, agent_id="agent-1",
                       user_id=user, caller="40722000111")


async def _user_messages(call):
    return [e.data["content"] for e in await call.store.get_events(call.session_id)
            if e.type == EventType.USER_MESSAGE.value]


async def test_send_posts_the_utterance_and_wakes_the_agent(call):
    after = await call.send("Bună ziua")
    assert await _user_messages(call) == ["Bună ziua"]
    queued = await call.redis.zrange(SHARED_WORK_QUEUE_KEY, 0, -1)
    assert after > 0 and any(str(call.session_id) in m.decode() for m in queued)


async def test_stream_follows_the_turn_live_and_stops_at_the_final_answer(call):
    after = await call.send("Cât e euro?")
    emit = call.store.emit_event

    async def agent():  # the harness writing its turn while the call is already listening
        await asyncio.sleep(0.05)
        await emit(call.session_id, EventType.LLM_REQUEST, {})
        await emit(call.session_id, EventType.LLM_DELTA, {"content": "O clipă, verific. "})
        await emit(call.session_id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "tool_calls": [{"id": "1"}]}})
        await asyncio.sleep(0.05)
        await emit(call.session_id, EventType.LLM_DELTA, {"content": "Euro e 4,97 lei."})
        await emit(call.session_id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "Euro e 4,97 lei."}})
        await emit(call.session_id, EventType.LLM_DELTA, {"content": "from the next turn"})

    writer = asyncio.create_task(agent())
    text = "".join([t async for t in call.stream(after)])
    await writer
    assert text == "O clipă, verific. Euro e 4,97 lei."


async def test_a_question_from_the_agent_is_spoken_and_ends_the_turn(call):
    after = await call.send("Vreau o programare")
    args = {"questions": [{"prompt": "Ce zi vă convine?", "choices": [{"label": "luni"}, {"label": "marți"}]}]}
    await call.store.emit_event(call.session_id, EventType.LLM_REQUEST, {})
    await call.store.emit_event(call.session_id, EventType.TOOL_CALL,
                                {"tool_call_id": "q1", "name": "ask_user_question", "arguments": json.dumps(args)})
    assert [t async for t in call.stream(after)] == ["Ce zi vă convine? Variante: luni, marți."]


async def test_interrupt_stops_the_turn_without_pausing_the_session(call):
    pubsub = call.redis.pubsub()
    await pubsub.subscribe(f"surogates:interrupt:{call.session_id}")
    await pubsub.get_message(timeout=1)  # the subscribe confirmation
    await call.interrupt()
    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
    await pubsub.aclose()
    assert json.loads(msg["data"]) == {"reason": "channel_stop"}
    assert (await call.store.get_session(call.session_id)).status != "paused"


async def test_barge_in_mid_generation_records_only_what_was_heard(call):
    await call.send("Spune-mi știrile")
    await call.record_heard("Prima știre este despre")
    replies = [e.data for e in await call.store.get_events(call.session_id) if e.type == EventType.LLM_RESPONSE.value]
    assert replies == [{"message": {"role": "assistant", "content": "Prima știre este despre"}, "synthetic": "voice_heard"}]
    await call.send("Altceva")
    assert (await _user_messages(call))[-1] == "Altceva"


async def test_barge_in_after_the_answer_was_written_tells_the_agent_on_the_next_turn(call):
    await call.send("Spune-mi știrile")
    await call.store.emit_event(call.session_id, EventType.LLM_RESPONSE,
                                {"message": {"role": "assistant", "content": "Prima știre... A doua știre..."}})
    await call.record_heard("Prima știre")
    await call.send("Stop")
    assert (await _user_messages(call))[-1] == "[Apelantul te-a întrerupt; din răspunsul tău anterior a auzit doar: «Prima știre».] Stop"


async def test_cut_before_a_word_was_heard(call):
    await call.send("Salut")
    await call.record_heard("")
    await call.send("Alo?")
    assert (await _user_messages(call))[-1] == f"{HEARD_NONE}Alo?"


async def test_the_previous_turn_ending_does_not_end_the_callers_new_turn(call):
    """Barge-in: the old turn's tail, its answer and its stop land after the caller's new words."""
    after = await call.send("Stop, spune-mi doar anul.")
    emit = call.store.emit_event
    await emit(call.session_id, EventType.LLM_DELTA, {"content": "...tail of the story nobody asked for. "})
    await emit(call.session_id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "The whole story."}})
    await emit(call.session_id, EventType.SESSION_STOPPED, {"reason": "channel_stop"})
    await emit(call.session_id, EventType.LLM_REQUEST, {})
    await emit(call.session_id, EventType.LLM_DELTA, {"content": "În 1659."})
    await emit(call.session_id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "În 1659."}})
    assert "".join([t async for t in call.stream(after)]) == "În 1659."


async def test_a_failed_session_ends_the_turn_even_before_it_started(call):
    after = await call.send("Alo?")
    await call.store.emit_event(call.session_id, EventType.SESSION_FAIL, {"reason": "crash_loop_detected"})
    assert [t async for t in call.stream(after)] == []


async def test_hanging_up_ends_the_session_so_nothing_reruns_after_the_caller_left(call):
    """A session left active after the call is found by the orphan sweeper and re-run for nobody."""
    pubsub = call.redis.pubsub()
    await pubsub.subscribe(f"surogates:interrupt:{call.session_id}")
    await pubsub.get_message(timeout=1)
    await call.send("Caută cursul euro")
    await call.end()
    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
    await pubsub.aclose()
    assert msg and json.loads(msg["data"]) == {"reason": "channel_stop"}  # a turn still running is stopped
    assert (await call.store.get_session(call.session_id)).status == "completed"
    last = (await call.store.get_events(call.session_id))[-1]
    assert (last.type, last.data) == ("session.complete", {"reason": "call_ended"})


async def test_a_call_where_nobody_spoke_ends_cleanly_too(call):
    await call.end()
    assert (await call.store.get_session(call.session_id)).status == "completed"
