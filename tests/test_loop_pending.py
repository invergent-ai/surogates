"""Which events left past the cursor give a wake work to do, and when a command has its answer."""

from types import SimpleNamespace

import pytest

from surogates.harness.loop_pending import _actionable_pending_events, _command_answered, _taken_up
from surogates.session.events import EventType


def event(id_: int, type_: EventType) -> SimpleNamespace:
    return SimpleNamespace(id=id_, type=type_)


def log(*types: EventType) -> list[SimpleNamespace]:
    """A session's log of *types*, their ids counting from 1."""
    return [event(id_, type_) for id_, type_ in enumerate(types, start=1)]


def test_device_wait_events_give_a_wake_no_work():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.DEVICE_RESUMED)]
    assert _actionable_pending_events(events, cursor=4) == []


def test_a_user_message_still_does():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.USER_MESSAGE)]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [6]


def test_a_message_past_the_cursor_is_taken_up_by_no_wake_yet():
    # Its wake began and died: the cursor is what says a wake has done with a message.
    events = log(EventType.USER_MESSAGE, EventType.HARNESS_WAKE, EventType.CONTEXT_COMPACT)
    assert _taken_up(events, typed_at=1, cursor=0) is False


@pytest.mark.parametrize("since", [EventType.HARNESS_WAKE, EventType.LLM_REQUEST], ids=["a wake began", "the model was asked"])
def test_a_message_at_or_behind_the_cursor_is_taken_up_once_a_wake_or_a_request_followed_it(since):
    events = log(EventType.USER_MESSAGE, since, EventType.LLM_RESPONSE)
    assert [_taken_up(events, typed_at=1, cursor=cursor) for cursor in (1, 3)] == [True, True]
    assert _taken_up([event(e.id, e.type.value) for e in events], typed_at=1, cursor=3) is True


def test_a_message_a_failed_turn_moved_the_cursor_past_is_not_taken_up():
    # The turn was refused before any wake read the message: what it asked for is still to do.
    events = log(EventType.HARNESS_WAKE, EventType.LLM_REQUEST, EventType.USER_MESSAGE, EventType.SESSION_FAIL, EventType.SESSION_RESUME)
    assert _taken_up(events, typed_at=3, cursor=4) is False


@pytest.mark.parametrize("answer", [EventType.LLM_RESPONSE, EventType.CODE_RUN_RESULT])
def test_a_command_is_answered_once_the_wake_that_took_it_up_has_written_its_answer(answer):
    events = log(EventType.USER_MESSAGE, EventType.HARNESS_WAKE, EventType.CODE_RUN_STARTED, answer)
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
