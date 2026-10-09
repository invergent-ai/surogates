"""Which events left past the cursor give a wake work to do, and when a command has its answer."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from surogates.harness.loop_context_replay import ContextReplayMixin
from surogates.harness.loop_pending import (
    _actionable_pending_events, _command_answered, _cut_off_at, _in_typed_order, _left_behind, _read_as_words,
    _shown_before_its_answer,
)
from surogates.session.events import EventType


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
    assert [e.id for e in _in_typed_order(events)] == [1, 5, 6, 2, 3, 7, 4, 9, 8]
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
