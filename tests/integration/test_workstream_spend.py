"""A project's turns count against the user's allowance and paid turns, as a typed message does."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

import surogates.harness.loop as loop_module
from surogates.api.routes._commerce_turn import AllowanceReserveError, CommerceReserveError
from surogates.db.models import User
from surogates.runtime import SlashCommandConfig
from surogates.runtime.platform_client import AllowanceExhaustedError, CommercePaymentRequiredError
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from tests.test_wake_slash_command_gate import _harness

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import answered, events_of, start, turn_ends
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


def worker(api, monkeypatch, payload: dict, ops: Ops, *, ends: str = "completed"):
    """A worker whose wake runs for real; its turn spends 1,200 tokens in and
    300 out, then ends as the loop ends one: ``completed``, ``stopped`` (the
    user stopped or resolved the thread) or ``failed`` (the provider kept
    failing).  Returns it and the turns it ran."""
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    state = api.app.state
    harness = _harness(state.session_store, SlashCommandConfig())
    del harness._rebuild_messages  # the real replay
    harness._compressor.prune_stale_browser_states.side_effect = lambda messages: messages
    harness._redis, harness._session_factory = state.redis, state.session_factory
    harness._platform_client, harness._runtime_config_cache = ops, RuntimeConfig(payload)
    ran: list = []

    async def turn(session, messages, system_prompt, lease, *, cost_tracker, **_):
        ran.append(session.id)
        cost_tracker.record_call(1200, 300, 0.0)
        if ends == "stopped":
            harness.interrupt("Resolved by the user")  # as the dispatcher relays a stop
            await harness._abort_iteration_with_pause(session, None, cost_tracker)
        elif ends == "failed":
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
