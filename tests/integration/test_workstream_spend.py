"""A project's turns count against the user's allowance and paid turns, as a typed message does."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import update

import surogates.harness.loop as loop_module
from surogates.api.routes._commerce_turn import AllowanceReserveError, CommerceReserveError
from surogates.channels.channel_state import ChannelAdapterState
from surogates.channels.identity import get_or_create_channel_session
from surogates.channels.inbound import ChannelInboundPipeline, InboundMessage, InboundOutcome, PipelineDeps
from surogates.db.models import User
from surogates.harness.loop_context_replay import unread_reports
from surogates.runtime import SlashCommandConfig
from surogates.runtime.platform_client import AllowanceExhaustedError, CommercePaymentRequiredError
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from tests.test_wake_slash_command_gate import _harness

from .test_devices import AGENT_ID, api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import answered, events_of, start, turn_ends, turn_of_the_master_ends
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

BUY = "https://buy.example/q3"
CAPPED = {"end_user_token_allowance": 50_000, "commerce_buy_url": BUY}
PAID = {"commerce_mode": "subscription", "commerce_buy_url": BUY}


class Ops:
    """The allowance and paid-turn planes, as ops answers them."""

    def __init__(self, *, allowance_left: bool = True, paid_left: bool = True, tokens_left: int | None = None) -> None:
        """*tokens_left*, when given, is what is left of the user's allowance:
        ops refuses a hold above it, and takes each turn's spend off it,
        down to zero."""
        self.allowance_left, self.paid_left, self.tokens_left = allowance_left, paid_left, tokens_left
        self.held: list[tuple[str, str, str | None]] = []
        self.spent: list[tuple[str, str, int]] = []

    async def allowance_authorize(self, agent_id, *, end_user_id, estimated_tokens, channel=None):
        self.held.append(("allowance", end_user_id, channel))
        if not self.allowance_left or (self.tokens_left is not None and estimated_tokens > self.tokens_left):
            raise AllowanceExhaustedError("insufficient_allowance")
        return {"allowance_id": "al-1", "reserved_tokens": estimated_tokens,
                "reservation_id": f"hold-{len(self.held)}", "features": None}

    async def allowance_debit(self, agent_id, *, allowance_id, reserved_tokens, actual_tokens, reservation_id=None):
        self.spent.append(("allowance", reservation_id, actual_tokens))
        if self.tokens_left is not None:
            self.tokens_left = max(0, self.tokens_left - actual_tokens)

    async def commerce_authorize(self, agent_id, *, firebase_uid, estimated_tokens, email=None, name=None, channel=None):
        self.held.append(("paid", firebase_uid, channel))
        if not self.paid_left:
            raise CommercePaymentRequiredError("insufficient_tokens")
        return {"entitlement_id": "ent-1", "reserved_tokens": estimated_tokens,
                "reservation_id": f"hold-{len(self.held)}", "features": None}

    async def commerce_debit(self, agent_id, *, entitlement_id, reserved_tokens, actual_tokens, reservation_id=None):
        self.spent.append(("paid", reservation_id, actual_tokens))


class Down(Ops):
    """Ops, unreachable on both planes."""

    async def allowance_authorize(self, agent_id, **_):
        raise ConnectionError("ops is unreachable")

    async def commerce_authorize(self, agent_id, **_):
        raise ConnectionError("ops is unreachable")


class RuntimeConfig:
    """The agent's runtime config, as ops projects it."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def get(self, agent_id):
        return self.payload


def served(api, payload: dict, ops: Ops) -> None:
    """The API checks typed messages against *payload*'s planes, at *ops*."""
    api.app.state.runtime_config_cache = RuntimeConfig(payload)
    api.app.state.platform_client = ops


def worker(api, monkeypatch, payload: dict, ops: Ops, *, ends: str = "completed", turns: tuple = ()):
    """A worker whose wake runs for real; its turn spends 1,200 tokens in and
    300 out, then ends as the loop ends one: ``completed``, ``stopped`` (the
    user stopped or resolved the thread) or ``failed`` (the provider kept
    failing).  *turns*, when given, scripts its wakes' turns in order instead,
    each as the tokens it spends, what the user does while it runs (called
    with the harness and the session), and how it ends.  Returns it and the
    turns it ran."""
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    state = api.app.state
    harness = _harness(state.session_store, SlashCommandConfig())
    del harness._rebuild_messages  # the real replay
    harness._compressor.prune_stale_browser_states.side_effect = lambda messages: messages
    harness._redis, harness._session_factory = state.redis, state.session_factory
    harness._platform_client, harness._runtime_config_cache = ops, RuntimeConfig(payload)
    ran: list = []

    async def resolved(harness, session):
        harness.interrupt("Resolved by the user")  # as the dispatcher relays a stop

    async def turn(session, messages, system_prompt, lease, *, cost_tracker, **_):
        tokens, meanwhile, end = turns[len(ran)] if turns else (1500, resolved if ends == "stopped" else None, ends)
        ran.append(session.id)
        cost_tracker.record_call(tokens * 4 // 5, tokens // 5, 0.0)
        if meanwhile is not None:
            await meanwhile(harness, session)
        if end == "stopped":  # at the loop's next interrupt check
            await harness._abort_iteration_with_pause(session, None, cost_tracker)
        elif end == "failed":
            await harness._fail_session(session, messages, lease, reason="provider_error", cost_tracker=cost_tracker)
        else:
            await harness._complete_session(session, messages, lease, reason="completed", cost_tracker=cost_tracker)

    harness._run_loop = turn
    return harness, ran


async def a_firebase_user(api) -> None:
    async with api.app.state.session_factory() as db:
        await db.execute(update(User).where(User.id == api.user_id).values(
            auth_provider="firebase:google.com", external_id="fb-flavius",
        ))
        await db.commit()


async def test_a_threads_turn_is_held_and_then_spent_against_the_users_allowance(api, monkeypatch):
    master = await master_of(api, await create(api))
    thread = await start(api, master)
    ops = Ops()
    harness, ran = worker(api, monkeypatch, CAPPED, ops)
    await harness.wake(thread.id)
    assert ran == [thread.id]
    assert ops.held == [("allowance", str(api.user_id), "web")]
    assert ops.spent == [("allowance", "hold-1", 1500)]
    assert "allowance_reservations" not in (await api.app.state.session_store.get_session(thread.id)).config


async def test_a_threads_turn_holds_a_paid_turn_on_a_monetized_agent(api, monkeypatch):
    await a_firebase_user(api)
    thread = await start(api, await master_of(api, await create(api)))
    ops = Ops()
    harness, _ = worker(api, monkeypatch, PAID, ops)
    await harness.wake(thread.id)
    # An agent with no per-user cap holds nothing on the allowance plane.
    assert ops.held == [("paid", "fb-flavius", "web")]
    assert ops.spent == [("paid", "hold-1", 1500)]


@pytest.mark.parametrize("ends", ["stopped", "failed"])
async def test_a_stopped_or_failed_threads_turn_is_spent(api, monkeypatch, ends):
    thread = await start(api, await master_of(api, await create(api)))
    ops = Ops()
    harness, ran = worker(api, monkeypatch, CAPPED, ops, ends=ends)
    await harness.wake(thread.id)
    # What it spent before it stopped is counted, and its hold is not left reserved.
    assert (ran, ops.spent) == ([thread.id], [("allowance", "hold-1", 1500)])
    assert "allowance_reservations" not in (await api.app.state.session_store.get_session(thread.id)).config


async def test_a_threads_helper_is_counted_too(api, monkeypatch):
    thread = await start(api, await master_of(api, await create(api)))
    helper = await create_child_session(store=api.app.state.session_store, parent=thread, channel="delegation")
    await api.app.state.session_store.emit_event(helper.id, EventType.USER_MESSAGE, {"content": "Check the totals."})
    ops = Ops()
    harness, ran = worker(api, monkeypatch, CAPPED, ops)
    await harness.wake(helper.id)
    assert (ran, ops.spent) == ([helper.id], [("allowance", "hold-1", 1500)])


@pytest.mark.parametrize("payload, ops, notice", [
    (CAPPED, Ops(allowance_left=False), f"You've reached your usage limit for this assistant. Get more access here: {BUY}"),
    (PAID, Ops(paid_left=False), f"You've reached your usage limit for this assistant. Get more access here: {BUY}"),
], ids=["allowance", "paid"])
async def test_a_thread_does_not_run_once_the_user_has_spent_their_limit(api, monkeypatch, payload, ops, notice):
    await a_firebase_user(api)
    project = await create(api)
    master = await master_of(api, project)
    thread = await start(api, master)
    harness, ran = worker(api, monkeypatch, payload, ops)
    await harness.wake(thread.id)
    assert ran == []
    assert ops.spent == []
    [row] = (await api.client.get(f"/v1/workstreams/{project['id']}/threads", headers=api.auth())).json()
    assert (row["group"], row["reason"], row["status_line"]) == ("waiting", "failed", notice)
    # The master hears of it as of any failure.
    [failed] = await events_of(api, master.id, EventType.WORKER_FAILED)
    assert failed.data["worker_id"] == str(thread.id)


@pytest.mark.parametrize("payload, error", [
    (PAID, CommerceReserveError), (CAPPED, AllowanceReserveError),
], ids=["paid", "allowance"])
async def test_a_threads_turn_does_not_run_while_ops_cannot_be_reached(api, monkeypatch, payload, error):
    await a_firebase_user(api)
    thread = await start(api, await master_of(api, await create(api)))
    harness, ran = worker(api, monkeypatch, payload, Down())
    # It fails closed: the wake crashes, and the dispatcher retries it.
    with pytest.raises(error):
        await harness.wake(thread.id)
    assert ran == []
    assert await events_of(api, thread.id, EventType.HARNESS_CRASH)


async def test_a_paid_turn_the_allowance_then_refuses_is_released(api, monkeypatch):
    await a_firebase_user(api)
    thread = await start(api, await master_of(api, await create(api)))
    harness, ran = worker(api, monkeypatch, {**PAID, **CAPPED}, ops := Ops(allowance_left=False))
    await harness.wake(thread.id)
    # The paid turn was held first, as the message route orders them; the
    # turn never ran, so its hold goes back with nothing spent.
    assert (ran, ops.held) == ([], [("paid", "fb-flavius", "web"), ("allowance", str(api.user_id), "web")])
    assert ops.spent == [("paid", "hold-1", 0)]


async def test_a_message_typed_into_a_thread_is_counted_once(api, monkeypatch):
    master = await master_of(api, await create(api))
    thread = await start(api, master)
    await answered(api, thread, "Drafted the memo.")
    await turn_ends(api, thread)
    ops = Ops()
    served(api, CAPPED, ops)
    response = await api.client.post(
        f"/v1/sessions/{thread.id}/messages", json={"content": "Make it shorter."}, headers=api.auth(),
    )
    assert response.status_code == 202, response.text
    harness, ran = worker(api, monkeypatch, CAPPED, ops)
    await harness.wake(thread.id)
    assert (ran, ops.held, ops.spent) == (
        [thread.id], [("allowance", str(api.user_id), "web")], [("allowance", "hold-1", 1500)],
    )


@pytest.mark.parametrize("payload, plane, buyer", [
    (CAPPED, "allowance", None), (PAID, "paid", "fb-flavius"),
], ids=["allowance", "paid"])
async def test_a_message_typed_to_the_master_is_held_once(api, monkeypatch, payload, plane, buyer):
    await a_firebase_user(api)
    master = await master_of(api, await create(api))
    ops = Ops()
    served(api, payload, ops)
    response = await api.client.post(
        f"/v1/sessions/{master.id}/messages", json={"content": "Where are we?"}, headers=api.auth(),
    )
    assert response.status_code == 202, response.text
    harness, ran = worker(api, monkeypatch, payload, ops)
    await harness.wake(master.id)
    assert (ran, ops.held, ops.spent) == (
        [master.id], [(plane, buyer or str(api.user_id), "web")], [(plane, "hold-1", 1500)],
    )


@pytest.mark.parametrize("payload, ops, status, detail", [
    (PAID, Ops(paid_left=False), 402, {"code": "insufficient_tokens", "buy_url": BUY}),
    (CAPPED, Ops(allowance_left=False), 402, {"code": "insufficient_allowance", "buy_url": BUY}),
    (PAID, Down(), 503, "Access checks are temporarily unavailable; try again."),
    (CAPPED, Down(), 503, "Access checks are temporarily unavailable; try again."),
], ids=["paid", "allowance", "paid-unreachable", "allowance-unreachable"])
async def test_a_message_typed_to_the_master_past_the_users_limit_is_refused(api, payload, ops, status, detail):
    await a_firebase_user(api)
    master = await master_of(api, await create(api))
    served(api, payload, ops)
    response = await api.client.post(
        f"/v1/sessions/{master.id}/messages", json={"content": "Where are we?"}, headers=api.auth(),
    )
    assert (response.status_code, response.json()["detail"]) == (status, detail)
    # The message is not taken, so no turn runs for it.
    typed = await events_of(api, master.id, EventType.USER_MESSAGE)
    assert "Where are we?" not in [event.data.get("content") for event in typed]


async def reported(api):
    """A master whose turn ended, and whose thread has reported since."""
    master = await master_of(api, await create(api))
    thread = await start(api, master)
    await turn_of_the_master_ends(api, master)
    await answered(api, thread, "Drafted the memo.")
    await turn_ends(api, thread)
    return master


async def test_a_report_wake_is_held_and_spent_against_the_users_allowance(api, monkeypatch):
    master = await reported(api)
    ops = Ops()
    harness, ran = worker(api, monkeypatch, CAPPED, ops)
    await harness.wake(master.id)
    assert ran == [master.id]
    assert (ops.held, ops.spent) == ([("allowance", str(api.user_id), "web")], [("allowance", "hold-1", 1500)])


@pytest.mark.parametrize("payload, ops", [
    (CAPPED, Ops(allowance_left=False)), (PAID, Ops(paid_left=False)),
], ids=["allowance", "paid"])
async def test_a_report_waits_while_the_users_limit_is_spent(api, monkeypatch, payload, ops):
    await a_firebase_user(api)
    master = await reported(api)
    harness, ran = worker(api, monkeypatch, payload, ops)
    await harness.wake(master.id)
    assert ran == []
    assert (await api.app.state.session_store.get_session(master.id)).status == "completed"
    assert await events_of(api, master.id, EventType.SESSION_RESUME, EventType.SESSION_FAIL) == []
    # The user's next message, once their limit allows it, reads the report.
    assert unread_reports(await api.app.state.session_store.get_events(master.id))


async def test_a_paid_turn_a_refused_report_held_is_released(api, monkeypatch):
    await a_firebase_user(api)
    master = await reported(api)
    harness, ran = worker(api, monkeypatch, {**PAID, **CAPPED}, ops := Ops(allowance_left=False))
    await harness.wake(master.id)
    # The report waits, and the paid turn it held goes back with nothing spent.
    assert (ran, ops.held) == ([], [("paid", "fb-flavius", "web"), ("allowance", str(api.user_id), "web")])
    assert ops.spent == [("paid", "hold-1", 0)]
    assert "commerce_reservations" not in (await api.app.state.session_store.get_session(master.id)).config
    # The paid balance runs out and the allowance refills: the next report
    # wake asks the paid plane again, which refuses it.
    ops.allowance_left, ops.paid_left = True, False
    await harness.wake(master.id)
    assert (ran, ops.held[2:]) == ([], [("paid", "fb-flavius", "web")])


async def test_a_message_the_allowance_refuses_leaves_no_paid_hold(api, monkeypatch):
    await a_firebase_user(api)
    master = await reported(api)
    served(api, {**PAID, **CAPPED}, ops := Ops(allowance_left=False))
    response = await api.client.post(
        f"/v1/sessions/{master.id}/messages", json={"content": "Where are we?"}, headers=api.auth(),
    )
    assert response.status_code == 402, response.text
    # The paid turn the route held goes back with nothing spent.
    assert ops.spent == [("paid", "hold-1", 0)]
    assert "commerce_reservations" not in (await api.app.state.session_store.get_session(master.id)).config
    # The paid balance runs out and the allowance refills: the report's wake
    # asks the paid plane, which refuses it.
    ops.allowance_left, ops.paid_left = True, False
    harness, ran = worker(api, monkeypatch, {**PAID, **CAPPED}, ops)
    await harness.wake(master.id)
    assert (ran, ops.held[2:]) == ([], [("paid", "fb-flavius", "web")])


async def test_an_ops_outage_at_a_report_wake_leaves_the_master_as_it_was(api, monkeypatch):
    master = await reported(api)
    harness, ran = worker(api, monkeypatch, CAPPED, Down())
    await harness.wake(master.id)
    # No turn runs unheld, and the next report or message wakes it again.
    assert ran == []
    assert (await api.app.state.session_store.get_session(master.id)).status == "completed"
    assert await events_of(api, master.id, EventType.HARNESS_CRASH, EventType.SESSION_RESUME) == []
    assert unread_reports(await api.app.state.session_store.get_events(master.id))


async def test_a_refused_thread_wakes_the_master_at_most_once(api, monkeypatch):
    master = await master_of(api, await create(api))
    await turn_of_the_master_ends(api, master)
    # 100 tokens left: less than a thread's goal, more than a report's 4.
    harness, ran = worker(api, monkeypatch, CAPPED, ops := Ops(tokens_left=100))
    goal = "Draft A from the board's Q3 figures, " * 20
    await harness.wake((await start(api, master, goal=goal)).id)
    # The thread is refused, and its report wakes the master into one turn
    # past the limit, which the settle takes down to nothing left.
    await harness.wake(master.id)
    assert (ran, ops.tokens_left) == ([master.id], 0)
    # From then on a report waits for the user's next message.
    await harness.wake((await start(api, master, title="Summarise B", goal=goal)).id)
    await harness.wake(master.id)
    assert ran == [master.id]


async def a_chat(api) -> UUID:
    """An ordinary chat, outside any project."""
    response = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def typed(api, session_id, text: str) -> None:
    response = await api.client.post(f"/v1/sessions/{session_id}/messages", json={"content": text}, headers=api.auth())
    assert response.status_code == 202, response.text


async def acted(api, session_id, action: str) -> None:
    """The user pauses, resumes or retries *session_id*."""
    response = await api.client.post(f"/v1/sessions/{session_id}/{action}", headers=api.auth())
    assert response.status_code == 200, response.text


def paused(api, *, then_typed: str | None = None):
    """The user pauses the chat while its turn runs, and types *then_typed*
    before the worker's next interrupt check."""
    async def act(harness, session):
        await acted(api, session.id, "pause")
        harness.interrupt("paused by user")  # as the worker's interrupt listener relays it
        if then_typed is not None:
            await typed(api, session.id, then_typed)
    return act


async def charged(api, monkeypatch, payload, ops, first, *, after=None) -> list:
    """An ordinary chat's first turn plays *first*, *after* runs, then its
    next turn spends 1,500 tokens and completes.  Returns what ops debited."""
    await a_firebase_user(api)
    served(api, payload, ops)
    chat = await a_chat(api)
    await typed(api, chat, "Draft the memo.")
    harness, ran = worker(api, monkeypatch, payload, ops, turns=(first, (1500, None, "completed")))
    await harness.wake(chat)
    if after is not None:
        await after(chat)
    await harness.wake(chat)
    assert ran == [chat, chat]
    return ops.spent


PLANES = pytest.mark.parametrize("payload, plane", [(CAPPED, "allowance"), (PAID, "paid")], ids=["allowance", "paid"])


# Only a project's session holds its next turn again at its wake.  Any other
# session's stopped or failed turn leaves its holds to the turn that runs
# next, a resume's, a retry's or a message's typed meanwhile, which spends them.

@PLANES
async def test_a_message_typed_while_an_ordinary_chat_stops_is_charged_in_full(api, monkeypatch, payload, plane):
    spent = await charged(api, monkeypatch, payload, Ops(), (150, paused(api, then_typed="Make it shorter."), "stopped"))
    assert spent == [(plane, "hold-1", 1500), (plane, "hold-2", 0)]


@PLANES
@pytest.mark.parametrize("ends, action", [("stopped", "resume"), ("failed", "retry")], ids=["resumed", "retried"])
async def test_an_ordinary_chat_resumed_or_retried_is_charged_in_full(api, monkeypatch, payload, plane, ends, action):
    spent = await charged(
        api, monkeypatch, payload, Ops(), (150, paused(api) if ends == "stopped" else None, ends),
        after=lambda chat: acted(api, chat, action),
    )
    assert spent == [(plane, "hold-1", 1500)]


@PLANES
async def test_an_ordinary_chat_stopped_at_once_and_resumed_past_the_limit_is_charged_in_full(
    api, monkeypatch, payload, plane,
):
    ops = Ops()

    async def resumed_past_the_limit(chat):
        ops.allowance_left = ops.paid_left = False  # a resume is not held again
        await acted(api, chat, "resume")

    spent = await charged(api, monkeypatch, payload, ops, (0, paused(api), "stopped"), after=resumed_past_the_limit)
    assert spent == [(plane, "hold-1", 1500)]


async def test_a_message_sent_after_a_channel_stop_is_charged_in_full(api, monkeypatch):
    ops, pipeline, chats = Ops(), ChannelInboundPipeline(), []
    routing = SimpleNamespace(org_id=api.org_id, agent_id=AGENT_ID, platform="slack", identifier="A0APP")

    async def enqueued(redis, *, org_id, agent_id, session_id):
        chats.append(session_id)

    async def resolved_identity(*_, **__):
        return SimpleNamespace(user_id=api.user_id)

    async def nothing(*_, **__):
        return None

    deps = PipelineDeps(
        session_store=api.app.state.session_store, redis=api.app.state.redis,
        state=ChannelAdapterState(api.app.state.redis, agent_id=AGENT_ID, platform="slack"),
        firehose_append=nothing, get_or_create_session=get_or_create_channel_session,
        enqueue_session=enqueued, resolve_identity=resolved_identity,
        session_factory=api.app.state.session_factory, platform_client=ops, runtime_config=RuntimeConfig(CAPPED).get,
    )

    async def sent(text: str, ts: str) -> InboundOutcome:
        """The user sends *text* to the agent in a Slack DM."""
        message = InboundMessage(
            kind="text", identifier="D1", thread_key=None, platform_user_id="U1", user_name="Flavius",
            text=text, media_urls=[], media_types=[], is_dm=True, is_mention=False, ts=ts, source={},
        )
        return await pipeline.handle(message, routing=routing, config={"require_mention": True}, deps=deps)

    async def stopped_and_sent_again(harness, session):
        assert await sent("/stop", "2.0") == InboundOutcome.INTERRUPTED
        harness.interrupt("channel_stop")  # as the worker's interrupt listener relays it
        assert await sent("Draft the letter instead.", "3.0") == InboundOutcome.PROCESSED

    assert await sent("Draft the memo.", "1.0") == InboundOutcome.PROCESSED
    harness, ran = worker(api, monkeypatch, CAPPED, ops, turns=(
        (150, stopped_and_sent_again, "stopped"), (1500, None, "completed"),
    ))
    await harness.wake(chats[0])
    await harness.wake(chats[0])
    assert ran == [chats[0], chats[0]]
    assert ops.spent == [("allowance", "hold-1", 1500), ("allowance", "hold-2", 0)]
