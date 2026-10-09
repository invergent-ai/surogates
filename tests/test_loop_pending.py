"""Which events left past the cursor give a wake work to do, and when a command has its answer."""

from types import SimpleNamespace

import pytest

from surogates.harness.loop_pending import _actionable_pending_events, _command_answered
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
