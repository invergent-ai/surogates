"""Which events left past the cursor give a wake work to do."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.loop_context_replay import news, unread_reports
from surogates.harness.loop_pending import _actionable_pending_events, _hand_back_unread, _turn_for_a_hand_back
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


def resumed(id_: int, source: str = "browser_hand_back") -> SimpleNamespace:
    """The resume the control route writes when a hand back its user confirmed gives the chat's agent a turn."""
    return SimpleNamespace(id=id_, type=EventType.SESSION_RESUME.value, data={"source": source})


def test_the_clouds_hand_back_still_gives_a_wake_work():
    # As it was: a release of the cloud's browser is the session's wake.
    events = [event(5, EventType.BROWSER_CONTROL_GRANTED), handed_back(6)]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [6]


def test_a_computers_hand_back_is_told_for_the_pane_and_the_turn_it_gives_is_its_resume():
    # However it was made, the hand back's own event is for the pane, as the take-over was.
    gave_a_turn = handed_back(5, computer=True, resumes=True)
    gave_none = handed_back(6, computer=True)
    elsewhere = handed_back(7, computer=True, handed_back_from=str(uuid4()))
    # The turn a confirmed hand back gives is the resume written with it.
    resume = resumed(8)
    # Only a hand back is read so: nothing a message carries takes its turn away.
    message = SimpleNamespace(id=9, type=EventType.USER_MESSAGE.value, data={"content": "Go on.", "computer": True})
    events = [gave_a_turn, gave_none, elsewhere, resume, message]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [8, 9]


def taken_over(id_: int, *, computer: bool = True) -> SimpleNamespace:
    """A take-over as the control route writes it: a computer's says it is one, the cloud's does not."""
    return SimpleNamespace(
        id=id_, type=EventType.BROWSER_CONTROL_GRANTED.value,
        data={"owner_user_id": "user-1", **({"computer": True} if computer else {})},
    )


def test_a_hand_backs_resume_is_no_work_once_its_user_took_the_browser_over_again_before_it_was_read():
    def work(*log: SimpleNamespace) -> list[int]:
        return [e.id for e in _actionable_pending_events(list(log), cursor=4)]

    assert work(resumed(5)) == [5]
    assert work(resumed(5), taken_over(6)) == []
    # Each hand back's own: one made after the take-over is work.
    assert work(resumed(5), taken_over(6), resumed(7)) == [7]
    # Read by a request first, it was the work of that turn, which a wake may have to run again.
    assert work(resumed(5), asked(6), taken_over(7)) == [5, 6]
    # Only a take-over of the computer's browser, and only a hand back's resume.
    assert work(resumed(5), taken_over(6, computer=False)) == [5]
    assert work(resumed(5, "user_retry"), taken_over(6)) == [5]


def test_a_take_over_takes_only_the_hand_back_from_the_news_that_waits_for_the_next_request():
    report = SimpleNamespace(id=1, type=EventType.WORKER_COMPLETE.value, data={"worker_id": "w-1", "result": "Drafted the memo."})
    # A worker's report is true whoever holds the browser, and one that came after the take-over as well.
    assert unread_reports([report, resumed(2)]) == [news(report), news(resumed(2))]
    assert unread_reports([report, resumed(2), taken_over(3)]) == [news(report)]
    assert unread_reports([resumed(1), taken_over(2), report, resumed(4)]) == [news(report), news(resumed(4))]
    # The cloud's browser taken over is no word of the computer's.
    assert unread_reports([resumed(1), taken_over(2, computer=False)]) == [news(resumed(1))]


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


def woken(id_: int) -> SimpleNamespace:
    """A wake that took the session's work: the user's last message, if one waited."""
    return SimpleNamespace(id=id_, type=EventType.HARNESS_WAKE.value, data={"worker_id": "worker-1"})


@pytest.mark.parametrize(("log", "unread"), [
    ([resumed(1)], True),
    ([resumed(1), asked(2)], False),
    ([resumed(1), taken_over(2)], False),
    ([resumed(1), taken_over(2), resumed(3)], True),
    ([resumed(1), taken_over(2, computer=False)], True),
    ([resumed(1, "user_retry")], False),
], ids=["given", "read", "taken-over-again", "handed-back-anew", "the-clouds-taken-over", "another-resume"])
def test_a_hand_back_is_unread_until_a_request_reads_it_or_its_user_takes_the_browser_over_again(log, unread):
    assert _hand_back_unread(log) is unread


@pytest.mark.parametrize(("log", "the_hand_backs"), [
    # The user's message was answered, then the browser handed back: the turn is the hand back's.
    ([said(1), answered(2), resumed(3)], True),
    # A turn that ended with no answer of the model's ended all the same.
    ([said(1), answered(2, calls=True), ended(3), resumed(4)], True),
    # The hand back's turn was begun and cut off: it is still the hand back's.
    ([said(1), answered(2), resumed(3), woken(4), asked(5), answered(6, calls=True)], True),
    # Handed back as the turn before was ending, between its answer and its end: that end is not the hand back's.
    ([said(1), answered(2), resumed(3), ended(4)], True),
    # The hand back's own turn ran and ended with no answer of the model's.
    ([said(1), answered(2), resumed(3), asked(4), answered(5, calls=True), ended(6)], False),
    # The message has not been taken by a wake yet: the turn is the message's, and its command runs.
    ([said(1, "/compress"), resumed(2)], False),
    # Its turn is under way, the model asked: the turn is the message's.
    ([said(1), woken(2), asked(3), answered(4, calls=True), resumed(5)], False),
    # The user typed since the hand back.
    ([said(1), answered(2), resumed(3), said(4, "/compress")], False),
    # The hand back was read and answered: a later wake is not its turn.
    ([said(1), answered(2), resumed(3), asked(4), answered(5)], False),
    # Handed back while a command's own wake was answering it: read by no request, it is the next turn's.
    ([said(1, "/goal status"), woken(2), resumed(3), answered(4)], True),
    # A command a wake took and answered in a way of its own, with no answer of the model's in the log:
    # the wake made no request of the model, so the command is done with, and the turn is the hand back's.
    ([said(1, "/code fix the totals"), woken(2), resumed(3)], True),
    # Handed back while the message's turn was under way, and read in it at its next request.
    ([said(1), woken(2), asked(3), answered(4, calls=True), resumed(5), asked(6), answered(7)], False),
    # A command answered, then a new message no wake has taken yet: what took the command took none of
    # this one, which keeps its turn.
    ([said(1, "/goal status"), woken(2), answered(3), said(4, "/compress"), resumed(5)], False),
    # A turn the model answered, then a command its own wake answers with no request: the request was the
    # turn before's, and this command is done with.
    ([said(1), woken(2), asked(3), answered(4), said(5, "/code fix the totals"), woken(6), resumed(7)], True),
    # Its user took the browser over again before the hand back's turn came: that turn is nobody's, and
    # the log reads as it did before the hand back.
    ([said(1), answered(2), resumed(3), taken_over(4)], False),
    ([said(1, "/compress"), resumed(2), taken_over(3)], False),
    # And handed it back anew: the turn is that hand back's.
    ([said(1), answered(2), resumed(3), taken_over(4), resumed(5)], True),
    # Taken over again once the hand back's turn had begun, and been cut off: the turn is still its own.
    ([said(1), answered(2), resumed(3), woken(4), asked(5), answered(6, calls=True), taken_over(7)], True),
    # The cloud's browser taken over takes no turn away.
    ([said(1), answered(2), resumed(3), taken_over(4, computer=False)], True),
    # A hand back's own event, a resume for any other reason, and the cloud's hand back give no such turn.
    ([said(1), answered(2), handed_back(3, computer=True, resumes=True)], False),
    ([said(1), answered(2), resumed(3, "user_retry")], False),
    ([said(1), answered(2), handed_back(3)], False),
    ([], False),
], ids=[
    "answered-then-handed-back", "ended-then-handed-back", "its-turn-cut-off", "as-the-turn-before-ended",
    "its-turn-ended-unanswered", "message-not-taken", "message-under-way", "typed-since", "hand-back-answered",
    "during-a-commands-wake", "a-command-answered-its-own-way", "read-in-the-messages-turn",
    "a-new-message-after-a-command", "a-command-after-an-answered-turn",
    "taken-over-again", "taken-over-again-over-a-message", "handed-back-anew", "taken-over-again-in-its-turn",
    "the-clouds-taken-over",
    "the-hand-backs-own-event", "another-resume", "the-clouds", "empty",
])
def test_a_turn_is_a_hand_backs_once_the_users_last_message_is_done_with_and_until_it_is_answered(log, the_hand_backs):
    assert _turn_for_a_hand_back(log) is the_hand_backs


async def _wake(monkeypatch, *since_the_turn: SimpleNamespace) -> tuple[int, list[EventType]]:
    """Wake a local-folder chat whose last turn ended with an answer, *since_the_turn* landing after it:
    how many turns the wake ran, and what it wrote to the chat's log."""
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    session = _session()
    session.config.update({"execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/u/project"})
    events = [
        SimpleNamespace(id=1, type=EventType.USER_MESSAGE.value, data={"content": "Open the report."}),
        SimpleNamespace(id=2, type=EventType.LLM_RESPONSE.value, data={"message": {"role": "assistant", "content": "It is open."}}),
        *since_the_turn,
    ]
    store = _stub_store(session, events)
    store.get_harness_cursor = AsyncMock(return_value=2)
    harness = _harness(store, _permissive())
    # The helper's compressor is a spec mock: left alone it hands the turn a mock instead of the messages.
    harness._compressor.prune_stale_browser_states = lambda messages: messages
    harness._run_loop = AsyncMock()
    await asyncio.wait_for(harness.wake(session.id), 5.0)
    return harness._run_loop.await_count, [call.args[1] for call in store.emit_event.call_args_list]


def told(id_: int, kind: EventType, **said) -> SimpleNamespace:
    """What the chat's pane is told of its browser on the computer."""
    return SimpleNamespace(id=id_, type=kind.value, data={"session_id": "s-1", "computer": True, **said})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", FOR_THE_PANE, ids=lambda kind: kind.value)
async def test_a_wake_that_finds_only_a_take_over_or_the_browsers_opening_or_closing_runs_no_turn(monkeypatch, kind):
    # Its user took the browser over to stop the agent: a wake for it gives the agent no turn, and writes nothing.
    assert await _wake(monkeypatch, told(3, kind)) == (0, [])


@pytest.mark.asyncio
async def test_a_wake_at_a_hand_back_its_user_confirmed_runs_the_agents_turn(monkeypatch):
    taken_over = told(3, EventType.BROWSER_CONTROL_GRANTED)
    handed_back_ = told(4, EventType.BROWSER_CONTROL_RETURNED, resumes=True)
    turns, wrote = await _wake(monkeypatch, taken_over, handed_back_, resumed(5))
    assert turns == 1
    assert wrote == [EventType.HARNESS_WAKE]


@pytest.mark.asyncio
async def test_a_commands_wake_with_a_hand_back_unread_and_no_queue_to_reach_ends_as_any_commands_wake(monkeypatch):
    # A worker with no queue of its own, as a test's or a one-off run's: there is nowhere to queue the
    # hand back's turn, and the command is answered all the same.
    handed_back_ = told(4, EventType.BROWSER_CONTROL_RETURNED, resumes=True)
    refused = said(6, "/auto-research status")
    turns, wrote = await _wake(monkeypatch, told(3, EventType.BROWSER_CONTROL_GRANTED), handed_back_, resumed(5), refused)
    assert turns == 0
    assert wrote == [EventType.HARNESS_WAKE, EventType.LLM_RESPONSE]


@pytest.mark.asyncio
async def test_a_wake_at_a_hand_back_whose_user_took_the_browser_over_again_runs_no_turn(monkeypatch):
    taken_over_ = told(3, EventType.BROWSER_CONTROL_GRANTED)
    handed_back_ = told(4, EventType.BROWSER_CONTROL_RETURNED, resumes=True)
    again = told(6, EventType.BROWSER_CONTROL_GRANTED)
    assert await _wake(monkeypatch, taken_over_, handed_back_, resumed(5), again) == (0, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("said_too", [{}, {"resumes": True}, {"handed_back_from": str(uuid4())}], ids=[
    "no-hand-back", "its-resume-never-written", "another-chats",
])
async def test_a_wake_that_finds_only_a_hand_backs_own_event_runs_no_turn(monkeypatch, said_too):
    taken_over = told(3, EventType.BROWSER_CONTROL_GRANTED)
    assert await _wake(monkeypatch, taken_over, told(4, EventType.BROWSER_CONTROL_RETURNED, **said_too)) == (0, [])
