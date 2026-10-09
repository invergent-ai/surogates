"""Which events left past the cursor give a wake work to do."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.loop_pending import _actionable_pending_events, _turn_for_a_hand_back
from surogates.session.events import EventType
from tests.test_wake_slash_command_gate import _harness, _permissive, _session, _stub_store


def event(id_: int, type_: EventType) -> SimpleNamespace:
    return SimpleNamespace(id=id_, type=type_)


def test_device_wait_events_give_a_wake_no_work():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.DEVICE_RESUMED)]
    assert _actionable_pending_events(events, cursor=4) == []


def test_a_user_message_still_does():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.USER_MESSAGE)]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [6]


# What tells a chat's pane of its browser, and that its user took it over: none is work for the agent.
FOR_THE_PANE = [
    EventType.BROWSER_CONTROL_GRANTED,
    EventType.BROWSER_PROVISIONED,
    EventType.BROWSER_DESTROYED,
    EventType.BROWSER_UNAVAILABLE,
]


@pytest.mark.parametrize("told", FOR_THE_PANE, ids=lambda kind: kind.value)
def test_a_browsers_take_over_and_its_opening_and_closing_give_a_wake_no_work(told):
    assert _actionable_pending_events([event(5, told)], cursor=4) == []


def handed_back(id_: int, **said) -> SimpleNamespace:
    """A hand back as the control route writes it: a computer's says it is one, the cloud's does not."""
    return SimpleNamespace(id=id_, type=EventType.BROWSER_CONTROL_RETURNED.value, data={"released_by": "user-1", **said})


# A hand back on the user's computer after a take-over that had stopped the chat's agent.
STOPPED = {"computer": True, "resumes": True}


def test_the_clouds_hand_back_still_gives_a_wake_work():
    # As it was: a release of the cloud's browser is the session's wake.
    events = [event(5, EventType.BROWSER_CONTROL_GRANTED), handed_back(6)]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [6]


def test_a_computers_hand_back_gives_a_wake_work_only_when_the_take_over_had_stopped_the_agent():
    stopped = handed_back(5, **STOPPED)
    # Nothing was stopped, or it was made from another chat: told for the pane, as the take-over was.
    nothing_stopped = handed_back(6, computer=True)
    elsewhere = handed_back(7, computer=True, handed_back_from=str(uuid4()))
    # Only a hand back is read so: nothing a message carries takes its turn away.
    message = SimpleNamespace(id=8, type=EventType.USER_MESSAGE.value, data={"content": "Go on.", "computer": True})
    events = [stopped, nothing_stopped, elsewhere, message]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [5, 8]


def said(id_: int, text: str = "Open the report.") -> SimpleNamespace:
    return SimpleNamespace(id=id_, type=EventType.USER_MESSAGE.value, data={"content": text})


def answered(id_: int, *, calls: bool = False) -> SimpleNamespace:
    """The model's response: its answer, which ends a turn, or its *calls* for tools, which does not."""
    message = {"role": "assistant", "content": "" if calls else "Done."}
    if calls:
        message["tool_calls"] = [{"id": "call-1", "type": "function", "function": {"name": "browser_click", "arguments": "{}"}}]
    return SimpleNamespace(id=id_, type=EventType.LLM_RESPONSE.value, data={"message": message})


def ended(id_: int) -> SimpleNamespace:
    return SimpleNamespace(id=id_, type=EventType.SESSION_COMPLETE.value, data={"reason": "tool_loop_halt"})


def asked(id_: int) -> SimpleNamespace:
    """A request to the model, which reads the hand backs written before it."""
    return SimpleNamespace(id=id_, type=EventType.LLM_REQUEST.value, data={})


@pytest.mark.parametrize(("log", "the_hand_backs"), [
    # The user's message was answered, then the browser handed back: the turn is the hand back's.
    ([said(1), answered(2), handed_back(3, **STOPPED)], True),
    # A turn that ended with no answer of the model's ended all the same.
    ([said(1), answered(2, calls=True), ended(3), handed_back(4, **STOPPED)], True),
    # The hand back's turn was begun and cut off: it is still the hand back's.
    ([said(1), answered(2), handed_back(3, **STOPPED), asked(4), answered(5, calls=True)], True),
    # Handed back as the turn before was ending, between its answer and its end: that end is not the hand back's.
    ([said(1), answered(2), handed_back(3, **STOPPED), ended(4)], True),
    # The hand back's own turn ran and ended with no answer of the model's.
    ([said(1), answered(2), handed_back(3, **STOPPED), asked(4), answered(5, calls=True), ended(6)], False),
    # The message has no answer yet, or its turn is still under way: the turn is the message's.
    ([said(1), handed_back(2, **STOPPED)], False),
    ([said(1), answered(2, calls=True), handed_back(3, **STOPPED)], False),
    # The user typed since the hand back.
    ([said(1), answered(2), handed_back(3, **STOPPED), said(4, "/compress")], False),
    # The hand back was read and answered: a later wake is not its turn.
    ([said(1), answered(2), handed_back(3, **STOPPED), asked(4), answered(5)], False),
    # Handed back while a command's own wake was answering it: read by no request, it is the next turn's.
    ([said(1, "/goal status"), handed_back(2, **STOPPED), answered(3)], True),
    # Handed back while the message's turn was under way, and read in it at its next request.
    ([said(1), asked(2), answered(3, calls=True), handed_back(4, **STOPPED), asked(5), answered(6)], False),
    # A hand back that stopped nothing, one made from another chat, and the cloud's give no turn of their own.
    ([said(1), answered(2), handed_back(3, computer=True)], False),
    ([said(1), answered(2), handed_back(3, computer=True, handed_back_from="another")], False),
    ([said(1), answered(2), handed_back(3)], False),
    ([], False),
], ids=[
    "answered-then-handed-back", "ended-then-handed-back", "its-turn-cut-off", "as-the-turn-before-ended",
    "its-turn-ended-unanswered", "message-unanswered",
    "message-under-way", "typed-since", "hand-back-answered", "during-a-commands-wake", "read-in-the-messages-turn",
    "nothing-stopped", "another-chats", "the-clouds", "empty",
])
def test_a_turn_is_a_hand_backs_once_the_users_last_message_was_answered_and_until_it_is(log, the_hand_backs):
    assert _turn_for_a_hand_back(log) is the_hand_backs


async def _wake(monkeypatch, *since_the_turn: EventType, **told: str) -> tuple[int, list[EventType]]:
    """Wake a local-folder chat whose last turn ended with an answer, *since_the_turn* landing after it,
    each saying *told* besides: how many turns the wake ran, and what it wrote to the chat's log."""
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    session = _session()
    session.config.update({"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/u/project"})
    events = [
        SimpleNamespace(id=1, type=EventType.USER_MESSAGE.value, data={"content": "Open the report."}),
        SimpleNamespace(id=2, type=EventType.LLM_RESPONSE.value, data={"message": {"role": "assistant", "content": "It is open."}}),
        *[
            SimpleNamespace(id=3 + n, type=kind.value, data={"session_id": str(session.id), "computer": True, **told})
            for n, kind in enumerate(since_the_turn)
        ],
    ]
    store = _stub_store(session, events)
    store.get_harness_cursor = AsyncMock(return_value=2)
    harness = _harness(store, _permissive())
    # The helper's compressor is a spec mock: left alone it hands the turn a mock instead of the messages.
    harness._compressor.prune_stale_browser_states = lambda messages: messages
    harness._run_loop = AsyncMock()
    await asyncio.wait_for(harness.wake(session.id), 5.0)
    return harness._run_loop.await_count, [call.args[1] for call in store.emit_event.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("told", FOR_THE_PANE, ids=lambda kind: kind.value)
async def test_a_wake_that_finds_only_a_take_over_or_the_browsers_opening_or_closing_runs_no_turn(monkeypatch, told):
    # Its user took the browser over to stop the agent: a wake for it gives the agent no turn, and writes nothing.
    assert await _wake(monkeypatch, told) == (0, [])


@pytest.mark.asyncio
async def test_a_wake_at_a_hand_back_runs_the_turn_of_an_agent_the_take_over_had_stopped(monkeypatch):
    turns, wrote = await _wake(
        monkeypatch, EventType.BROWSER_CONTROL_GRANTED, EventType.BROWSER_CONTROL_RETURNED, resumes=True,
    )
    assert turns == 1
    assert wrote == [EventType.HARNESS_WAKE]


@pytest.mark.asyncio
@pytest.mark.parametrize("told", [{}, {"handed_back_from": str(uuid4())}], ids=["nothing-stopped", "another-chats"])
async def test_a_wake_that_finds_only_a_hand_back_for_the_pane_runs_no_turn(monkeypatch, told):
    assert await _wake(monkeypatch, EventType.BROWSER_CONTROL_GRANTED, EventType.BROWSER_CONTROL_RETURNED, **told) == (0, [])
