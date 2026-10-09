"""Wherever a command is typed against a turn, its chat comes out whole.

A placement is a built-in command, the moment of a turn its user types it at,
what they type with it, and where the worker that answers it dies, or fails
to write its answer.  Every
placement is built by the loop at the foot of this file and run through the
real wake, handlers, replay, queue and sweeper; only the model is scripted,
and each of its replies is its own, so a later request shows where it stands.

Of every placement:

* each command ran its effect once and has one answer that names it.  A
  command whose worker died before its answer was written is run again by
  the wake that recovers the chat, whichever command it is: its handler
  starts twice, and what it made is there once;
* each message of the user's own, and each follow-up of a project
  coordinator's to its thread, was read by one turn of the model's, and no
  turn was asked with nothing new to read;
* each request is one a provider takes: a call's results follow it at once,
  a request starts on a user's message and ends on a user's message or a
  result, no command is shown that still waits, and nothing is shown twice;
* the chat comes to rest with nothing of its user's left waiting for their
  next message, and ends at rest: nobody queued, nothing for the sweeper,
  the cursor at the end; and the sweeper recovered it only where a worker
  died.
"""

from __future__ import annotations

import asyncio
import copy
import itertools
import json
from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select, text

import surogates.harness.loop as loop_module
from surogates.config import SHARED_WORK_QUEUE_KEY
from surogates.db.models import Mission as MissionRow
from surogates.session.events import EventType
from tests.test_steer_loop import _final_response

from .test_command_wake_once import MISSION, Workers, a_coordinator, workers  # noqa: F401  (workers is a fixture)
from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import answered, call_tool, queued, start, turn_ends
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

BEFORE_ANY_WAKE, FIRST_REQUEST, TOOL_CALL = "before any wake", "during the turn's first request", "during a tool call"
BETWEEN_CALLS, LAST_REQUEST, TURN_ENDED = "between two tool calls", "during the turn's last request", "after the turn's end"
MOMENTS = (BEFORE_ANY_WAKE, FIRST_REQUEST, TOOL_CALL, BETWEEN_CALLS, LAST_REQUEST, TURN_ENDED)
ALONE, MESSAGE_BEFORE, MESSAGE_AFTER, COMMAND_AFTER = "alone", "a message before it", "a message after it", "a command after it"
#: In a project's thread, where its coordinator follows up: never the user's message, and read as one.
FOLLOW_UP_BEFORE, FOLLOW_UP_AFTER = "a coordinator's follow-up before it", "a coordinator's follow-up after it"
COMPANY = (ALONE, MESSAGE_BEFORE, MESSAGE_AFTER, COMMAND_AFTER, FOLLOW_UP_BEFORE, FOLLOW_UP_AFTER)
NO_DEATH, BEFORE_THE_ANSWER, AFTER_THE_ANSWER = "no death", "its worker dies before the answer", "its worker dies after the answer"
#: No death, and no answer of the handler's either: the database is away as it writes it.
NOT_WRITTEN = "its answer cannot be written"
DEATHS = (NO_DEATH, BEFORE_THE_ANSWER, AFTER_THE_ANSWER, NOT_WRITTEN)

#: What the user says before the command, after it, and once the chat has come to rest.
ASKED_BEFORE, ASKED_AFTER, ASKED_LAST = "And Q1?", "And Q4?", "And then?"
#: What a thread's coordinator follows up with, and the thread's log holds of it.
FOLLOW_UP = "Keep it to one page."
FOLLOWED_UP = f"[From the project's coordinator]\n{FOLLOW_UP}"
#: What is work for a wake while it lies past the cursor; a turn's end leaves only its own notes there.
WORK = frozenset(kind.value for kind in (
    EventType.USER_MESSAGE, EventType.COORDINATOR_MESSAGE,
    EventType.LLM_REQUEST, EventType.LLM_RESPONSE, EventType.TOOL_CALL, EventType.TOOL_RESULT,
    EventType.WORKER_COMPLETE, EventType.WORKER_FAILED, EventType.CONTEXT_COMPACT,
))
#: The wake's own answer for a command whose handler wrote none, and what it tells its user to look at.
THE_WAKES_OWN = "{command} was cut off before it could answer. Check {look} before typing it again."
LOOK_AT = {
    "/goal": "`/goal status`", "/mission": "`/mission status`", "/loop": "`/loop list`", "/code": "`/code status`",
    "/compress": "the conversation", "/clear": "the conversation", "/deep-research": "the conversation",
}


def calls(*ids: str) -> tuple[dict, dict]:
    """The model's answer that calls the todo tool once for each of *ids*."""
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call, "type": "function", "function": {"name": "todo", "arguments": json.dumps({"list": call})}}
            for call in ids
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


def told(message: dict) -> tuple:
    """A message of a request, as what tells it from every other."""
    content = message.get("content")
    return (
        message.get("role"), content if isinstance(content, str) else json.dumps(content),
        tuple(call["id"] for call in message.get("tool_calls") or ()), message.get("tool_call_id"),
    )


def tail(request: list[dict]) -> list[dict]:
    """What a request holds that the model has not answered: all after its last assistant message."""
    answered = max((n for n, message in enumerate(request) if message.get("role") == "assistant"), default=-1)
    return request[answered + 1:]


def kept_by_compression(messages: list[dict]) -> list[dict]:
    """What the scripted compressor keeps: the conversation from its last user message on."""
    last = max((n for n, message in enumerate(messages) if message.get("role") == "user"), default=0)
    return list(messages[last:])


def not_taken_by_a_provider(conversation: list[dict]) -> list[str]:
    """Why a provider refuses *conversation*: a call apart from its results, or a result with no call before it."""
    faults, n = [], 0
    while n < len(conversation):
        message = conversation[n]
        n += 1
        if message.get("role") == "tool":
            faults.append(f"the result of {message.get('tool_call_id')} follows no call")
        elif message.get("tool_calls"):
            wanted = [call["id"] for call in message["tool_calls"]]
            results = [m.get("tool_call_id") for m in conversation[n:n + len(wanted)] if m.get("role") == "tool"]
            if sorted(results) != sorted(wanted):
                faults.append(f"the calls {wanted} are followed by the results {results}")
            n += len(results)
    return faults


class Dies:
    """The store of a chat's workers, of which the one that answers the command dies once: as it
    writes the answer, or at its next step once the answer is written.  Or it does not die, and
    the database is away for that one write."""

    def __init__(self, store, when: str) -> None:
        self._store, self._when, self._armed, self._answered = store, when, when != NO_DEATH, False
        #: The id of the command's message, once its user has typed it.
        self.command: int | None = None

    def __getattr__(self, name: str):
        step = getattr(self._store, name)
        if not (self._armed and self._answered and callable(step)):
            return step

        async def dies(*_, **__):
            self._armed = False
            raise asyncio.CancelledError

        return dies

    async def emit_event(self, session_id, event_type, data, **kwargs):
        its_answer = event_type == EventType.LLM_RESPONSE and self.command is not None and data.get("answers") == self.command
        if self._armed and (self._answered or its_answer and self._when == BEFORE_THE_ANSWER):
            self._armed = False
            raise asyncio.CancelledError
        if self._armed and its_answer and self._when == NOT_WRITTEN:
            self._armed = False
            raise ConnectionError("the database is away")
        event_id = await self._store.emit_event(session_id, event_type, data, **kwargs)
        self._answered = self._answered or its_answer
        return event_id


class Model:
    """The scripted model: what it was asked, in order, and what it answered each time."""

    def __init__(self) -> None:
        self.requests: list[list[dict]] = []
        self.replies: list[dict] = []
        #: What it answers next, in order; then a reply it has given to no other request.
        self.script: list[tuple[dict, dict]] = []
        self._answered = 0
        #: What happens while it writes its n-th answer.
        self.during: dict[int, object] = {}

    async def __call__(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["create_kwargs"]["messages"][1:]))  # after the system prompt
        meanwhile = self.during.pop(len(self.requests), None)
        if meanwhile is not None:
            await meanwhile()
        self._answered += 1
        message, usage = self.script.pop(0) if self.script else _final_response(f"Reply {self._answered}.")
        self.replies.append(message)
        if kwargs.get("on_tool_call_complete") is not None:
            for call in message.get("tool_calls") or []:
                kwargs["on_tool_call_complete"](call)
        return message, usage


@dataclass(frozen=True)
class Command:
    """A built-in command as its user types it, and what one run of it leaves."""

    typed: str
    handler: str
    #: How its handler's answer begins.
    answers: tuple[str, ...]
    #: What it made, checked by the method of ``Placement`` of this name.
    made: str | None = None
    #: Typed in a chat whose mission is in flight.
    of_a_coordinator: bool = False
    #: Typed in a project's thread, once its first turn has read its goal.
    in_a_thread: bool = False


COMMANDS = {
    "/goal status": Command("/goal status", "_handle_goal_command", ("No active outcome.",)),
    "/goal <text>": Command("/goal Ship the Q3 report", "_handle_goal_command", ("Outcome defined (",), made="a_goal"),
    "/mission <text>": Command(MISSION, "_handle_mission_command", ("Mission ",), made="a_mission"),
    "/mission pause": Command("/mission pause", "_handle_mission_command", ("Mission paused.",), made="a_pause", of_a_coordinator=True),
    "/compress": Command("/compress", "_handle_compress_command", ("Context compressed", "Context is too small"), made="a_compaction"),
    "/clear": Command("/clear", "_handle_clear_command", ("Conversation cleared.",), made="a_clearing"),
    "/loop <prompt>": Command("/loop 1d Check the cash report", "_handle_loop_command", ("Loop scheduled: ",), made="a_routine"),
    "/loop list": Command("/loop list", "_handle_loop_command", ("No active loops.",)),
    "/code status": Command("/code status", "_handle_code_command", ("",)),
    "/mission <text> in a thread": Command(MISSION, "_handle_mission_command", ("Mission ",), made="a_mission", in_a_thread=True),
    # Refused there, by the wake itself: no handler is run for it.
    "/deep-research in a thread": Command(
        "/deep-research Heat pumps in cold climates", "", ("A thread can't start /deep-research",), in_a_thread=True,
    ),
}
#: The command typed after the one placed: one of another handler's, so each one's runs can be counted.
SECOND = {False: Command("/loop list", "_handle_loop_command", ("",)), True: Command("/goal status", "_handle_goal_command", ("",))}


class Placement:
    """One chat, the command placed in it, and all that was asked of the model on the way."""

    def __init__(self, workers: Workers, model: Model, command: Command, dies: str) -> None:
        self.workers, self.model, self.command, self.dies = workers, model, command, dies
        self.store = Dies(workers.store, dies)
        # A thread refuses /loop.
        self.second = SECOND[command.handler == "_handle_loop_command" or command.in_a_thread]
        #: Each conversation /compress was given, and what the user typed, in order.
        self.compressed: list[list[dict]] = []
        self.typed: list[str] = []
        self.chat: UUID | None = None
        #: The coordinator of the thread's project, where the chat is a thread.
        self.master = None
        self.began = 0
        self.left_waiting: list[str] = []

    async def begin(self, moment: str) -> None:
        """The chat as it stands before anything of the placement is typed."""
        if self.command.of_a_coordinator:
            self.chat = await a_coordinator(self.workers)
        elif self.command.in_a_thread:
            self.master = await master_of(self.workers.api, await create(self.workers.api))
            thread = await start(self.workers.api, self.master)
            await self.workers.store.emit_event(thread.id, EventType.LLM_REQUEST, {})
            await answered(self.workers.api, thread, "On it.")
            await turn_ends(self.workers.api, thread)
            self.chat = thread.id
        else:
            self.chat = await self.workers.chat(talk=() if moment == BEFORE_ANY_WAKE else TALKED)
        await self.workers.api.app.state.redis.delete(SHARED_WORK_QUEUE_KEY)
        self.workers.ran.clear()
        # What the model was asked while the chat was set up is none of the placement's.
        self.model.requests.clear()
        self.model.replies.clear()
        self.began = max((event.id for event in await self.workers.store.get_events(self.chat)), default=0)

    async def says(self, words: str) -> None:
        await self.workers.says(self.chat, words)
        self.typed.append(words)
        if words == self.command.typed:
            self.store.command = (await self.workers.store.get_events(self.chat, types=[EventType.USER_MESSAGE]))[-1].id

    async def is_followed_up(self) -> None:
        """The thread's coordinator sends it a follow-up, as its ``message_thread`` does."""
        sent = await call_tool(self.workers.api, self.master, "message_thread", thread_id=str(self.chat), message=FOLLOW_UP)
        assert sent == {"status": "sent", "thread_id": str(self.chat)}
        self.typed.append(FOLLOWED_UP)

    async def wake(self) -> None:
        """A new worker wakes the chat; it may be the one that dies."""
        worker = self.workers.worker(store=self.store)

        async def compress(messages, *_, **__):
            self.compressed.append(copy.deepcopy(list(messages)))
            kept = kept_by_compression(messages)
            return kept, {"strategy": "summary", "original_message_count": len(messages), "compressed_message_count": len(kept)}

        worker._compressor.compress = compress
        try:
            await worker.wake(self.chat)
        except asyncio.CancelledError:
            pass

    async def settles(self) -> None:
        """The chat is woken as its dispatcher and the sweeper wake it, until neither has anything for it."""
        redis = self.workers.api.app.state.redis
        for _ in range(40):
            if await queued(self.workers.api, await self.workers.session(self.chat)):
                await redis.delete(SHARED_WORK_QUEUE_KEY)
                await self.wake()
            elif not await self.workers.swept(self.chat):
                return
        raise AssertionError("the chat is woken again and again, and never comes to rest")

    async def play(self, moment: str, company: str) -> None:
        """The whole placement: the turn, what is typed at *moment* of it, and a last message once all is at rest."""
        words = {
            ALONE: [self.command.typed],
            MESSAGE_BEFORE: [ASKED_BEFORE, self.command.typed],
            MESSAGE_AFTER: [self.command.typed, ASKED_AFTER],
            COMMAND_AFTER: [self.command.typed, self.second.typed],
            FOLLOW_UP_BEFORE: [FOLLOWED_UP, self.command.typed],
            FOLLOW_UP_AFTER: [self.command.typed, FOLLOWED_UP],
        }[company]

        async def typing():
            for said in words:
                await (self.is_followed_up() if said == FOLLOWED_UP else self.says(said))

        await self.begin(moment)
        if moment == BEFORE_ANY_WAKE:
            await typing()
        else:
            # The turn on "Go on.": the model calls the todo tool, then answers.
            first = len(self.model.requests) + 1
            self.model.script.append(calls("call_1", "call_2") if moment == BETWEEN_CALLS else calls("call_1"))
            if moment == FIRST_REQUEST:
                self.model.during[first] = typing
            elif moment == LAST_REQUEST:
                self.model.during[first + 1] = typing
            elif moment == TOOL_CALL:
                self.workers.during_the_tool_call = typing
            elif moment == BETWEEN_CALLS:
                async def the_first_call_runs():
                    self.workers.during_the_tool_call = typing

                self.workers.during_the_tool_call = the_first_call_runs
            await self.says("Go on.")
            await self.settles()
            if moment == TURN_ENDED:
                await typing()
        await self.settles()
        self.left_waiting = await self.still_waiting()
        await self.says(ASKED_LAST)
        await self.settles()

    async def still_waiting(self) -> list[str]:
        """What its user typed that the chat came to rest over: a command with no answer, a message no request holds."""
        self.events = await self.workers.store.get_events(self.chat)
        return [
            words.split("\n")[0] for words in self.typed
            if not (
                self.answers_to(self.message_of(words).id) if words.startswith("/")
                else any(told(message)[1] == words for request in self.model.requests for message in request)
            )
        ]

    # -- What is asserted of it --

    async def faults(self) -> list[str]:
        self.events = await self.workers.store.get_events(self.chat)
        self.asked = [event for event in self.events if event.type == EventType.LLM_REQUEST.value and event.id > self.began]
        faults = await self.of_the_commands() + self.of_the_messages() + self.of_the_requests() + await self.of_its_rest()
        # Not one that waits until its user says something more.
        return faults + [f"{words} waited for its user's next message" for words in self.left_waiting]

    def answers_to(self, typed_at: int) -> list:
        return [
            event for event in self.events
            if event.type == EventType.LLM_RESPONSE.value and event.data.get("answers") == typed_at
        ]

    def message_of(self, words: str):
        return next((
            event for event in reversed(self.events)
            if event.type == EventType.USER_MESSAGE.value and event.data.get("content") == words
        ), None)

    async def of_the_commands(self) -> list[str]:
        faults, answered_at = [], []
        for command in (self.command, self.second):
            if command.typed not in self.typed:
                continue
            answers = self.answers_to(self.message_of(command.typed).id)
            said = [event.data["message"]["content"] for event in answers]
            name = command.typed.split("\n")[0]
            if len(said) != 1:
                faults.append(f"{name} has {len(said)} answers that name it: {said}")
                continue
            answered_at.append(answers[0].id)
            head = command.typed.split()[0]
            if command is self.command and self.dies == NOT_WRITTEN:
                # The wake's own answer: it claims nothing of what the command did.
                if said[0] != THE_WAKES_OWN.format(command=head, look=LOOK_AT[head]):
                    faults.append(f"{name} is answered {said[0]!r} by its wake")
            elif not said[0].startswith(command.answers) or said[0].startswith(head):
                faults.append(f"{name} is answered {said[0]!r}")
            # Run again by design after a death before its answer, and only then.
            runs = 1 + (command is self.command and self.dies == BEFORE_THE_ANSWER)
            if command.handler and self.workers.ran.count(command.handler) != runs:
                faults.append(f"{name} was run {self.workers.ran.count(command.handler)} times, not {runs}: {self.workers.ran}")
        if answered_at != sorted(answered_at):
            faults.append("the second command was answered before the first")
        if self.command.made is not None:
            faults += await getattr(self, self.command.made)()
        return faults

    def user_messages(self, synthetic: str) -> list:
        return [
            event for event in self.events
            if event.type == EventType.USER_MESSAGE.value and event.data.get("synthetic") == synthetic
        ]

    async def a_goal(self) -> list[str]:
        goal = ((await self.workers.session(self.chat)).config.get("outcome") or {}).get("description")
        kickoffs = self.user_messages("outcome_kickoff")
        faults = [] if goal == "Ship the Q3 report" else [f"the chat's goal is {goal!r}"]
        if len(kickoffs) != 1:
            return faults + [f"the goal has {len(kickoffs)} first messages"]
        return faults + self.read_once(kickoffs[0].data["content"], "the goal's first message")

    async def a_mission(self) -> list[str]:
        async with self.workers.api.app.state.session_factory() as db:
            missions = list((await db.execute(select(MissionRow).where(MissionRow.session_id == self.chat))).scalars())
        kickoffs = self.user_messages("mission_kickoff")
        faults = [] if [mission.status for mission in missions] == ["active"] else [f"the chat's missions: {[m.status for m in missions]}"]
        if len(kickoffs) != 1:
            return faults + [f"the mission has {len(kickoffs)} first messages"]
        return faults + self.read_once(kickoffs[0].data["content"], "the mission's first message")

    async def a_pause(self) -> list[str]:
        [mission] = await self.workers.missions(self.chat)
        pauses = [event for event in self.events if event.type == EventType.MISSION_PAUSED.value]
        return [] if (mission.status, len(pauses)) == ("paused", 1) else [f"the mission is {mission.status}, paused {len(pauses)} times"]

    def compactions(self) -> list:
        typed_at = self.message_of(self.command.typed).id
        return [
            event for event in self.events
            if event.type == EventType.CONTEXT_COMPACT.value and event.data.get("answers") == typed_at
        ]

    async def a_compaction(self) -> list[str]:
        [answer] = [event.data["message"]["content"] for event in self.answers_to(self.message_of(self.command.typed).id)][:1] or [""]
        made = (len(self.compactions()), len(self.compressed))
        if answer.startswith("Context is too small") or self.dies == NOT_WRITTEN and not self.compressed:
            return [] if made == (0, 0) else [f"/compress says too small, and compressed {made}"]
        faults = [] if made == (1, 1) else [f"/compress wrote {made[0]} compactions from {made[1]} runs of the compressor"]
        for conversation in self.compressed:
            faults += [f"/compress was given a conversation in which {fault}" for fault in not_taken_by_a_provider(conversation)]
            faults += self.shown_twice(conversation, "/compress was given")
            if any(told(message)[1] in (ASKED_AFTER, self.second.typed) for message in tail(conversation)):
                faults.append("/compress was given what its user typed after it, unread")
        return faults

    async def a_clearing(self) -> list[str]:
        cleared = [event.data.get("strategy") for event in self.compactions()]
        return [] if cleared == ["clear"] else [f"/clear wrote the compactions {cleared}"]

    async def a_routine(self) -> list[str]:
        routines = [routine for routine in await self.workers.routines() if routine.prompt == "Check the cash report"]
        if len(routines) != 1:
            return [f"/loop made {len(routines)} routines"]
        [answer] = [event.data["message"]["content"] for event in self.answers_to(self.message_of(self.command.typed).id)][:1] or [""]
        return [] if str(routines[0].id) in answer or self.dies == NOT_WRITTEN else ["/loop's answer does not name its routine"]

    def read_once(self, words: str, what: str | None = None) -> list[str]:
        """*words* stand, unanswered, in one request: the turn that could read them."""
        reads = [n + 1 for n, request in enumerate(self.model.requests) if any(told(m)[1] == words for m in tail(request))]
        return [] if len(reads) == 1 else [f"{what or repr(words)} was the model's to read in the requests {reads}, not in one"]

    def of_the_messages(self) -> list[str]:
        faults = []
        for words in self.typed:
            if not words.startswith("/"):
                faults += self.read_once(words)
        seen = []
        for n, request in enumerate(self.model.requests, 1):
            if [told(message) for message in request] in seen:
                faults.append(f"request {n} asks what an earlier request asked")
            seen.append([told(message) for message in request])
        return faults

    def shown_twice(self, conversation: list[dict], where: str) -> list[str]:
        """What the user typed, what the model answered, a call and a result: each stands in a conversation once."""
        ours = set(self.typed) | {str(reply.get("content")) for reply in self.model.replies if reply.get("content")}
        marks = [
            mark for message in conversation for mark in (
                [told(message)[1]] if told(message)[1] in ours else []
            ) + [f"call {call}" for call in told(message)[2]] + ([f"result {told(message)[3]}"] if told(message)[3] else [])
        ]
        return sorted({f"{where} {mark!r} {marks.count(mark)} times" for mark in marks if marks.count(mark) > 1})

    def of_the_requests(self) -> list[str]:
        faults = []
        requests, replies = self.model.requests, self.model.replies
        if len(requests) != len(self.asked):
            return [f"the model was asked {len(requests)} times, and the log holds {len(self.asked)} requests"]
        commands = {
            command.typed: self.answers_to(self.message_of(command.typed).id)
            for command in (self.command, self.second) if command.typed in self.typed
        }
        compactions = [event for event in self.events if event.type == EventType.CONTEXT_COMPACT.value and event.id > self.began]
        for n, request in enumerate(requests):
            where = f"request {n + 1}"
            roles = [message.get("role") for message in request]
            if not roles:
                faults.append(f"{where} is empty")
                continue
            if roles[0] != "user":
                faults.append(f"{where} starts on a message of the {roles[0]}'s")
            if roles[-1] not in ("user", "tool"):
                faults.append(f"{where} ends on a message of the {roles[-1]}'s")
            faults += [f"in {where} {fault}" for fault in not_taken_by_a_provider(request)]
            faults += self.shown_twice(request, f"{where} shows")
            for at, message in enumerate(request):
                answers = commands.get(told(message)[1]) if message.get("role") == "user" else None
                if answers is None:
                    continue
                answer = {"role": "assistant", "content": answers[0].data["message"]["content"]} if answers else None
                if answer is None or self.asked[n].id < answers[0].id:
                    faults.append(f"{where} shows the model {told(message)[1]!r}, which still waits")
                elif at + 1 == len(request) or told(request[at + 1]) != told(answer):
                    faults.append(f"{where} shows {told(message)[1]!r} without its answer after it")
            if n == 0:
                continue
            # From one request to the next the conversation only grows, by the model's reply and
            # what came after it, unless a command compacted or cleared it between the two.
            before = [told(message) for message in requests[n - 1]] + [told(replies[n - 1])]
            between = [event for event in compactions if self.asked[n - 1].id < event.id < self.asked[n].id]
            if not between:
                if [told(message) for message in request[:len(before)]] != before:
                    faults.append(f"{where} does not go on from request {n} and its reply: {self.shape(request)}")
            elif between[-1].data.get("strategy") == "clear":
                left = [told(message)[1] for message in request if told(message) in before]
                if left:
                    faults.append(f"{where} shows what was cleared: {left}")
            else:
                kept = [told(message) for message in between[-1].data["compacted_messages"]]
                if [told(message) for message in request[:len(kept)]] != kept:
                    faults.append(f"{where} does not start on the compacted conversation: {self.shape(request)}")
        for conversation, compaction in zip(self.compressed, self.compactions()):
            earlier = [n for n, asked in enumerate(self.asked) if asked.id < compaction.id]
            if earlier:
                before = [told(message) for message in requests[earlier[-1]]] + [told(replies[earlier[-1]])]
                if [told(message) for message in conversation[:len(before)]] != before:
                    faults.append(f"/compress was given a conversation that does not go on from request {earlier[-1] + 1} and its reply")
        return faults

    @staticmethod
    def shape(request: list[dict]) -> list:
        return [(m.get("role"), "calls" if m.get("tool_calls") else str(m.get("content"))[:40]) for m in request[-8:]]

    async def of_its_rest(self) -> list[str]:
        faults = []
        types = [event.type for event in self.events]
        for ended_badly in (EventType.SESSION_FAIL.value, EventType.HARNESS_CRASH.value):
            if ended_badly in types:
                faults.append(f"the log holds {ended_badly}")
        # Where no worker died the sweeper has nothing to recover: what waits gets its wake from the queue.
        recovered = types.count(EventType.HARNESS_RECOVERED.value)
        if recovered > (self.dies in (BEFORE_THE_ANSWER, AFTER_THE_ANSWER)):
            faults.append(f"the sweeper recovered the chat {recovered} times")
        in_flight = bool(await self.workers.missions(self.chat))
        status = await self.workers.status(self.chat)
        # A mission's chat stays active between its turns.
        if status != ("active" if in_flight else "completed"):
            faults.append(f"the chat ends {status}")
        if await queued(self.workers.api, await self.workers.session(self.chat)):
            faults.append("the chat ends queued")
        done = await self.workers.store.get_harness_cursor(self.chat)
        if in_flight:
            # A mission's chat has no turn's end to move the cursor: its last turn lies past it, answered.
            done = max([done] + [
                event.id for event in self.events
                if event.type == EventType.LLM_RESPONSE.value and not event.data["message"].get("tool_calls")
            ])
        waiting = sorted({event.type for event in self.events if event.id > done and event.type in WORK})
        if waiting:
            faults.append(f"the chat ends with work past the cursor: {waiting}")
        if await self.workers.looks_abandoned(self.chat):
            faults.append("the chat ends as one the sweeper takes for abandoned")
        return faults


#: What the chat has said before the placement, where it has said anything.
TALKED = (("Open the report.", "It is open."), ("Read it.", "It says Q3 was flat."), ("And Q2 of last year?", "It grew."), ("Thanks.", "Any time."))


@pytest_asyncio.fixture(loop_scope="session")
async def placed(workers, monkeypatch):
    model = Model()
    monkeypatch.setattr(loop_module, "call_llm_with_retry", model)
    placements: list[Placement] = []

    def place(command: Command, dies: str) -> Placement:
        placements.append(Placement(workers, model, command, dies))
        return placements[-1]

    yield place
    # A sweep takes a chat for one quiet for years: no other test's sweep should find these again.
    async with workers.api.app.state.session_factory() as db:
        for placement in placements:
            await db.execute(text("UPDATE sessions SET updated_at = now() WHERE id = :id"), {"id": placement.chat})
        await db.commit()


PLACEMENTS = [
    placement for placement in itertools.product(COMMANDS, MOMENTS, COMPANY, DEATHS)
    # A thread's command is placed where its user types one: after a turn's end.
    if not COMMANDS[placement[0]].in_a_thread or placement[1] == BEFORE_ANY_WAKE
    # Only a thread has a coordinator to follow it up.
    if COMMANDS[placement[0]].in_a_thread or placement[2] not in (FOLLOW_UP_BEFORE, FOLLOW_UP_AFTER)
]


@pytest.mark.parametrize("command, moment, company, dies", PLACEMENTS, ids=[" | ".join(placement) for placement in PLACEMENTS])
async def test_a_command_placed_against_a_turn_leaves_its_chat_whole(placed, command, moment, company, dies):
    placement = placed(COMMANDS[command], dies)
    await placement.play(moment, company)
    faults = await placement.faults()
    assert not faults, "\n".join(faults)
