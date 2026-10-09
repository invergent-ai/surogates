"""Which events left past the cursor give a wake work to do, and when a command has its answer."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import surogates.harness.loop as loop_module
from surogates.harness.loop_context_replay import ContextReplayMixin, news, unread_reports
from surogates.harness.loop_pending import (
    _actionable_pending_events, _command_answered, _cut_off_at, _first_unread, _hand_back_unread, _in_typed_order,
    _left_behind, _read_as_words, _redo_unread, _shown_before_its_answer, _turn_for_a_hand_back, _turn_for_a_redo,
)
from surogates.session.events import EventType
from tests.test_wake_slash_command_gate import _harness, _permissive, _session, _stub_store


def event(id_: int, type_: EventType, **data) -> SimpleNamespace:
    return SimpleNamespace(id=id_, type=type_, data=data)


def log(*types: EventType) -> list[SimpleNamespace]:
    """A session's log of *types*, their ids counting from 1."""
    return [event(id_, type_) for id_, type_ in enumerate(types, start=1)]


def test_device_wait_events_give_a_wake_no_work():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.DEVICE_RESUMED)]
    assert _actionable_pending_events(events, cursor=4) == []


def test_a_user_message_still_does():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.USER_MESSAGE)]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [6]


def test_a_command_is_answered_by_the_answer_that_names_its_message():
    events = [
        event(1, EventType.USER_MESSAGE), event(2, EventType.USER_MESSAGE), event(3, EventType.HARNESS_WAKE),
        event(4, EventType.LLM_RESPONSE, answers=1),
    ]
    # One wake read both commands and answered the first: the second is not answered by that.
    assert (_command_answered(events, typed_at=1), _command_answered(events, typed_at=2)) == (True, False)
    events.append(event(5, EventType.LLM_RESPONSE, answers=2))
    assert _command_answered(events, typed_at=2) is True


def test_a_coding_runs_result_answers_the_command_that_started_the_run():
    events = [
        event(1, EventType.USER_MESSAGE), event(2, EventType.USER_MESSAGE), event(3, EventType.HARNESS_WAKE),
        event(4, EventType.CODE_RUN_STARTED, run_id="run-1", source_event_id=1),
        event(5, EventType.CODE_RUN_RESULT, run_id="run-1"),
        # A run the model started with its tool answers no command.
        event(6, EventType.CODE_RUN_STARTED, run_id="run-2"),
        event(7, EventType.CODE_RUN_RESULT, run_id="run-2"),
    ]
    assert (_command_answered(events, typed_at=1), _command_answered(events, typed_at=2)) == (True, False)
    # A run begun and not finished is no answer.
    assert _command_answered(events[:4], typed_at=1) is False


def test_an_answer_written_before_answers_were_named_counts_for_the_message_it_follows():
    events = log(EventType.USER_MESSAGE, EventType.HARNESS_WAKE, EventType.CONTEXT_COMPACT, EventType.LLM_RESPONSE)
    assert _command_answered(events, typed_at=1) is True
    # As the store gives them: an event's type is its name.
    assert _command_answered([event(e.id, e.type.value) for e in events], typed_at=1) is True


@pytest.mark.parametrize("left", [
    (EventType.USER_MESSAGE,),
    (EventType.USER_MESSAGE, EventType.HARNESS_WAKE),
    # The worker died in the middle of the command: what it wrote is no answer.
    (EventType.USER_MESSAGE, EventType.HARNESS_WAKE, EventType.CONTEXT_COMPACT),
    (EventType.USER_MESSAGE, EventType.HARNESS_WAKE, EventType.CODE_RUN_STARTED, EventType.CODE_RUN_PROGRESS),
], ids=["not woken", "woken", "cleared and no word", "a run begun"])
def test_a_command_no_wake_has_answered_is_not(left):
    assert _command_answered(log(*left), typed_at=1) is False


def test_an_answer_with_no_wake_before_it_is_an_earlier_commands():
    # The second command was typed while the first was being answered.
    events = log(EventType.USER_MESSAGE, EventType.HARNESS_WAKE, EventType.USER_MESSAGE, EventType.LLM_RESPONSE)
    assert (_command_answered(events, typed_at=1), _command_answered(events, typed_at=3)) == (True, False)


def test_what_the_model_says_in_a_turn_of_its_own_is_no_commands_answer():
    # A message typed during the model's turn was read by the model, as its user's words.
    events = log(EventType.USER_MESSAGE, EventType.HARNESS_WAKE, EventType.LLM_REQUEST, EventType.LLM_RESPONSE)
    assert _command_answered(events, typed_at=1) is False
    # A command answered, and then a turn the model took for something else.
    events = log(
        EventType.USER_MESSAGE, EventType.HARNESS_WAKE, EventType.LLM_RESPONSE,
        EventType.HARNESS_WAKE, EventType.LLM_REQUEST, EventType.LLM_RESPONSE,
    )
    assert _command_answered(events, typed_at=1) is True


def test_what_was_said_before_a_command_does_not_answer_it():
    events = log(EventType.HARNESS_WAKE, EventType.LLM_RESPONSE, EventType.USER_MESSAGE, EventType.HARNESS_WAKE)
    assert _command_answered(events, typed_at=3) is False
    # Nor does the wake before it make a later word its answer.
    events = log(EventType.HARNESS_WAKE, EventType.USER_MESSAGE, EventType.LLM_RESPONSE)
    assert _command_answered(events, typed_at=2) is False


def test_a_command_is_left_behind_once_the_turns_end_after_it_is_older_than_the_window():
    now, hour = datetime(2026, 10, 9, 12, tzinfo=timezone.utc), timedelta(hours=1)

    def ended(ago: timedelta, zone=timezone.utc) -> list[SimpleNamespace]:
        end = SimpleNamespace(id=2, type=EventType.SESSION_COMPLETE.value, data={}, created_at=(now - ago).replace(tzinfo=zone))
        return [event(1, EventType.USER_MESSAGE), end]

    assert _left_behind(ended(hour + timedelta(seconds=1)), 1, now=now, window=hour) is True
    # The sweeper's own bound for a crash: at the hour itself it is not yet old.
    assert _left_behind(ended(hour), 1, now=now, window=hour) is False
    assert _left_behind(ended(timedelta(minutes=59)), 1, now=now, window=hour) is False
    # A time the store gives without its zone is UTC.
    assert _left_behind(ended(timedelta(hours=2), zone=None), 1, now=now, window=hour) is True
    # A turn's end before the command, however old, says nothing of it; nor does any other event after it.
    assert _left_behind(ended(timedelta(days=40)), 2, now=now, window=hour) is False
    old = SimpleNamespace(id=2, type=EventType.LLM_RESPONSE.value, data={}, created_at=now - timedelta(days=40))
    assert _left_behind([event(1, EventType.USER_MESSAGE), old], 1, now=now, window=hour) is False


def test_what_the_harness_wrote_for_a_command_is_replayed_right_after_the_command():
    events = [
        event(1, EventType.USER_MESSAGE), event(2, EventType.USER_MESSAGE), event(3, EventType.USER_MESSAGE),
        event(4, EventType.HARNESS_WAKE), event(5, EventType.CONTEXT_COMPACT, answers=1), event(6, EventType.LLM_RESPONSE, answers=1),
        event(7, EventType.LLM_RESPONSE, answers=3), event(8, EventType.LLM_RESPONSE, answers=99), event(9, EventType.LLM_RESPONSE),
    ]
    # Each with its own command, in the order written; one whose command is not here, and the
    # model's own words, stay where they are.
    assert [e.id for e in _in_typed_order(events)] == [1, 5, 6, 2, 3, 7, 4, 8, 9]
    assert _in_typed_order(events[:4]) == events[:4]


def test_what_was_written_for_a_command_that_waited_for_a_turn_is_replayed_after_that_turn():
    events = [
        event(1, EventType.LLM_REQUEST), event(2, EventType.LLM_RESPONSE), event(3, EventType.TOOL_CALL),
        event(4, EventType.USER_MESSAGE), event(5, EventType.TOOL_RESULT), event(6, EventType.LLM_REQUEST),
        event(7, EventType.LLM_RESPONSE), event(8, EventType.SESSION_COMPLETE), event(9, EventType.HARNESS_WAKE),
        event(10, EventType.CONTEXT_COMPACT, answers=4), event(11, EventType.LLM_RESPONSE, answers=4),
    ]
    # Typed between a call and its result: the block stands after the turn's last answer, not inside the turn.
    assert [e.id for e in _in_typed_order(events)] == [1, 2, 3, 5, 6, 7, 4, 10, 11, 8, 9]


def test_what_its_user_typed_behind_a_command_and_the_turn_did_not_read_is_replayed_after_the_commands_block():
    events = [
        event(1, EventType.LLM_REQUEST), event(2, EventType.USER_MESSAGE), event(3, EventType.USER_MESSAGE),
        event(4, EventType.USER_MESSAGE, synthetic="outcome_continuation"), event(5, EventType.LLM_RESPONSE),
        event(6, EventType.USER_MESSAGE), event(7, EventType.CONTEXT_COMPACT, answers=2),
        event(8, EventType.LLM_RESPONSE, answers=2),
    ]
    # 3 was typed behind the command while the turn's last answer was written: no request read it.
    assert [e.id for e in _in_typed_order(events)] == [1, 4, 5, 2, 7, 8, 3, 6]
    # Read by a request of the turn, it is the turn's, and stays in it.
    read = [*events[:3], event(3.5, EventType.LLM_REQUEST), *events[3:]]
    assert [e.id for e in _in_typed_order(read)] == [1, 3, 3.5, 4, 5, 2, 7, 8, 6]


def test_two_commands_that_waited_for_one_turn_are_replayed_after_it_in_the_order_typed():
    events = [
        event(1, EventType.LLM_REQUEST), event(2, EventType.USER_MESSAGE), event(3, EventType.USER_MESSAGE),
        event(4, EventType.USER_MESSAGE), event(5, EventType.LLM_RESPONSE), event(6, EventType.LLM_RESPONSE, answers=2),
        event(7, EventType.LLM_RESPONSE, answers=4),
    ]
    assert [e.id for e in _in_typed_order(events)] == [1, 5, 2, 6, 3, 4, 7]


def test_a_command_is_never_replayed_before_a_compaction_no_command_asked_for_that_was_written_before_its_own():
    events = [
        event(1, EventType.USER_MESSAGE), event(2, EventType.HARNESS_WAKE), event(3, EventType.CONTEXT_COMPACT),
        event(4, EventType.CONTEXT_COMPACT, answers=1), event(5, EventType.LLM_RESPONSE, answers=1),
    ]
    # Before it, the wake's own compaction would put back what the command cleared.
    assert [e.id for e in _in_typed_order(events)] == [2, 3, 1, 4, 5]


def test_the_answer_to_a_clear_is_left_out_of_what_the_model_is_shown():
    events = [
        event(1, EventType.USER_MESSAGE), event(2, EventType.CONTEXT_COMPACT, answers=1, strategy="clear"),
        event(3, EventType.LLM_RESPONSE, answers=1), event(4, EventType.USER_MESSAGE),
        event(5, EventType.CONTEXT_COMPACT, answers=4, strategy="summary"), event(6, EventType.LLM_RESPONSE, answers=4),
    ]
    # A cleared conversation starts on what its user says next; a compressed one keeps its answer.
    assert [e.id for e in _in_typed_order(events)] == [1, 2, 4, 5, 6]


def test_the_conversation_a_command_acts_on_ends_where_its_answer_will_stand():
    typed_in_a_turn = [
        event(1, EventType.USER_MESSAGE), event(2, EventType.LLM_REQUEST), event(3, EventType.USER_MESSAGE),
        event(4, EventType.USER_MESSAGE), event(5, EventType.LLM_RESPONSE), event(6, EventType.HARNESS_WAKE),
    ]
    assert [e.id for e in _shown_before_its_answer(typed_in_a_turn, 3)] == [1, 2, 5, 3]
    typed_before_any = [event(1, EventType.USER_MESSAGE), event(2, EventType.USER_MESSAGE), event(3, EventType.HARNESS_WAKE)]
    assert [e.id for e in _shown_before_its_answer(typed_before_any, 1)] == [1]


def test_a_commands_answer_after_a_turn_that_was_stopped_in_a_call_is_replayed_behind_its_command():
    call = {"id": "call_1", "type": "function", "function": {"name": "todo", "arguments": "{}"}}
    events = [
        event(1, EventType.USER_MESSAGE, content="Go on."), event(2, EventType.LLM_REQUEST),
        event(3, EventType.LLM_RESPONSE, message={"role": "assistant", "content": "", "tool_calls": [call]}),
        event(4, EventType.TOOL_CALL, tool_call_id="call_1"), event(5, EventType.SESSION_PAUSE),
        event(6, EventType.USER_MESSAGE, content="/goal status"),
        event(7, EventType.LLM_RESPONSE, answers=6, message={"role": "assistant", "content": "No active outcome."}),
    ]
    for each in events:
        each.type = each.type.value
    replayed = ContextReplayMixin._rebuild_messages(SimpleNamespace(), events)
    # The turn's call never got its result: the command is not held back for one, behind its own answer.
    assert [(m["role"], m["content"]) for m in replayed[-2:]] == [("user", "/goal status"), ("assistant", "No active outcome.")]


def test_where_a_turn_was_cut_off():
    asked = event(2, EventType.LLM_REQUEST)
    calls = event(3, EventType.LLM_RESPONSE, message={"tool_calls": [{"id": "call_1"}]})
    said = event(3, EventType.LLM_RESPONSE, message={"content": "Done."})
    assert _cut_off_at([event(1, EventType.USER_MESSAGE), asked]) == 2
    assert _cut_off_at([asked, calls, event(4, EventType.TOOL_RESULT)]) == 3
    assert _cut_off_at([asked, said]) is None
    # A command's answer is no word of the model's; a turn's end, a stop and a clear close the turn.
    assert _cut_off_at([asked, event(3, EventType.LLM_RESPONSE, answers=1, message={"content": "No active outcome."})]) == 2
    for end in (EventType.SESSION_COMPLETE, EventType.SESSION_FAIL, EventType.SESSION_PAUSE, EventType.SESSION_STOPPED):
        assert _cut_off_at([asked, event(3, end)]) is None
    assert _cut_off_at([asked, event(3, EventType.CONTEXT_COMPACT, strategy="clear")]) is None
    assert _cut_off_at([asked, event(3, EventType.CONTEXT_COMPACT, strategy="summary")]) == 2


def test_an_answer_with_no_name_answers_nothing_under_a_wake_of_this_harness():
    old = [event(1, EventType.USER_MESSAGE), event(2, EventType.HARNESS_WAKE), event(3, EventType.LLM_RESPONSE)]
    new = [event(1, EventType.USER_MESSAGE), event(2, EventType.HARNESS_WAKE, names_answers=True), event(3, EventType.LLM_RESPONSE)]
    assert (_command_answered(old, typed_at=1), _command_answered(new, typed_at=1)) == (True, False)
    # The wake that counts is the one the word was written under.
    mixed = [*new[:2], event(3, EventType.HARNESS_WAKE), event(4, EventType.LLM_RESPONSE)]
    assert _command_answered(mixed, typed_at=1) is True
    assert _command_answered([*old[:2], *[event(e.id + 1, e.type, **e.data) for e in new[1:]]], typed_at=1) is False


def test_a_command_is_read_as_words_when_an_older_harness_asked_the_model_after_it():
    wake, marked = event(1, EventType.HARNESS_WAKE), event(1, EventType.HARNESS_WAKE, names_answers=True)
    typed, asked = event(2, EventType.USER_MESSAGE), event(3, EventType.LLM_REQUEST)
    assert _read_as_words([wake, typed, asked], typed_at=2) is True
    # With no wake before it in the log, it is an older harness's too.
    assert _read_as_words([typed, asked], typed_at=2) is True
    # This harness never gives a command to the model; a request before the command read nothing of it.
    assert _read_as_words([marked, typed, asked], typed_at=2) is False
    assert _read_as_words([wake, event(2, EventType.LLM_REQUEST), event(3, EventType.USER_MESSAGE)], typed_at=3) is False
    # An older turn, then this harness's wake: the request under the new wake does not count.
    later = [wake, typed, event(3, EventType.HARNESS_WAKE, names_answers=True), event(4, EventType.LLM_REQUEST)]
    assert _read_as_words(later, typed_at=2) is False


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
        # The turn as a wake leaves it: the model was asked, so its user's message is one it has read.
        SimpleNamespace(id=0, type=EventType.USER_MESSAGE.value, data={"content": "Open the report."}),
        SimpleNamespace(id=1, type=EventType.LLM_REQUEST.value, data={}),
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


REQUEST, ANSWER, DONE, REDO, SAID = (
    EventType.LLM_REQUEST, EventType.LLM_RESPONSE, EventType.SESSION_COMPLETE, EventType.HISTORY_REDO, EventType.USER_MESSAGE,
)


@pytest.mark.parametrize("types, unread", [
    ((SAID, REQUEST, ANSWER, DONE, REDO), True),
    # A command's answer is no request of the model's.
    ((SAID, REQUEST, ANSWER, DONE, REDO, SAID, EventType.HARNESS_WAKE, ANSWER), True),
    ((SAID, REQUEST, ANSWER, DONE, REDO, REQUEST), False),
    ((SAID, REQUEST, ANSWER, DONE, REDO, REQUEST, ANSWER, DONE, REDO), True),
    ((SAID, REQUEST, ANSWER, DONE), False),
], ids=["told", "a command answered since", "read by a request", "told again", "never told"])
def test_a_redo_is_unread_until_a_request_of_the_models_comes_after_it(types, unread):
    assert _redo_unread(log(*types)) is unread


def test_a_commands_end_does_not_move_the_cursor_past_a_redo():
    events = log(SAID, REQUEST, ANSWER, DONE, REDO, SAID)
    nothing_plain = dict(goal_in_flight=False, is_plain_message=lambda _event: False)
    assert _first_unread(events, **nothing_plain) == 5
    assert _first_unread([*events, event(7, REQUEST)], **nothing_plain) is None


@pytest.mark.parametrize("after_the_redo, the_redos", [
    ((), True),
    # A command the harness answers opens no turn of the model's, answered or not.
    (("/loop 5m Go on.",), True),
    (("/loop 5m Go on.", EventType.HARNESS_WAKE, ANSWER), True),
    # A wake that died before it asked the model.
    ((EventType.SESSION_RESUME, EventType.HARNESS_WAKE), True),
    # Anything else its user says has the turn, and so has what the harness says for them.
    (("Go on.",), False),
    (("/report-writer Go on.",), False),
    (("/loop 5m Go on.", "Go on."), False),
    (("Go on.", "/loop 5m Go on."), False),
    ((REQUEST,), False),
    ((REQUEST, ANSWER, DONE, "/loop 5m Go on."), False),
], ids=[
    "the redo alone", "a command waiting", "a command answered", "a dead wake", "a message", "a skill",
    "a message behind a command", "a command behind a message", "read", "read, and a command since",
])
def test_the_turn_after_a_redo_is_the_redos_unless_something_else_opened_it(after_the_redo, the_redos):
    events = log(SAID, REQUEST, ANSWER, DONE, REDO)
    for said in after_the_redo:
        events.append(
            event(len(events) + 1, said) if isinstance(said, EventType) else event(len(events) + 1, SAID, content=said)
        )
    is_command = lambda e: e.type == SAID and (e.data.get("content") or "").startswith("/loop")  # noqa: E731
    assert _turn_for_a_redo(events, is_command=is_command) is the_redos


def test_a_turn_no_redo_opened_is_not_the_redos():
    assert _turn_for_a_redo(log(SAID, REQUEST, ANSWER, DONE), is_command=lambda _event: True) is False
