"""Pending-event and slash-command idempotency helpers for the harness loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from surogates.devices.browser import for_the_pane_alone, resumes_the_agent, takes_the_browser_over
from surogates.session.events import EventType

_HARNESS_CONTROL_PENDING_EVENT_TYPES = frozenset({
    EventType.HARNESS_RECOVERED.value,
    EventType.HARNESS_WAKE.value,
    # These tell viewers about a wait on the user's computer and give the harness nothing to do.
    EventType.DEVICE_WAITING.value,
    EventType.DEVICE_RESUMED.value,
    # These tell a chat's browser pane that its browser opened, closed or is not there, and that its
    # user took it over, which is to stop the agent.  The hand back is not among them: the cloud's
    # wakes the agent.  A computer's is for the pane too (for_the_pane_alone): the turn a hand back
    # its user confirmed gives is the resume written with it.
    EventType.BROWSER_PROVISIONED.value,
    EventType.BROWSER_DESTROYED.value,
    EventType.BROWSER_UNAVAILABLE.value,
    EventType.BROWSER_CONTROL_GRANTED.value,
})


def _event_type(event: Any) -> str:
    return event.type.value if isinstance(event.type, EventType) else str(event.type)


def _hand_backs_taken_over_again(events: list[Any]) -> set[int]:
    """The resumes a hand back of the browser gave a chat that are news no more: its user took the
    browser over again before any request of the model's had read them.

    The agent would read that the browser tools work again while its user
    holds the browser.  Such a resume is as though it had not been written:
    no work for a wake, no turn, and nothing to read.  The next hand back
    gives its own.
    """
    waiting: list[int] = []
    taken_again: set[int] = set()
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value:
            waiting = []
        elif resumes_the_agent(event):
            waiting.append(event.id)
        elif takes_the_browser_over(event):
            taken_again.update(waiting)
            waiting = []
    return taken_again


def _actionable_pending_events(events: list[Any], cursor: int) -> list[Any]:
    """Return post-cursor events that should start harness work."""
    taken_again = _hand_backs_taken_over_again(events)
    pending = []
    for event in events:
        if (
            event.id is not None
            and event.id > cursor
            and _event_type(event) not in _HARNESS_CONTROL_PENDING_EVENT_TYPES
            and not for_the_pane_alone(event)
            and event.id not in taken_again
        ):
            pending.append(event)
    return pending


def _hand_back_unread(events: list[Any]) -> bool:
    """Whether the resume a hand back of the browser gave the chat waits to be read: none of the
    model's requests came after it, and its user has not taken the browser over again since.

    The cursor cannot tell: one that lands while a turn, or a command's
    wake, is under way is behind the cursor once that moves.  Every model
    request reads the hand backs written before it, live and in replay
    alike (surogates.harness.loop_context_replay.unread_reports).
    """
    unread = False
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value or takes_the_browser_over(event):
            unread = False
        elif resumes_the_agent(event):
            unread = True
    return unread


def _turn_for_a_hand_back(events: list[Any]) -> bool:
    """Whether the turn a wake is about to run is one a hand back of the browser gives the agent.

    No message of the user's waits to be taken, and the resume a hand back
    gave has not been answered.  A wake reads the user's last message to
    run its command; in such a turn that message is not what the wake is
    for, and its command must not run again.

    A hand back opens a turn when it lands with none under way.  A message a
    wake took, with no request of the model's since, is a command the
    harness answered itself, however it answered: done with, so the hand
    back opens a turn then too.  A message no wake has taken yet keeps its
    turn, and its command runs once.  A hand back that lands in a turn the
    model is in opens the next, unless a request of that turn read it.  A
    turn the hand back opened and a dead worker cut off is still its own.
    """
    opened_by: str | None = None
    # Whether a wake took the user's message, and whether the model was asked since.
    taken = asked = False
    # A hand back no model request has read yet.
    unread = False
    taken_again = _hand_backs_taken_over_again(events)
    for event in events:
        if event.id in taken_again:
            continue
        kind = _event_type(event)
        ends_a_turn = kind == EventType.SESSION_COMPLETE.value or (
            # The model's answer ends a turn; its calls for tools do not.
            kind == EventType.LLM_RESPONSE.value
            and not ((getattr(event, "data", None) or {}).get("message") or {}).get("tool_calls")
        )
        if kind == EventType.USER_MESSAGE.value:
            opened_by, taken, asked = "message", False, False
        elif kind == EventType.HARNESS_WAKE.value:
            taken = True
        elif kind == EventType.LLM_REQUEST.value:
            unread, asked = False, True
        elif ends_a_turn:
            opened_by = "hand back" if unread else None
        elif resumes_the_agent(event):
            unread = True
            if opened_by is None or (opened_by == "message" and taken and not asked):
                opened_by = "hand back"
    return opened_by == "hand back"


def _redo_unread(events: list[Any]) -> bool:
    """Whether a project's thread was told to redo files and none of the model's requests came after:
    no turn has read it.

    Told like a hand back (``_hand_back_unread``): a command's end moves the
    cursor, and a redo is written as its turn ends, where a command typed
    during that turn still waits.
    """
    unread = False
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value:
            unread = False
        elif _event_type(event) == EventType.HISTORY_REDO.value:
            unread = True
    return unread


def _turn_for_a_redo(events: list[Any], *, is_command: Any) -> bool:
    """Whether the turn a wake is about to run, or to go on with, is the one a redo gives its thread.

    Nothing was said after the redo that opens a turn of its own, and the
    turn it opened has not ended.  A wake reads the user's last message to
    run its command; in such a turn that message is not what the wake is
    for, and its command must not run again.  A command the harness answers
    itself (*is_command* says which) opens no turn of the model's, typed
    before the redo or after it; any other message after the redo is the
    turn's, and its command runs once.  So is one typed as the turn before
    the redo landed, which stands before the redo with no request to have
    read it: its turn comes first, and reads the redo too.

    The turn is the redo's until it ends: at the model's answer that calls
    no tool, or at a turn's end, once the model was asked in it.  One a dead
    worker cut off, before its first request or after it, is still the
    redo's for the wake that goes on with it.
    """
    # Whether the redo's turn is open, whether the model was asked in it, and
    # whether a message that opens a turn of its own waits, read by no request.
    opened = asked = waits = False
    for event in events:
        kind = _event_type(event)
        data = getattr(event, "data", None) or {}
        if kind == EventType.LLM_REQUEST.value:
            asked, waits = opened, False
        elif kind == EventType.HISTORY_REDO.value:
            opened, asked = not waits, False
        elif kind == EventType.USER_MESSAGE.value and not is_command(event):
            opened, waits = False, True
        elif asked and (
            kind in _TURN_END_EVENT_TYPES
            # The model's answer ends a turn; its calls for tools do not, nor the harness's answer to a command.
            or kind == EventType.LLM_RESPONSE.value and "answers" not in data
            and not (data.get("message") or {}).get("tool_calls")
        ):
            opened = False
    return opened


#: The field a wake of this harness sets on its ``harness.wake``: under such
#: a wake every answer to a command names its message, and a command is
#: never given to the model as words.  A wake without it is an older
#: harness's.
NAMES_ANSWERS = "names_answers"


def _command_answered(events: list[Any], typed_at: int) -> bool:
    """Return True if the harness has answered the command the user typed at event *typed_at*.

    An answer names the message it answers: ``answers`` on the harness's
    ``llm.response``, and for a coding run the ``source_event_id`` of the
    ``code.run_started`` whose ``code.run_result`` is the answer.  So each
    command is answered by its own answer and no other's, however many
    commands one wake read.

    Nothing else says a command was answered.  Not the cursor: a tool's
    result, or a turn that was refused or failed, moves it past a message
    nobody read.  Not a model's request after it: a command is never the
    model's to read.

    Under a wake of an older harness, one without ``NAMES_ANSWERS``,
    answers carry no name.  There an answer counts for the message it
    follows when that wake began between the two and the model was not
    asked: it wrote ``harness.wake`` and then the answer, with no request
    between them.  Under a wake of this harness an answer with no name
    answers nothing.
    """
    runs: dict[Any, Any] = {}
    taken_up = False
    for event in events:
        if event.id is None or event.id <= typed_at:
            continue
        event_type = _event_type(event)
        data = getattr(event, "data", None) or {}
        if event_type == EventType.CODE_RUN_STARTED.value:
            runs[data.get("run_id")] = data.get("source_event_id")
        elif event_type == EventType.CODE_RUN_RESULT.value:
            if data.get("run_id") in runs and runs[data.get("run_id")] == typed_at:
                return True
        elif event_type == EventType.LLM_RESPONSE.value and "answers" in data:
            if data["answers"] == typed_at:
                return True
        elif event_type == EventType.HARNESS_WAKE.value:
            taken_up = not data.get(NAMES_ANSWERS)
        elif event_type == EventType.LLM_REQUEST.value:
            taken_up = False
        elif taken_up and event_type == EventType.LLM_RESPONSE.value:
            return True
    return False


def _read_as_words(events: list[Any], typed_at: int) -> bool:
    """Return True if an older harness gave the command typed at event *typed_at* to the model as words.

    The model was asked after the message under a wake without
    ``NAMES_ANSWERS``: that harness steered whatever its user typed into
    the turn under way.  Its user has had the model's words for the command
    since, and it is never run, however the chat stands.
    """
    older = True
    for event in events:
        event_type = _event_type(event)
        if event_type == EventType.HARNESS_WAKE.value:
            older = not (getattr(event, "data", None) or {}).get(NAMES_ANSWERS)
        elif event_type == EventType.LLM_REQUEST.value and older and event.id is not None and event.id > typed_at:
            return True
    return False


def _left_behind(events: list[Any], typed_at: int, *, now: datetime, window: timedelta) -> bool:
    """Return True if the command typed at event *typed_at* lies behind a turn's end older than *window*.

    A command typed during a turn waits for that turn's end, and its own
    wake follows at once.  One still unanswered long after is from a log an
    older harness left, whose user has had the model's words for it since:
    it is not run.
    """
    return any(
        event.id is not None
        and event.id > typed_at
        and _event_type(event) == EventType.SESSION_COMPLETE.value
        and _aware(event.created_at) < now - window
        for event in events
    )


def _aware(moment: datetime) -> datetime:
    """*moment* in UTC: the store gives some timestamps without their zone."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


_REPORT_EVENT_TYPES = frozenset({
    EventType.WORKER_COMPLETE.value,
    EventType.WORKER_FAILED.value,
})
#: The messages the harness writes to give a goal its next turn.
_GOAL_TURN_MESSAGES = frozenset({"outcome_kickoff", "outcome_continuation"})


def _first_unread(events: list[Any], *, goal_in_flight: bool, is_plain_message: Any) -> int | None:
    """Return the id of the first event in *events* that still waits for the model to read it.

    A worker's report; a redo a thread was told of; a message of the
    user's own that is no command (*is_plain_message* says which); and,
    while a goal is in flight, the message that gives the goal its next
    turn.  A request reads what was written before it, so the unread ones
    are those after the log's last ``llm.request``.
    """
    first: int | None = None
    for event in events:
        event_type = _event_type(event)
        if event_type == EventType.LLM_REQUEST.value:
            first = None
        elif first is None and (
            event_type in _REPORT_EVENT_TYPES
            or event_type == EventType.HISTORY_REDO.value
            or is_plain_message(event)
            or goal_in_flight and _gives_a_goal_its_turn(event)
        ):
            first = event.id
    return first


#: What ends a turn of the model's for good: after one of these the turn is not one to go on with.
_TURN_END_EVENT_TYPES = frozenset({
    EventType.SESSION_COMPLETE.value,
    EventType.SESSION_FAIL.value,
    EventType.SESSION_PAUSE.value,
    EventType.SESSION_STOPPED.value,
})


def _turn_cut_off(events: list[Any]) -> bool:
    """Return True if *events* end in a turn of the model's that its worker's death cut off."""
    return _cut_off_at(events) is not None


def _cut_off_at(events: list[Any]) -> int | None:
    """Return where the turn of the model's that *events* end in was cut off, as the id of its last
    request or answer; None when they end in no such turn.

    The model was asked and has not answered, or its last answer called
    tools: the turn is to be gone on with.  An answer of the harness's to a
    command is no word of the model's and leaves the turn as it was.
    """
    at: int | None = None
    for event in events:
        event_type = _event_type(event)
        if event_type == EventType.LLM_REQUEST.value:
            at = event.id
        elif event_type == EventType.LLM_RESPONSE.value:
            data = getattr(event, "data", None) or {}
            if "answers" not in data:
                at = event.id if (data.get("message") or {}).get("tool_calls") else None
        elif event_type in _TURN_END_EVENT_TYPES:
            at = None
        elif _clears(event):
            # The user cleared the conversation: the turn that was in it is not one to go on with.
            at = None
    return at


def _first_plain_message_unread(events: list[Any], *, is_plain_message: Any) -> int | None:
    """Return the id of the first message the user wrote themselves, and no command, that no model request came after."""
    first: int | None = None
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value:
            first = None
        elif first is None and is_plain_message(event):
            first = event.id
    return first


def _gives_a_goal_its_turn(event: Any) -> bool:
    return (
        _event_type(event) == EventType.USER_MESSAGE.value
        and (event.data or {}).get("synthetic") in _GOAL_TURN_MESSAGES
    )


def _goal_turn_waits(events: list[Any]) -> bool:
    """Return True if a goal's next turn is queued in *events* and no model request has read it."""
    waits = False
    for event in events:
        if _event_type(event) == EventType.LLM_REQUEST.value:
            waits = False
        elif _gives_a_goal_its_turn(event):
            waits = True
    return waits


def _plain_message_unread(events: list[Any], *, is_plain_message: Any) -> bool:
    """Return True if a message the user wrote themselves, and no command, is in *events* with no model request after it."""
    return _first_plain_message_unread(events, is_plain_message=is_plain_message) is not None


#: What a turn of the model's writes.  So does a compaction no command asked for: the conversation as it stood.
_TURN_EVENT_TYPES = frozenset({
    EventType.LLM_REQUEST.value,
    EventType.LLM_RESPONSE.value,
    EventType.TOOL_CALL.value,
    EventType.TOOL_RESULT.value,
    EventType.CONTEXT_COMPACT.value,
})


def _of_a_turn(event: Any) -> bool:
    """Whether *event* is a turn's own, and nothing the harness wrote for a command."""
    return _event_type(event) in _TURN_EVENT_TYPES and "answers" not in (getattr(event, "data", None) or {})


def _clears(event: Any) -> bool:
    """Whether *event* is the compaction a ``/clear`` wrote."""
    return (
        _event_type(event) == EventType.CONTEXT_COMPACT.value
        and (getattr(event, "data", None) or {}).get("strategy") == "clear"
    )


def _in_typed_order(events: list[Any]) -> list[Any]:
    """Return *events* in the order the model is shown them: each command the harness answered
    as one block, at the place it took effect.

    A command's block is its message and what the harness wrote for it: the
    compaction of ``/compress`` or ``/clear``, and the answer.  Both name
    the message.  They are written when the command's wake runs, which can
    be after its user has said more, and after the turn the command was
    typed in has gone on to its end.

    With nothing of a turn's between the message and what was written for
    it, the block stands where the message does: what was said after the
    command is said after its answer, and is not swallowed by its
    compaction.  Otherwise the command waited for a turn, and the block
    stands after the last thing that turn wrote: never inside it, where it
    would part a call from its result, and where a compaction would leave
    the turn's end standing.  What its user typed after the command and
    that turn did not read comes after the block.

    The answer to a ``/clear`` is left out: it is the harness's word to
    its user and nothing for the model, whose conversation starts anew on
    what its user says next.
    """
    named: dict[Any, list[Any]] = {}
    for event in events:
        answers = (getattr(event, "data", None) or {}).get("answers")
        if answers is not None and _event_type(event) in (EventType.LLM_RESPONSE.value, EventType.CONTEXT_COMPACT.value):
            named.setdefault(answers, []).append(event)
    if not named:
        return events
    at = {id(event): place for place, event in enumerate(events)}
    typed = {
        event.id: event for event in events
        if _event_type(event) == EventType.USER_MESSAGE.value and event.id in named
    }
    # An answer whose message is not among the events stays where it was written.
    moved = {id(event) for typed_at in typed for event in (typed[typed_at], *named[typed_at])}
    #: What stands right after each place, each group with the place it was written at.
    after: dict[int, list[tuple[int, list[Any]]]] = {}
    for typed_at in sorted(typed):
        message, block = typed[typed_at], named[typed_at]
        waited_for = [
            place for place in range(at[id(message)] + 1, at[id(block[0])]) if _of_a_turn(events[place])
        ]
        stands_after = waited_for[-1] if waited_for else at[id(message)]
        if any(_clears(event) for event in block):
            block = [event for event in block if _event_type(event) != EventType.LLM_RESPONSE.value]
        after.setdefault(stands_after, []).append((at[id(message)], [message, *block]))
        read_to = max(
            (place for place in waited_for if _event_type(events[place]) == EventType.LLM_REQUEST.value),
            default=at[id(message)],
        )
        for place in range(read_to + 1, stands_after):
            said = events[place]
            if (
                _event_type(said) == EventType.USER_MESSAGE.value
                and not (getattr(said, "data", None) or {}).get("synthetic")
                and id(said) not in moved
            ):
                moved.add(id(said))
                after[stands_after].append((place, [said]))
    ordered: list[Any] = []
    for place, event in enumerate(events):
        if id(event) not in moved:
            ordered.append(event)
        for _, group in sorted(after.get(place, ()), key=lambda placed: placed[0]):
            ordered.extend(group)
    return ordered


def _shown_before_its_answer(events: list[Any], typed_at: int) -> list[Any]:
    """Return what the model is shown up to the command typed at event *typed_at*, the command's
    message last: the conversation the command acts on, by the order its answer will stand in."""
    to_come = _AnswerToCome(typed_at)
    ordered = _in_typed_order([*events, to_come])
    return ordered[:ordered.index(to_come)]


class _AnswerToCome:
    """The answer a command's handler is about to write, for ``_in_typed_order`` to place."""

    id = None
    type = EventType.LLM_RESPONSE.value

    def __init__(self, typed_at: int) -> None:
        self.data = {"answers": typed_at}
