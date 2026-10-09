"""A command the user typed is answered once, whatever wakes its session afterwards.

The wake, the command handlers, replay, the loop and the turn's end are the
real ones, on the tests' Postgres and Redis; only the model is scripted.
Every wake is a new worker's: two wakes share nothing but the database.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select, text

import surogates.harness.loop as loop_module
from surogates.coding_agents.run_core import CodingRunOutcome
from surogates.config import SHARED_WORK_QUEUE_KEY
from surogates.db.models import Mission as MissionRow
from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.harness.loop_pending import _actionable_pending_events
from surogates.harness.slash_skill import build_deep_research_message
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.runtime import SLASH_COMMAND_IDS, SlashCommandConfig
from surogates.scheduled.materialize import materialize_scheduled_run
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session import LeaseNotHeldError
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from tests.test_steer_loop import _final_response

from .test_devices import AGENT_ID, api  # noqa: F401  (api is a fixture)
from .test_workstream_threads import TODO_CALL, answered, queued, start, turn_ends
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

HANDLERS = (
    "_handle_compress_command", "_handle_clear_command", "_handle_goal_command", "_handle_mission_command",
    "_handle_auto_research_command", "_handle_code_command", "_handle_loop_command",
)
# What a chat has said before its user types a command: enough for /compress to compress.
TALK = (("Open the report.", "It is open."), ("Read it.", "It says Q3 was flat."), ("And Q2?", "Q2 grew."), ("Thanks.", "Any time."))
# Further back than any other test leaves a session, so a sweep for sessions this quiet finds only ours.
LONG_QUIET = "interval '10 years'"
QUIET_FOR_NINE_YEARS = 9 * 365 * 86400


class Meanwhile:
    """The store of a worker to which something happens in the middle of a command.

    *dies*: the worker stops as it writes the command's answer (``answering``), once the answer is
    written and before the cursor has moved past it (``answered``), or as it ends the command's turn
    (``ending``).  *then*: what happens right after the answer is written.
    """

    def __init__(self, store, dies: str | None = None, then=None, before=None, as_it_wakes=None) -> None:
        self._store, self._dies, self._then, self._answered = store, dies, then, False
        #: What happens right before the answer is written, and right before the wake says it began.
        self._before, self._as_it_wakes = before, as_it_wakes

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    async def emit_event(self, session_id, event_type, data, **kwargs):
        if event_type == EventType.HARNESS_WAKE and self._as_it_wakes is not None:
            await self._as_it_wakes()
        if event_type != EventType.LLM_RESPONSE:
            return await self._store.emit_event(session_id, event_type, data, **kwargs)
        if self._dies == "answering":
            raise asyncio.CancelledError
        if self._before is not None:
            await self._before()
        event_id = await self._store.emit_event(session_id, event_type, data, **kwargs)
        self._answered = True
        if self._then is not None:
            await self._then()
        return event_id

    async def advance_harness_cursor(self, *args, **kwargs):
        # Only the write that ends a turn says whether the session comes to rest.
        if self._answered and (self._dies == "answered" or (self._dies == "ending" and "at_rest" in kwargs)):
            raise asyncio.CancelledError
        return await self._store.advance_harness_cursor(*args, **kwargs)


class Workers:
    """The workers that wake the app's sessions, and what they ran."""

    def __init__(self, api, monkeypatch) -> None:
        self.api, self.store = api, api.app.state.session_store
        #: Each command handler a wake ran, in order.
        self.ran: list[str] = []
        #: The conversation of each request made to the model.
        self.requests: list[list[dict]] = []
        #: The title the next wake gives an untitled chat, as the wake of a chat's first message does.
        self.title: str | None = None
        #: A stand-in for the sandbox pool, where a test's command needs one.
        self.sandbox_pool = None
        #: What the model answers next, in order; "Noted." once it runs out.
        self.replies: list[tuple[dict, dict]] = []
        #: What happens while the model writes its next answer, and while its tool call runs.
        self.during_the_request = None
        self.during_the_tool_call = None
        self._workers = 0
        self._taken_from_the_queue = False
        self._work_done = asyncio.Event()
        monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))

        async def title(**_):
            # A title is a model's call: it comes back after a command's instant answer.
            if self.title is not None:
                await self._work_done.wait()
            return self.title

        async def model(**kwargs):
            self.requests.append(kwargs["create_kwargs"]["messages"][1:])  # after the system prompt
            meanwhile, self.during_the_request = self.during_the_request, None
            if meanwhile is not None:
                await meanwhile()
            message, usage = self.replies.pop(0) if self.replies else _final_response("Noted.")
            if kwargs.get("on_tool_call_complete") is not None:
                for call in message.get("tool_calls") or []:
                    kwargs["on_tool_call_complete"](call)
            return message, usage

        monkeypatch.setattr(loop_module, "maybe_generate_session_title", title)
        monkeypatch.setattr(loop_module, "call_llm_with_retry", model)

    def worker(self, commands: SlashCommandConfig | None = None, store=None) -> AgentHarness:
        """A worker started just now: it knows nothing an earlier one learnt."""
        state, self._workers = self.api.app.state, self._workers + 1
        self._work_done.clear()

        async def compress(messages, *_, **__):
            kept = list(messages[-2:])
            return kept, {"strategy": "summary", "original_message_count": len(messages), "compressed_message_count": len(kept)}

        registry = ToolRegistry()
        ToolRuntime(registry).register_builtins()

        async def tool(name, arguments, **_):
            meanwhile, self.during_the_tool_call = self.during_the_tool_call, None
            if meanwhile is not None:
                await meanwhile()
            return '{"ok": true}'

        registry.dispatch = tool
        harness = AgentHarness(
            session_store=store or self.store,
            tool_registry=registry,
            llm_client=AsyncMock(),
            tenant=TenantContext(
                org_id=self.api.org_id, user_id=self.api.user_id, org_config={}, user_preferences={},
                permissions=frozenset(), asset_root="/tmp/test",
            ),
            worker_id=f"worker-{self._workers}",
            budget=IterationBudget(max_total=10),
            context_compressor=SimpleNamespace(
                context_length=200_000, prune_stale_browser_states=lambda messages: messages,
                should_compress=lambda *_, **__: False, compress=compress,
            ),
            prompt_builder=SimpleNamespace(has_agents=False, set_agent_def=lambda _: None, build=lambda: "SYS"),
            redis_client=state.redis,
            session_factory=state.session_factory,
            credential_vault=state.credential_vault,
            sandbox_pool=self.sandbox_pool,
            slash_commands=commands,
        )
        harness._renew_lease_forever = AsyncMock(return_value=None)
        harness._build_system_prompt = AsyncMock(return_value="SYS")
        drain = harness._drain_background_tasks

        async def drained(session_id):
            self._work_done.set()
            await drain(session_id)

        harness._drain_background_tasks = drained
        for name in HANDLERS:
            setattr(harness, name, self._watched(name, getattr(harness, name)))
        return harness

    def _watched(self, name: str, handler):
        async def run(*args, **kwargs):
            self.ran.append(name)
            return await handler(*args, **kwargs)

        return run

    async def wake(self, chat: UUID, commands: SlashCommandConfig | None = None) -> None:
        await self.worker(commands).wake(chat)

    async def wake_of_a_worker_that_dies(self, chat: UUID, when: str) -> None:
        """A wake cut off *when* (see ``Meanwhile``), as when its worker is stopped."""
        try:
            await self.worker(store=Meanwhile(self.store, dies=when)).wake(chat)
        except asyncio.CancelledError:
            pass

    async def chat(self, talk=TALK) -> UUID:
        """A chat of the app's user in which *talk* has been said, each turn ended as a turn ends."""
        created = await self.api.client.post("/v1/sessions", json={}, headers=self.api.auth())
        assert created.status_code == 201, created.text
        chat = UUID(created.json()["id"])
        for asked, said in talk:
            await self.says(chat, asked)
            await self.store.emit_event(chat, EventType.LLM_REQUEST, {})
            session = await self.session(chat)
            await answered(self.api, session, said)
            await turn_ends(self.api, session)
        return chat

    async def says(self, chat: UUID, words: str) -> None:
        """The chat's user sends *words*, as the web client does."""
        sent = await self.api.client.post(f"/v1/sessions/{chat}/messages", json={"content": words}, headers=self.api.auth())
        assert sent.status_code == 202, sent.text
        if self._taken_from_the_queue:
            await self.nobody_is_queued()

    async def nobody_is_queued(self) -> None:
        """From now on a dispatcher takes each wake a message queues: what is queued after is the harness's doing."""
        self._taken_from_the_queue = True
        await self.api.app.state.redis.delete(SHARED_WORK_QUEUE_KEY)

    async def types(self, chat: UUID, command: str, commands: SlashCommandConfig | None = None) -> str:
        """The chat's user sends *command*, and the wake their message queued answers it: the answer."""
        await self.says(chat, command)
        await self.wake(chat, commands)
        return (await self.said(chat))[-1]

    async def session(self, chat: UUID):
        return await self.store.get_session(chat)

    async def status(self, chat: UUID) -> str:
        return (await self.session(chat)).status

    async def log(self, chat: UUID) -> list[str]:
        return [event.type for event in await self.store.get_events(chat)]

    async def said(self, chat: UUID) -> list[str]:
        """What the chat's assistant said, in order: the model's words and the harness's answers alike."""
        events = await self.store.get_events(chat, types=[EventType.LLM_RESPONSE])
        return [event.data["message"]["content"] for event in events]

    async def looks_abandoned(self, chat: UUID) -> bool:
        """Whether a sweeper would take the chat for one whose worker died."""
        abandoned = await self.store.find_orphaned_sessions(stale_seconds=0, agent_id=AGENT_ID)
        return chat in [session.id for session in abandoned]

    async def nothing_waits(self, chat: UUID) -> bool:
        """Whether nothing of the chat's waits for a wake, and no sweeper takes it for abandoned."""
        cursor, events = await self.store.get_harness_cursor(chat), await self.store.get_events(chat)
        return not await self.looks_abandoned(chat) and _actionable_pending_events(events, cursor) == []

    async def a_helper_reports(self, chat: UUID) -> UUID:
        """A helper the chat started ends its turn: its report lands in the chat, which is queued."""
        helper = await create_child_session(store=self.store, parent=await self.session(chat), channel="worker")
        await answered(self.api, helper, "Checked the figures.")
        await turn_ends(self.api, helper)
        return helper.id

    async def its_browser_is_handed_back(self, chat: UUID) -> None:
        """What the browser's route writes when the chat's user hands its cloud browser back; it queues the chat."""
        await self.store.emit_event(
            chat, EventType.BROWSER_CONTROL_RETURNED, {"session_id": str(chat), "released_by": str(self.api.user_id)},
        )

    async def its_browser_is_taken_over(self, chat: UUID) -> None:
        """What the browser's route writes when the chat's user takes its cloud browser over; it queues nobody."""
        await self.store.emit_event(
            chat, EventType.BROWSER_CONTROL_GRANTED, {"session_id": str(chat), "owner_user_id": str(self.api.user_id)},
        )

    async def swept(self, chat: UUID) -> bool:
        """One pass of the orphan sweeper over the chat, quiet for long: whether it recovered and queued it."""
        state = self.api.app.state
        async with state.session_factory() as db:
            await db.execute(text(f"UPDATE sessions SET updated_at = now() - {LONG_QUIET} WHERE id = :id"), {"id": chat})
            await db.commit()
        before = (await self.log(chat)).count(EventType.HARNESS_RECOVERED.value)
        sweeper = Orchestrator(
            state.redis, self.store, lambda _session_id: None,
            queue_key=SHARED_WORK_QUEUE_KEY, max_concurrent=1, session_factory=state.session_factory, agent_id=AGENT_ID,
        )
        await sweeper._sweep_orphans_once(stale_seconds=QUIET_FOR_NINE_YEARS, reason="orchestrator_sweeper")
        return (await self.log(chat)).count(EventType.HARNESS_RECOVERED.value) > before

    async def missions(self, chat: UUID, mission_id=None) -> list:
        """The chat's mission in flight; or mission *mission_id*, whatever its state."""
        async with self.api.app.state.session_factory() as db:
            if mission_id is not None:
                return [await db.get(MissionRow, mission_id)]
            return list((await db.execute(select(MissionRow).where(
                MissionRow.session_id == chat, MissionRow.status.in_(("active", "paused")),
            ))).scalars())

    async def routines(self) -> list:
        return await ScheduledSessionStore(self.api.app.state.session_factory).list_for_user(
            org_id=self.api.org_id, user_id=self.api.user_id, service_account_id=None, agent_id=AGENT_ID,
        )


@pytest_asyncio.fixture(loop_scope="session")
async def workers(api, monkeypatch):
    yield Workers(api, monkeypatch)
    # The routines these chats made are due and their missions idle: no other test's ticker should find them.
    async with api.app.state.session_factory() as db:
        await db.execute(text("DELETE FROM scheduled_sessions WHERE org_id = :org"), {"org": api.org_id})
        await db.execute(
            text("UPDATE missions SET status = 'cancelled' WHERE org_id = :org AND status IN ('active', 'paused')"),
            {"org": api.org_id},
        )
        await db.commit()


#: Each command the harness answers itself and that starts no work of its own, with its handler.
ANSWERED = {
    "/compress": "_handle_compress_command",
    "/clear": "_handle_clear_command",
    "/goal status": "_handle_goal_command",
    "/goal pause": "_handle_goal_command",
    "/goal clear": "_handle_goal_command",
    "/mission status": "_handle_mission_command",
    "/mission pause": "_handle_mission_command",
    "/auto-research status": "_handle_auto_research_command",
    "/code status": "_handle_code_command",
    "/code help": "_handle_code_command",
    "/loop list": "_handle_loop_command",
    "/loop 1d Check the cash report": "_handle_loop_command",
}


async def a_helper_reports(workers: Workers, chat: UUID) -> None:
    await workers.a_helper_reports(chat)


async def its_browser_is_handed_back(workers: Workers, chat: UUID) -> None:
    await workers.its_browser_is_handed_back(chat)


async def the_sweeper_passes_while_its_browser_is_taken_over(workers: Workers, chat: UUID) -> None:
    # A take-over queues nobody; the sweeper is what would wake a chat it took for abandoned.
    await workers.its_browser_is_taken_over(chat)
    await workers.swept(chat)


#: What wakes a chat for a reason that is none of its user's messages.
LATER = {
    "a helper's report": a_helper_reports,
    "its browser handed back": its_browser_is_handed_back,
    "a recovery": the_sweeper_passes_while_its_browser_is_taken_over,
}


# -- The fault: the command's own wake, then a wake for something else --


@pytest.mark.parametrize("later", list(LATER))
@pytest.mark.parametrize("command", list(ANSWERED))
async def test_a_command_is_not_run_again_by_a_wake_for_something_else(workers, command, later):
    chat = await workers.chat()
    answer = await workers.types(chat, command)
    assert workers.ran == [ANSWERED[command]]

    await LATER[later](workers, chat)
    written = await workers.log(chat)
    await workers.wake(chat)

    assert workers.ran == [ANSWERED[command]]
    assert (await workers.said(chat)).count(answer) == 1
    # As after any turn that ended: the wake had nothing of the agent's to do, and wrote nothing.
    assert workers.requests == []
    assert await workers.log(chat) == written


@pytest.mark.parametrize("command", list(ANSWERED))
async def test_a_second_wake_with_nothing_new_runs_nothing_and_writes_nothing(workers, command):
    # A chat's first message: its wake also titles the chat, after the command's answer.
    workers.title = "The Q3 report"
    chat = await workers.chat(talk=())
    await workers.types(chat, command)
    written = await workers.log(chat)
    assert written[-1] == EventType.SESSION_TITLE_UPDATED.value

    await workers.wake(chat)

    assert workers.ran == [ANSWERED[command]]
    assert await workers.log(chat) == written


@pytest.mark.parametrize("later", list(LATER))
async def test_a_command_switched_off_for_the_agent_is_refused_once(workers, later):
    without_loop = SlashCommandConfig(commands=frozenset(SLASH_COMMAND_IDS - {"loop"}))
    chat = await workers.chat()
    refusal = await workers.types(chat, "/loop 1d Check the cash report", without_loop)
    assert refusal == "/loop is disabled for this agent."

    await LATER[later](workers, chat)
    await workers.wake(chat, without_loop)

    assert (await workers.said(chat)).count(refusal) == 1
    assert (workers.ran, workers.requests, await workers.routines()) == ([], [], [])


# -- A worker's death between the command and the later wake --


@pytest.mark.parametrize("command", ["/compress", "/clear", "/goal status", "/mission status", "/code status"])
async def test_a_command_whose_worker_died_before_answering_is_run_once_by_the_wake_that_recovers_it(workers, command):
    chat = await workers.chat()
    before = await workers.said(chat)
    await workers.says(chat, command)
    await workers.wake_of_a_worker_that_dies(chat, "answering")
    assert (workers.ran, await workers.said(chat)) == ([ANSWERED[command]], before)

    # The sweeper finds the chat left half done, and its wake runs the command its user still waits for.
    assert await workers.swept(chat)
    await workers.wake(chat)
    assert workers.ran == [ANSWERED[command]] * 2
    assert len(await workers.said(chat)) == len(before) + 1

    # Answered now, it is run by no wake after that.
    await workers.its_browser_is_handed_back(chat)
    await workers.wake(chat)
    assert workers.ran == [ANSWERED[command]] * 2
    assert len(await workers.said(chat)) == len(before) + 1


@pytest.mark.parametrize("command", ["/loop 1d Check the cash report", "/loop Check the cash report"])
async def test_a_routine_is_made_once_also_when_the_worker_dies_before_it_says_so(workers, command):
    chat = await workers.chat()
    before = await workers.said(chat)
    await workers.says(chat, command)
    # The routine's row is written; the worker stops as it writes the answer.
    await workers.wake_of_a_worker_that_dies(chat, "answering")
    [routine] = await workers.routines()
    assert await workers.said(chat) == before

    assert await workers.swept(chat)
    await workers.wake(chat)

    # The command is still to answer, and its answer names the routine it had made.
    assert [made.id for made in await workers.routines()] == [routine.id]
    assert (await workers.said(chat))[-1].startswith(f"Loop scheduled: `{routine.id}`")
    # The same words typed again are a second routine.
    await workers.types(chat, command)
    assert len(await workers.routines()) == 2


async def test_a_routine_is_made_once_though_the_user_said_more_before_the_command_was_run_again(workers):
    chat = await workers.chat()
    await workers.says(chat, "/loop 1d Check the cash report")
    await workers.wake_of_a_worker_that_dies(chat, "answering")
    [routine] = await workers.routines()
    # The routine is older than the user's last message, and no older than the command that made it.
    await workers.says(chat, "And Q1?")

    await workers.wake(chat)

    assert [made.id for made in await workers.routines()] == [routine.id]
    assert (await workers.said(chat))[-1].startswith(f"Loop scheduled: `{routine.id}`")


async def test_a_routine_the_session_made_for_another_prompt_is_not_taken_for_the_commands(workers):
    from surogates.scheduled.schedule import parse_schedule

    chat = await workers.chat()
    await workers.says(chat, "/loop 1d Check the cash report")
    # A turn under way makes a routine with its tool before the command's wake.
    other = await ScheduledSessionStore(workers.api.app.state.session_factory).create_loop(
        org_id=workers.api.org_id, user_id=workers.api.user_id, service_account_id=None, agent_id=AGENT_ID,
        prompt="Check the stock report", schedule=parse_schedule("1d", timezone_name="UTC"), created_from_session_id=chat,
    )

    await workers.wake(chat)

    made = [routine for routine in await workers.routines() if routine.id != other.id]
    assert [routine.prompt for routine in made] == ["Check the cash report"]
    assert (await workers.said(chat))[-1].startswith(f"Loop scheduled: `{made[0].id}`")


async def test_a_goal_whose_queued_turn_a_request_has_read_does_not_keep_the_chat_from_resting(workers):
    chat = await workers.chat()
    await workers.types(chat, "/goal Ship the Q3 report")
    await workers.wake(chat)
    # The goal's next turn is taken up, the model is asked, and the user stops the chat.
    await workers.store.emit_event(chat, EventType.HARNESS_WAKE, {"worker_id": "a-worker", "cursor": 0})
    await workers.store.emit_event(chat, EventType.LLM_REQUEST, {})
    stopped = await workers.api.client.post(f"/v1/sessions/{chat}/pause", headers=workers.api.auth())
    assert stopped.status_code == 200, stopped.text

    await workers.types(chat, "/goal status")

    # The goal is still set, and nothing of it waits: the chat rests on the command's answer.
    assert (await workers.status(chat), workers.ran) == ("completed", ["_handle_goal_command"] * 2)


DIED = ["/compress", "/clear", "/goal status", "/mission status", "/code status", "/loop 1d Check the cash report"]


@pytest.mark.parametrize("later", list(LATER))
@pytest.mark.parametrize("command", DIED)
async def test_a_command_whose_worker_died_once_it_had_answered_is_not_run_again(workers, command, later):
    chat = await workers.chat()
    await workers.says(chat, command)
    # Its answer is written; the worker stops before the cursor moves.  It is also how every chat
    # was left whose last message was /compress or /clear, before a command's answer ended its turn.
    await workers.wake_of_a_worker_that_dies(chat, "answered")
    answers = len(await workers.said(chat))
    assert (workers.ran, await workers.status(chat)) == ([ANSWERED[command]], "active")

    await LATER[later](workers, chat)
    await workers.wake(chat)

    # The answer in the log is what says a command was answered: the wake only ends the turn left open.
    assert workers.ran == [ANSWERED[command]]
    assert (len(await workers.said(chat)), workers.requests) == (answers, [])
    assert len(await workers.routines()) == (1 if command.startswith("/loop") else 0)
    assert (await workers.status(chat), await workers.looks_abandoned(chat)) == ("completed", False)


@pytest.mark.parametrize("later", list(LATER))
@pytest.mark.parametrize("command", ["/goal status", "/mission status", "/code status", "/loop 1d Check the cash report"])
async def test_a_chat_left_active_behind_an_answered_command_is_brought_to_rest_by_the_next_wake(workers, command, later):
    chat = await workers.chat()
    await workers.says(chat, command)
    # These commands move the cursor past their answer themselves; the worker stops before the turn's
    # end is written.  It is also how every chat was left whose last message was such a command,
    # before a command's answer ended its turn.
    await workers.wake_of_a_worker_that_dies(chat, "ending")
    answer = (await workers.said(chat))[-1]

    await LATER[later](workers, chat)
    await workers.wake(chat)

    assert workers.ran == [ANSWERED[command]]
    assert (await workers.said(chat)).count(answer) == 1
    # Nothing in it for the model either: the wake ends the turn that was left open.
    assert (workers.requests, await workers.status(chat), await workers.looks_abandoned(chat)) == ([], "completed", False)


async def test_a_chat_resumed_after_a_stop_that_landed_on_a_command_takes_no_turn(workers):
    chat = await workers.chat()

    async def the_user_stops_it():
        stopped = await workers.api.client.post(f"/v1/sessions/{chat}/pause", headers=workers.api.auth())
        assert stopped.status_code == 200, stopped.text

    await workers.says(chat, "/compress")
    await workers.worker(store=Meanwhile(workers.store, then=the_user_stops_it)).wake(chat)
    resumed = await workers.api.client.post(f"/v1/sessions/{chat}/resume", headers=workers.api.auth())
    assert resumed.status_code == 200, resumed.text

    await workers.wake(chat)

    # The command was answered before the stop: there is nothing to go on with.
    assert workers.ran == ["_handle_compress_command"]
    assert (workers.requests, await workers.status(chat)) == ([], "completed")


MOMENTS = ["the model's request", "a tool call"]


async def in_a_turn(workers: Workers, chat: UUID, moment: str, command: str) -> None:
    """The chat's turn on "Go on." runs, the model calling a tool in it; its user sends *command* at *moment* of it."""

    async def the_user_types_it():
        await workers.says(chat, command)

    workers.replies.append(TODO_CALL)
    if moment == "a tool call":
        workers.during_the_tool_call = the_user_types_it
    else:
        workers.during_the_request = the_user_types_it
    await workers.says(chat, "Go on.")
    await workers.wake(chat)


@pytest.mark.parametrize("moment", MOMENTS)
@pytest.mark.parametrize("command, done", [("/mission cancel", "cancelled"), ("/mission pause", "paused")])
async def test_a_command_typed_during_a_coordinators_turn_is_run_by_its_own_wake(workers, command, done, moment):
    chat = await a_coordinator(workers)
    [mission] = await workers.missions(chat)
    await in_a_turn(workers, chat, moment, command)
    # The turn went on to its end without it: a command is the harness's to answer, never the model's to read.
    assert (workers.ran, len(workers.requests)) == ([], 2)
    assert all(message.get("content") != command for request in workers.requests for message in request)

    await workers.wake(chat)

    assert workers.ran == ["_handle_mission_command"]
    assert ((await workers.missions(chat, mission.id))[0].status, len(workers.requests)) == (done, 2)
    # Once: a helper's report does not run it again.
    await workers.a_helper_reports(chat)
    await workers.wake(chat)
    assert workers.ran == ["_handle_mission_command"]


@pytest.mark.parametrize("moment", MOMENTS)
@pytest.mark.parametrize("command", ["/compress", "/clear", "/goal status", "/code status", "/loop list"])
async def test_a_command_typed_during_a_chats_turn_is_run_by_its_own_wake_once_the_turn_has_ended(workers, command, moment):
    chat = await workers.chat()
    await in_a_turn(workers, chat, moment, command)
    assert (workers.ran, len(workers.requests), await workers.status(chat)) == ([], 2, "completed")
    assert all(message.get("content") != command for request in workers.requests for message in request)

    await workers.wake(chat)

    assert (workers.ran, len(workers.requests)) == ([ANSWERED[command]], 2)
    assert (await workers.status(chat), await workers.nothing_waits(chat)) == ("completed", True)
    await workers.its_browser_is_handed_back(chat)
    await workers.wake(chat)
    assert (workers.ran, len(workers.requests)) == ([ANSWERED[command]], 2)


@pytest.mark.parametrize("ago, run", [("40 days", False), ("61 minutes", False), ("59 minutes", True)])
async def test_a_command_behind_a_turns_end_waits_for_an_hour_and_no_longer(workers, ago, run):
    chat = await workers.chat()
    # As the harness once left it: the command typed during a turn, the turn gone on to its end, and
    # no wake since.  Its user has long had the model's answer, or has it still on the screen.
    await in_a_turn(workers, chat, "a tool call", "/clear")
    async with workers.api.app.state.session_factory() as db:
        await db.execute(
            text(f"UPDATE events SET created_at = created_at - interval '{ago}' WHERE session_id = :id"), {"id": chat},
        )
        await db.commit()
    await workers.its_browser_is_handed_back(chat)
    written = await workers.log(chat)

    await workers.wake(chat)

    if run:
        assert (workers.ran, (await workers.said(chat))[-1]) == (["_handle_clear_command"], "Conversation cleared.")
    else:
        assert (workers.ran, await workers.log(chat), await workers.status(chat)) == ([], written, "completed")


async def test_a_command_refused_by_its_users_limit_is_run_at_the_retry_also_when_the_retrys_first_wake_crashes(api, workers):
    master = await master_of(api, await create(api))
    await workers.says(master.id, "/compress")
    refusing = workers.worker()
    refusing._admit_turn = AsyncMock(return_value="You have reached your limit.")
    await refusing.wake(master.id)
    # A failed session is its user's to retry: no wake for something else runs the command meanwhile.
    await workers.its_browser_is_handed_back(master.id)
    await workers.wake(master.id)
    assert (workers.ran, await workers.status(master.id)) == ([], "failed")
    retried = await api.client.post(f"/v1/sessions/{master.id}/retry", headers=api.auth())
    assert retried.status_code == 200, retried.text
    # The retry's wake says it began, and crashes before it reaches the command.
    crashing = workers.worker()
    crashing._build_system_prompt = AsyncMock(side_effect=TimeoutError("the hub timed out"))
    with pytest.raises(TimeoutError):
        await crashing.wake(master.id)
    assert workers.ran == []

    await workers.wake(master.id)

    assert (workers.ran, workers.requests) == (["_handle_compress_command"], [])
    assert await workers.status(master.id) == "completed"


# -- The chat after a command --


@pytest.mark.parametrize("command", list(ANSWERED))
async def test_a_command_answered_leaves_its_chat_at_rest(workers, command):
    chat = await workers.chat()
    await workers.types(chat, command)
    assert await workers.status(chat) == "completed"
    assert await workers.nothing_waits(chat)


async def test_the_users_next_message_after_a_command_is_answered_by_the_model(workers):
    chat = await workers.chat()
    answer = await workers.types(chat, "/goal status")

    await workers.says(chat, "And Q1?")
    await workers.wake(chat)

    [conversation] = workers.requests
    assert conversation[-3:] == [
        {"role": "user", "content": "/goal status"}, {"role": "assistant", "content": answer},
        {"role": "user", "content": "And Q1?"},
    ]
    assert (workers.ran, (await workers.said(chat))[-1], await workers.status(chat)) == (
        ["_handle_goal_command"], "Noted.", "completed",
    )


@pytest.mark.parametrize("command", ["/goal status", "/loop list"])
async def test_a_command_typed_again_is_run_again(workers, command):
    chat = await workers.chat()
    first = await workers.types(chat, command)
    again = await workers.types(chat, command)
    assert (workers.ran, first, (await workers.said(chat))[-2:]) == ([ANSWERED[command]] * 2, again, [first, again])


@pytest.mark.parametrize("command", ["/compress", "/goal status"])
async def test_a_message_sent_as_a_command_is_answered_is_answered_too(workers, command):
    chat = await workers.chat()

    async def the_user_goes_on():
        await workers.says(chat, "And Q1?")

    await workers.says(chat, command)
    await workers.worker(store=Meanwhile(workers.store, then=the_user_goes_on)).wake(chat)
    # More was said: the cursor stays behind it, and the chat does not rest on the command's answer.
    assert await workers.status(chat) == "active"
    assert not await workers.nothing_waits(chat)

    await workers.wake(chat)

    [conversation] = workers.requests
    assert conversation[-1] == {"role": "user", "content": "And Q1?"}
    assert (workers.ran, await workers.status(chat)) == ([ANSWERED[command]], "completed")


@pytest.mark.parametrize("command", ["/goal status", "/loop list", "/mission status", "/code status"])
async def test_a_message_sent_before_a_commands_answer_is_written_is_answered_too(workers, command):
    chat = await workers.chat()

    async def the_user_goes_on():
        await workers.says(chat, "And Q1?")

    await workers.says(chat, command)
    await workers.worker(store=Meanwhile(workers.store, before=the_user_goes_on)).wake(chat)
    # No turn read the message: the cursor stays behind it, and the chat does not rest.
    assert (await workers.status(chat), await workers.nothing_waits(chat)) == ("active", False)

    await workers.wake(chat)

    # One turn of the model's, which reads the message where its user sent it.
    [conversation] = workers.requests
    assert {"role": "user", "content": "And Q1?"} in conversation
    assert (workers.ran, (await workers.said(chat))[-1], await workers.status(chat)) == ([ANSWERED[command]], "Noted.", "completed")


async def test_a_second_command_sent_before_the_first_ones_answer_is_written_is_run_by_its_own_wake(workers):
    chat = await workers.chat()

    async def the_user_types_another():
        await workers.says(chat, "/loop list")

    await workers.says(chat, "/goal status")
    await workers.worker(store=Meanwhile(workers.store, before=the_user_types_another)).wake(chat)
    # The first one's answer comes after the second command in the log, and is not its answer.
    assert (workers.ran, await workers.status(chat)) == (["_handle_goal_command"], "active")

    await workers.wake(chat)

    assert (workers.ran, (await workers.said(chat))[-1]) == (["_handle_goal_command", "_handle_loop_command"], "No active loops.")
    assert (workers.requests, await workers.status(chat)) == ([], "completed")


async def test_a_command_the_model_once_read_as_words_is_still_run(workers):
    chat = await a_coordinator(workers)
    [mission] = await workers.missions(chat)
    emit = workers.store.emit_event
    # As a turn left it that read what its user typed meanwhile: the model answered the command in
    # its own words, and a later wake took a turn of the model's for something else.
    await workers.says(chat, "/mission pause")
    await emit(chat, EventType.LLM_REQUEST, {})
    await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "I cannot pause a mission."}})
    await emit(chat, EventType.HARNESS_WAKE, {"worker_id": "an-earlier-worker", "cursor": 0})
    await emit(chat, EventType.LLM_REQUEST, {})
    await emit(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "Waiting for the helpers."}})

    await workers.a_helper_reports(chat)
    await workers.wake(chat)

    # What the model said is no answer of the harness's: the command runs.
    assert (workers.ran, (await workers.said(chat))[-1]) == (["_handle_mission_command"], "Mission paused.")
    assert ((await workers.missions(chat, mission.id))[0].status, workers.requests) == ("paused", [])


async def test_two_commands_typed_before_any_wake_are_each_run_once_in_the_order_typed(workers):
    chat = await a_coordinator(workers)
    [mission] = await workers.missions(chat)
    await workers.says(chat, "/mission pause")
    await workers.says(chat, "/mission status")
    await workers.nobody_is_queued()

    await workers.wake(chat)
    # One command a wake, the oldest first; the chat is queued while another waits.
    assert (workers.ran, (await workers.said(chat))[-1]) == (["_handle_mission_command"], "Mission paused.")
    assert await queued(workers.api, await workers.session(chat))
    for _ in range(2):
        await workers.wake(chat)

    assert workers.ran == ["_handle_mission_command"] * 2
    paused, status = (await workers.said(chat))[-2:]
    assert (paused, "status=paused" in status) == ("Mission paused.", True)
    assert ((await workers.missions(chat, mission.id))[0].status, workers.requests) == ("paused", [])


async def test_a_routine_asked_for_before_another_command_is_made(workers):
    chat = await workers.chat()
    await workers.says(chat, "/loop 1d Check the cash report")
    await workers.says(chat, "/goal status")
    for _ in range(3):
        await workers.wake(chat)

    [routine] = await workers.routines()
    made, status = (await workers.said(chat))[-2:]
    assert (made.startswith(f"Loop scheduled: `{routine.id}`"), status) == (True, "No active outcome. Set one with /goal <text>.")
    assert (workers.ran, workers.requests, await workers.status(chat)) == (
        ["_handle_loop_command", "_handle_goal_command"], [], "completed",
    )


async def test_three_commands_in_a_row_and_one_after_a_plain_message_are_each_run_once_in_order(workers):
    chat = await workers.chat()
    for words in ("/goal status", "/loop list", "/code status", "And Q1?", "/code help"):
        await workers.says(chat, words)
    for _ in range(6):
        await workers.wake(chat)

    assert workers.ran == ["_handle_goal_command", "_handle_loop_command", "_handle_code_command", "_handle_code_command"]
    answers = [event.data["answers"] for event in await workers.store.get_events(chat, types=[EventType.LLM_RESPONSE]) if "answers" in event.data]
    typed = [event.id for event in await workers.store.get_events(chat, types=[EventType.USER_MESSAGE])]
    # Each answer names the message it answers.
    assert answers == [typed[-5], typed[-4], typed[-3], typed[-1]]


async def test_a_command_whose_message_the_wake_could_not_yet_see_is_not_answered_by_the_first_ones_answer(workers):
    chat = await workers.chat()
    await workers.says(chat, "/goal status")
    await workers.says(chat, "/loop list")
    hidden = (await workers.store.get_events(chat, types=[EventType.USER_MESSAGE]))[-1].id

    class NotYetCommitted(Meanwhile):
        """The second command's event has its id, below the wake's own, and is committed only after the wake read the log."""

        async def get_events(self, *args, **kwargs):
            return [event for event in await self._store.get_events(*args, **kwargs) if event.id != hidden]

        async def last_event(self, *args, **kwargs):
            return None

    await workers.worker(store=NotYetCommitted(workers.store)).wake(chat)
    assert workers.ran == ["_handle_goal_command"]

    await workers.wake(chat)
    assert (workers.ran, (await workers.said(chat))[-1]) == (["_handle_goal_command", "_handle_loop_command"], "No active loops.")


async def test_a_second_command_sent_as_the_first_ones_wake_begins_is_run_and_not_taken_for_answered(workers):
    chat = await workers.chat()

    async def the_user_sets_a_goal():
        await workers.says(chat, "/goal Ship the Q3 report")

    await workers.says(chat, "/goal status")
    # The second command lands after the wake read the log and before it says it began.
    await workers.worker(store=Meanwhile(workers.store, as_it_wakes=the_user_sets_a_goal)).wake(chat)
    await workers.wake(chat)
    await workers.wake(chat)

    # Both are run, the first one first: no answer to another command counts as its own.
    said = await workers.said(chat)
    first, second = "No active outcome. Set one with /goal <text>.", "Outcome defined (20 iterations): Ship the Q3 report"
    assert (said.count(first), said.count(second), said.index(first) < said.index(second)) == (1, 1, True)
    assert ((await workers.session(chat)).config.get("outcome") or {}).get("description") == "Ship the Q3 report"
    assert workers.requests[0][-1] == {"role": "user", "content": "Ship the Q3 report"}


async def test_a_question_asked_before_a_command_gets_its_turn_after_it(workers):
    chat = await workers.chat()
    await workers.says(chat, "And Q1?")
    await workers.says(chat, "/goal status")
    await workers.nobody_is_queued()

    await workers.wake(chat)
    # The command is answered; the chat does not rest over the question, and is queued for it.
    assert (workers.ran, workers.requests, await workers.status(chat)) == (["_handle_goal_command"], [], "active")
    assert not await workers.nothing_waits(chat)
    assert await queued(workers.api, await workers.session(chat))

    await workers.wake(chat)
    [conversation] = workers.requests
    assert {"role": "user", "content": "And Q1?"} in conversation
    assert (workers.ran, (await workers.said(chat))[-1], await workers.status(chat)) == (["_handle_goal_command"], "Noted.", "completed")


async def test_a_question_and_a_command_sent_while_a_command_is_answered_are_each_taken_up(workers):
    chat = await workers.chat()

    async def the_user_goes_on():
        await workers.says(chat, "And Q1?")
        await workers.says(chat, "/loop list")

    await workers.says(chat, "/goal status")
    await workers.worker(store=Meanwhile(workers.store, before=the_user_goes_on)).wake(chat)
    for _ in range(3):
        await workers.wake(chat)

    assert workers.ran == ["_handle_goal_command", "_handle_loop_command"]
    [conversation] = workers.requests
    assert {"role": "user", "content": "And Q1?"} in conversation
    assert await workers.status(chat) == "completed"


async def a_turn_cut_off(workers: Workers, chat: UUID, *, at: str, command: str | None) -> None:
    """The chat's turn on "Open the report." is cut off by its worker's death, in a tool call or
    right after the call's result moved the cursor; *command* is typed during the call."""
    store = workers.store
    await workers.says(chat, "Open the report.")
    lease = await store.try_acquire_lease(chat, "a-worker-that-dies", ttl_seconds=60)
    await store.emit_event(chat, EventType.HARNESS_WAKE, {"worker_id": "a-worker-that-dies", "cursor": 0})
    await store.emit_event(chat, EventType.LLM_REQUEST, {})
    call = {"id": "call_todo", "type": "function", "function": {"name": "todo", "arguments": "{}"}}
    await store.emit_event(chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "", "tool_calls": [call]}})
    await store.emit_event(chat, EventType.TOOL_CALL, {"tool_call_id": "call_todo", "name": "todo", "arguments": "{}"})
    if command is not None:
        await workers.says(chat, command)
    if at == "after the call's result":
        result = await store.emit_event(
            chat, EventType.TOOL_RESULT, {"tool_call_id": "call_todo", "name": "todo", "content": '{"ok": true}'},
        )
        await store.advance_harness_cursor(chat, result, lease.lease_token)
    await store.release_lease(chat, lease.lease_token)


@pytest.mark.parametrize("at", ["in the call", "after the call's result"])
async def test_a_turn_cut_off_with_a_command_waiting_is_resumed_once_the_command_is_run(workers, at):
    chat = await workers.chat()
    await a_turn_cut_off(workers, chat, at=at, command="/goal status")
    assert await workers.swept(chat)

    await workers.wake(chat)
    # The command first, and the chat does not rest over the turn that was cut off.
    assert (workers.ran, workers.requests, await workers.status(chat)) == (["_handle_goal_command"], [], "active")

    await workers.wake(chat)
    assert (workers.ran, len(workers.requests), (await workers.said(chat))[-1]) == (["_handle_goal_command"], 1, "Noted.")
    assert (await workers.status(chat), EventType.SESSION_FAIL.value in await workers.log(chat)) == ("completed", False)


async def test_a_turn_cut_off_with_no_command_is_resumed_as_before(workers):
    chat = await workers.chat()
    await a_turn_cut_off(workers, chat, at="in the call", command=None)
    assert await workers.swept(chat)
    await workers.wake(chat)
    assert (workers.ran, len(workers.requests), await workers.status(chat)) == ([], 1, "completed")


@pytest.mark.parametrize("command", ["/goal status", "/clear", "/compress", "/code status", "/loop list", "/mission status"])
async def test_a_worker_whose_lease_moved_does_not_answer_a_command_another_worker_has_answered(workers, command):
    chat = await workers.chat()
    answers = len(await workers.said(chat))

    async def its_lease_expires_and_another_worker_takes_the_chat():
        async with workers.api.app.state.session_factory() as db:
            await db.execute(
                text("UPDATE session_leases SET expires_at = now() - interval '1 minute' WHERE session_id = :id"), {"id": chat},
            )
            await db.commit()
        await workers.wake(chat)

    await workers.says(chat, command)
    slow = workers.worker(store=Meanwhile(workers.store, before=its_lease_expires_and_another_worker_takes_the_chat))
    with pytest.raises(LeaseNotHeldError):
        await slow.wake(chat)

    # One answer in the chat: the worker that lost the session writes none.
    assert len(await workers.said(chat)) == answers + 1
    await workers.wake(chat)
    assert (len(await workers.said(chat)), workers.requests) == (answers + 1, [])


async def test_a_chat_its_user_stopped_while_a_command_was_answered_stays_stopped(workers):
    chat = await workers.chat()

    async def the_user_stops_it():
        stopped = await workers.api.client.post(f"/v1/sessions/{chat}/pause", headers=workers.api.auth())
        assert stopped.status_code == 200, stopped.text

    await workers.says(chat, "/compress")
    await workers.worker(store=Meanwhile(workers.store, then=the_user_stops_it)).wake(chat)

    # A stop is its user's: the command's answer does not turn it into a turn that ended by itself.
    assert await workers.status(chat) == "paused"
    await workers.its_browser_is_handed_back(chat)
    await workers.wake(chat)
    assert (workers.ran, workers.requests, await workers.status(chat)) == (["_handle_compress_command"], [], "paused")


# -- Commands that start work of their own --


async def test_a_goal_set_by_a_command_is_worked_on_by_the_next_wake(workers):
    chat = await workers.chat()
    await workers.types(chat, "/goal Ship the Q3 report")
    # The command queued its own first turn: the chat stays active for it.
    assert (await workers.status(chat), (await workers.log(chat))[-1]) == ("active", EventType.USER_MESSAGE.value)

    await workers.wake(chat)

    assert workers.requests[0][-1] == {"role": "user", "content": "Ship the Q3 report"}
    assert workers.ran == ["_handle_goal_command"]


@pytest.mark.parametrize("command", ["/goal status", "/compress"])
async def test_a_goal_in_flight_goes_on_after_a_command_typed_between_its_turns(workers, command):
    chat = await workers.chat()
    await workers.types(chat, "/goal Ship the Q3 report")
    await workers.wake(chat)
    # The goal's first turn ended with more to do: its next turn is queued, as a message of the harness's.
    [first] = workers.requests
    waiting = (await workers.store.get_events(chat, types=[EventType.USER_MESSAGE]))[-1]
    assert (waiting.data.get("synthetic"), await workers.status(chat)) == ("outcome_continuation", "active")

    await workers.nobody_is_queued()
    await workers.types(chat, command)
    # The command is answered, and the chat does not rest on it: the goal's turn still waits, and is queued.
    assert (await workers.status(chat), await workers.nothing_waits(chat)) == ("active", False)
    assert await queued(workers.api, await workers.session(chat))
    await workers.wake(chat)

    assert len(workers.requests) == 2
    assert {"role": "user", "content": waiting.data["content"]} in workers.requests[1]
    assert workers.ran == ["_handle_goal_command", ANSWERED[command]]


async def test_a_goal_just_set_is_worked_on_though_a_command_was_typed_before_its_first_turn(workers):
    chat = await workers.chat()
    await workers.types(chat, "/goal Ship the Q3 report")
    await workers.types(chat, "/goal status")
    # The goal's first turn is still queued: the chat does not rest on the second command's answer.
    assert (await workers.status(chat), await workers.nothing_waits(chat)) == ("active", False)

    await workers.wake(chat)

    assert {"role": "user", "content": "Ship the Q3 report"} in workers.requests[0]
    assert workers.ran == ["_handle_goal_command"] * 2


@pytest.mark.parametrize("moment", MOMENTS)
async def test_a_command_typed_during_a_goals_turn_is_run_though_the_goals_next_turn_was_queued_after_it(workers, moment):
    chat = await workers.chat()
    await workers.types(chat, "/goal Ship the Q3 report")

    async def the_user_asks():
        await workers.says(chat, "/goal status")

    workers.replies.append(TODO_CALL)
    if moment == "a tool call":
        workers.during_the_tool_call = the_user_asks
    else:
        workers.during_the_request = the_user_asks
    await workers.wake(chat)
    # The goal's turn ended with more to do: the message that queues its next turn is the log's last.
    waiting = (await workers.store.get_events(chat, types=[EventType.USER_MESSAGE]))[-1]
    assert (waiting.data.get("synthetic"), workers.ran, len(workers.requests)) == ("outcome_continuation", ["_handle_goal_command"], 2)

    await workers.wake(chat)
    # The command first: it is its user's last word, and nothing has answered it.
    assert (workers.ran, len(workers.requests)) == (["_handle_goal_command"] * 2, 2)
    assert (await workers.said(chat))[-1].startswith("Outcome (active, ")

    await workers.wake(chat)
    assert (workers.ran, len(workers.requests)) == (["_handle_goal_command"] * 2, 3)
    assert {"role": "user", "content": waiting.data["content"]} in workers.requests[2]


@pytest.mark.parametrize("status", ["paused", "failed"])
async def test_a_command_typed_in_a_chat_that_was_stopped_or_had_failed_is_run_once(workers, status):
    chat = await workers.chat()
    await workers.store.update_session_status(chat, status)
    await workers.types(chat, "/goal status")
    await workers.its_browser_is_handed_back(chat)
    await workers.wake(chat)
    assert (workers.ran, workers.requests, await workers.status(chat)) == (["_handle_goal_command"], [], "completed")


MISSION = "/mission Audit the Q3 figures\n\nRubric:\n- every figure is sourced"


async def test_a_mission_started_by_a_command_is_worked_on_by_the_next_wake(workers):
    chat = await workers.chat()
    await workers.types(chat, MISSION)
    assert (await workers.status(chat), (await workers.log(chat))[-1]) == ("active", EventType.USER_MESSAGE.value)

    await workers.wake(chat)

    [conversation] = workers.requests
    assert conversation[-1]["content"].startswith("[Mission kickoff]")
    # Its mission in flight, the coordinator's chat stays active for its helpers' reports.
    assert (workers.ran, await workers.status(chat)) == (["_handle_mission_command"], "active")


async def a_coordinator(workers: Workers) -> UUID:
    """A chat whose mission is in flight: started by its command, its first turn taken."""
    chat = await workers.chat()
    await workers.types(chat, MISSION)
    await workers.wake(chat)
    workers.ran.clear()
    workers.requests.clear()
    return chat


async def test_a_coordinator_takes_its_turn_on_a_report_after_a_command(workers):
    chat = await a_coordinator(workers)
    answer = await workers.types(chat, "/mission status")
    assert answer.startswith("Mission ") and "status=active" in answer
    # A mission's chat is not at rest between its turns.
    assert await workers.status(chat) == "active"

    helper = await workers.a_helper_reports(chat)
    await workers.wake(chat)

    assert workers.ran == ["_handle_mission_command"]
    assert (await workers.said(chat)).count(answer) == 1
    [conversation] = workers.requests
    assert conversation[-3:] == [
        {"role": "user", "content": "/mission status"}, {"role": "assistant", "content": answer},
        {"role": "user", "content": f"[Worker {helper} completed]\nChecked the figures."},
    ]


@pytest.mark.parametrize("command", ["/compress", "/mission status"])
async def test_a_coordinators_command_is_answered_once_and_a_wake_with_nothing_new_takes_no_turn(workers, command):
    chat = await a_coordinator(workers)
    handler = "_handle_compress_command" if command == "/compress" else "_handle_mission_command"
    # A report the coordinator has read is nothing the cursor waits before.
    await workers.a_helper_reports(chat)
    await workers.wake(chat)
    workers.requests.clear()
    released = []

    async def release_for_session(session_id, **_):
        released.append(session_id)

    workers.sandbox_pool = SimpleNamespace(release_for_session=release_for_session)
    await workers.types(chat, command)
    written = await workers.log(chat)
    # Its mission in flight, the chat's turn has not ended: nothing it holds is let go.
    assert released == []
    # The mission's chat stays active, with nothing waiting: the cursor is past the command's answer.
    assert (await workers.status(chat), await workers.nothing_waits(chat)) == ("active", True)

    await workers.wake(chat)
    assert (workers.ran, workers.requests, await workers.log(chat)) == ([handler], [], written)

    helper = await workers.a_helper_reports(chat)
    await workers.wake(chat)
    assert workers.ran == [handler]
    [conversation] = workers.requests
    assert conversation[-1] == {"role": "user", "content": f"[Worker {helper} completed]\nChecked the figures."}


async def pause(workers: Workers, chat: UUID, how: str) -> None:
    """The chat's mission is paused: by its user's command, or with no command, as a budget's end pauses it."""
    if how == "typed":
        assert await workers.types(chat, "/mission pause") == "Mission paused."
        workers.ran.clear()
    else:
        async with workers.api.app.state.session_factory() as db:
            await db.execute(text("UPDATE missions SET status = 'paused' WHERE session_id = :id"), {"id": chat})
            await db.commit()


@pytest.mark.parametrize("how", ["typed", "not typed"])
async def test_a_paused_missions_coordinator_takes_no_turn_and_writes_nothing_on_what_arrives(workers, how):
    chat = await a_coordinator(workers)
    await pause(workers, chat, how)
    written = await workers.log(chat)
    admitted = AsyncMock(return_value=None)

    reports = 0
    for arrives in [workers.a_helper_reports] * 6 + [workers.its_browser_is_handed_back]:
        await arrives(chat)
        reports += arrives == workers.a_helper_reports
        worker = workers.worker()
        worker._admit_turn = admitted
        await worker.wake(chat)
        # No turn, no word, no hold, and nothing a sweeper would take for a worker's death.
        assert (workers.requests, workers.ran, admitted.await_count) == ([], [], 0)
        assert await workers.log(chat) == written + ["worker.complete"] * reports + ["browser.control_returned"] * (reports == 6 and arrives != workers.a_helper_reports)
        assert not await workers.looks_abandoned(chat)
        assert not await workers.swept(chat)


async def test_a_paused_missions_user_still_gets_a_turn_on_what_they_say(workers):
    chat = await a_coordinator(workers)
    await pause(workers, chat, "typed")

    await workers.says(chat, "How far along is it?")
    await workers.wake(chat)
    [conversation] = workers.requests
    assert conversation[-1] == {"role": "user", "content": "How far along is it?"}

    # Their last message is no command now, and the mission is paused all the same.
    for _ in range(2):
        await workers.a_helper_reports(chat)
        await workers.wake(chat)
    assert (len(workers.requests), workers.ran, (await workers.missions(chat))[0].status) == (1, [], "paused")


async def test_a_resumed_missions_coordinator_reads_the_reports_that_waited_once_and_in_order(workers):
    chat = await a_coordinator(workers)
    await pause(workers, chat, "typed")
    helpers = []
    for _ in range(3):
        helpers.append(await workers.a_helper_reports(chat))
        await workers.wake(chat)

    assert await workers.types(chat, "/mission resume") == "Mission resumed."
    # The command queues the coordinator: its turn reads the reports that waited.
    await workers.wake(chat)

    [conversation] = workers.requests
    assert [message["content"] for message in conversation[-3:]] == [
        f"[Worker {helper} completed]\nChecked the figures." for helper in helpers
    ]
    assert workers.ran == ["_handle_mission_command"]


async def test_the_sweeper_spares_a_paused_missions_chat_only_while_no_turn_of_its_is_under_way(workers):
    chat = await a_coordinator(workers)
    await workers.a_helper_reports(chat)
    # A mission in flight whose chat ends on a report nobody woke it for: its wake was lost.
    assert await workers.looks_abandoned(chat)

    await pause(workers, chat, "not typed")
    assert not await workers.looks_abandoned(chat)

    # A worker that dies in a turn of a paused mission's chat is a death still.
    await workers.store.emit_event(chat, EventType.TOOL_CALL, {"tool_call_id": "call_todo", "name": "todo", "arguments": "{}"})
    assert await workers.looks_abandoned(chat)


async def test_a_wake_that_goes_on_to_the_model_behind_a_command_leaves_the_cursor_to_that_turn(workers):
    chat = await a_coordinator(workers)
    await workers.types(chat, "/mission status")
    handed_back = await workers.store.emit_event(
        chat, EventType.BROWSER_CONTROL_RETURNED, {"session_id": str(chat), "released_by": str(workers.api.user_id)},
    )

    # The coordinator's turn is cut off before it has asked the model anything.
    dying = workers.worker()
    dying._run_loop = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await dying.wake(chat)

    # What woke it is still past the cursor, for the wake that recovers the chat.
    assert await workers.store.get_harness_cursor(chat) < handed_back


async def test_a_helpers_failure_no_turn_has_read_is_not_passed_over_either(workers):
    chat = await a_coordinator(workers)
    await workers.store.emit_event(chat, EventType.WORKER_FAILED, {"worker_id": "a-helper", "error": "the hub timed out"})
    await workers.types(chat, "/mission status")
    assert not await workers.nothing_waits(chat)

    await workers.wake(chat)

    [conversation] = workers.requests
    assert {"role": "user", "content": "[Worker a-helper failed: the hub timed out]"} in conversation


async def test_a_report_written_right_after_a_commands_answer_still_wakes_its_coordinator(workers):
    chat = await a_coordinator(workers)
    reported = []

    async def a_helper_reports():
        reported.append(await workers.a_helper_reports(chat))

    await workers.says(chat, "/mission status")
    await workers.worker(store=Meanwhile(workers.store, then=a_helper_reports)).wake(chat)
    # The report is the log's last event: the cursor stops before it, not on it.
    assert not await workers.nothing_waits(chat)

    await workers.wake(chat)
    [conversation] = workers.requests
    assert conversation[-1] == {"role": "user", "content": f"[Worker {reported[0]} completed]\nChecked the figures."}


async def test_a_coordinators_chat_is_not_brought_to_rest_when_its_mission_cannot_be_read(workers, monkeypatch):
    from surogates.missions.store import MissionStore

    chat = await a_coordinator(workers)
    await workers.says(chat, "/compress")
    with monkeypatch.context() as down:
        down.setattr(MissionStore, "get_active_for_session", AsyncMock(side_effect=ConnectionError("the database is away")))
        with pytest.raises(ConnectionError):
            await workers.wake(chat)
    # Not knowing is not "no mission": at rest, its helpers' reports would wake nobody.
    assert (workers.ran, await workers.status(chat)) == (["_handle_compress_command"], "active")

    # The wake the dispatcher retries does not run the command again, and the chat is its mission's still.
    await workers.wake(chat)
    assert (workers.ran, await workers.status(chat)) == (["_handle_compress_command"], "active")
    assert (await workers.said(chat)).count("Context is too small to compress — only 4 messages.") <= 1


async def test_a_report_no_turn_has_read_is_not_passed_over_by_a_commands_end(workers):
    chat = await a_coordinator(workers)
    # A helper reports, and before any wake reads the report the user types a command.
    helper = await workers.a_helper_reports(chat)
    await workers.types(chat, "/mission status")
    assert (workers.ran, workers.requests) == (["_handle_mission_command"], [])
    assert not await workers.nothing_waits(chat)

    # One wake was queued for both; the next one, a sweeper's or another report's, reads the report.
    await workers.wake(chat)

    [conversation] = workers.requests
    assert {"role": "user", "content": f"[Worker {helper} completed]\nChecked the figures."} in conversation
    assert workers.ran == ["_handle_mission_command"]


async def test_a_coordinator_does_not_run_a_command_it_refused_when_a_report_wakes_it(workers):
    without_research = SlashCommandConfig(commands=frozenset(SLASH_COMMAND_IDS - {"deep-research"}))
    chat = await a_coordinator(workers)
    refusal = await workers.types(chat, "/deep-research The Q3 market", without_research)
    assert refusal == "/deep-research is disabled for this agent."

    await workers.a_helper_reports(chat)
    await workers.wake(chat, without_research)

    assert (await workers.said(chat)).count(refusal) == 1
    [conversation] = workers.requests
    # The refused command is read as it was typed: never as the research it asked for.
    assert {"role": "user", "content": "/deep-research The Q3 market"} in conversation
    assert build_deep_research_message(topic="The Q3 market") not in [message["content"] for message in conversation]


async def test_a_research_command_is_the_models_to_answer(workers):
    chat = await workers.chat()
    await workers.says(chat, "/deep-research The Q3 market")
    await workers.wake(chat)

    [conversation] = workers.requests
    assert conversation[-1] == {"role": "user", "content": build_deep_research_message(topic="The Q3 market")}
    assert (workers.ran, (await workers.said(chat))[-1], await workers.status(chat)) == ([], "Noted.", "completed")


async def test_a_research_command_typed_during_a_turn_is_that_turns_to_read(workers):
    chat = await workers.chat()
    await in_a_turn(workers, chat, "a tool call", "/deep-research The Q3 market")
    # It is the model's to answer, so the turn under way reads it, as it reads anything its user says.
    assert workers.requests[1][-1] == {"role": "user", "content": "/deep-research The Q3 market"}
    assert await workers.status(chat) == "completed"
    await workers.wake(chat)
    assert (workers.ran, len(workers.requests)) == ([], 2)


async def test_a_research_command_cut_off_in_its_turn_is_read_again_as_the_research_it_asked_for(workers):
    chat = await workers.chat()
    await workers.says(chat, "/deep-research The Q3 market")
    # Its worker made a tool call, whose result moved the cursor past the message, and died.
    lease = await workers.store.try_acquire_lease(chat, "a-worker-that-dies", ttl_seconds=60)
    await workers.store.emit_event(chat, EventType.HARNESS_WAKE, {"worker_id": "a-worker-that-dies", "cursor": 0})
    await workers.store.emit_event(chat, EventType.LLM_REQUEST, {})
    call = {"id": "call_todo", "type": "function", "function": {"name": "todo", "arguments": "{}"}}
    await workers.store.emit_event(
        chat, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "", "tool_calls": [call]}},
    )
    await workers.store.emit_event(chat, EventType.TOOL_CALL, {"tool_call_id": "call_todo", "name": "todo", "arguments": "{}"})
    result = await workers.store.emit_event(
        chat, EventType.TOOL_RESULT, {"tool_call_id": "call_todo", "name": "todo", "content": '{"ok": true}'},
    )
    await workers.store.advance_harness_cursor(chat, result, lease.lease_token)
    await workers.store.release_lease(chat, lease.lease_token)
    await workers.store.emit_event(chat, EventType.HARNESS_RECOVERED, {"recovered_by": "orchestrator_sweeper"})
    await workers.its_browser_is_handed_back(chat)

    await workers.wake(chat)

    [conversation] = workers.requests
    assert {"role": "user", "content": build_deep_research_message(topic="The Q3 market")} in conversation


# -- A project's master --


@pytest.mark.parametrize("command", ["/compress", "/loop 1d Check the cash report"])
async def test_a_master_reads_a_threads_report_after_a_command(api, workers, command):
    master = await master_of(api, await create(api))
    thread = await start(api, master)
    answer = await workers.types(master.id, command)
    at_rest = await workers.status(master.id)

    await answered(api, thread, "Drafted the memo.")
    await turn_ends(api, thread)
    await workers.wake(master.id)

    assert workers.ran == [ANSWERED[command]]
    assert (await workers.said(master.id)).count(answer) == 1
    # The report revives the master, as after any turn of its own, and its turn reads it.
    assert [conversation[-1]["content"].split("]")[0] for conversation in workers.requests] == [
        f'[Thread "Draft A" ({thread.id}) reported',
    ]
    assert at_rest == "completed"
    assert len(await workers.routines()) == (1 if command.startswith("/loop") else 0)


async def test_a_command_whose_turn_was_refused_before_any_wake_read_it_is_run_when_its_user_retries(api, workers):
    master = await master_of(api, await create(api))
    await workers.says(master.id, "/compress")
    # The user's limit refuses the turn at its wake: the session fails, its cursor past the message.
    refusing = workers.worker()
    refusing._admit_turn = AsyncMock(return_value="You have reached your limit.")
    await refusing.wake(master.id)
    [typed] = await workers.store.get_events(master.id, types=[EventType.USER_MESSAGE])
    assert (workers.ran, await workers.status(master.id)) == ([], "failed")
    assert await workers.store.get_harness_cursor(master.id) > typed.id

    retried = await api.client.post(f"/v1/sessions/{master.id}/retry", headers=api.auth())
    assert retried.status_code == 200, retried.text
    await workers.wake(master.id)

    # No wake had taken the command up: the cursor behind which it lies is the failure's.
    assert workers.ran == ["_handle_compress_command"]
    assert (workers.requests, await workers.status(master.id)) == ([], "completed")


async def test_a_report_that_reached_a_master_before_its_command_was_answered_is_read_after_it(api, workers):
    master = await master_of(api, await create(api))
    thread = await start(api, master)
    await workers.types(master.id, "/goal status")
    workers.ran.clear()
    # The master's thread reports, and before any wake reads the report its user types a command.
    await answered(api, thread, "Drafted the memo.")
    await turn_ends(api, thread)
    await workers.nobody_is_queued()
    await workers.says(master.id, "/compress")

    # One wake answers the command, rests the master, and queues it for the report still unread.
    await workers.wake(master.id)
    assert (workers.ran, workers.requests, await workers.status(master.id)) == (["_handle_compress_command"], [], "completed")
    assert await queued(api, await workers.session(master.id))

    await workers.wake(master.id)
    assert workers.ran == ["_handle_compress_command"]
    assert [conversation[-1]["content"].split("]")[0] for conversation in workers.requests] == [
        f'[Thread "Draft A" ({thread.id}) reported',
    ]


# -- The sweeper --


async def test_a_chat_that_scheduled_a_routine_is_not_failed_by_the_sweeper_after_a_wake_for_something_else(workers):
    chat = await workers.chat()
    await workers.types(chat, "/loop 1d Check the cash report")
    await workers.its_browser_is_handed_back(chat)
    await workers.wake(chat)

    # Every pass of the sweeper, and the wake it would queue.
    recovered = []
    for _ in range(4):
        recovered.append(await workers.swept(chat))
        await workers.wake(chat)

    assert recovered == [False] * 4
    assert (await workers.status(chat), EventType.SESSION_FAIL.value in await workers.log(chat)) == ("completed", False)
    assert (workers.ran, len(await workers.routines())) == (["_handle_loop_command"], 1)


async def test_a_coding_run_that_finished_leaves_its_chat_at_rest(workers, monkeypatch):
    async def run(*, store, session, agent, started_metadata, **_):
        await store.emit_event(session.id, EventType.CODE_RUN_STARTED, {"run_id": "run-1", "agent": agent, **started_metadata})
        result = await store.emit_event(
            session.id, EventType.CODE_RUN_RESULT,
            {"run_id": "run-1", "agent": agent, "final_message": "The totals are fixed.", "error": None},
        )
        return CodingRunOutcome(status="ok", result_event_id=result)

    monkeypatch.setattr("surogates.coding_agents.run_core.execute_coding_run", run)
    workers.sandbox_pool = SimpleNamespace()  # the run above never reaches it
    chat = await workers.chat()
    await workers.says(chat, '/code claude "Fix the totals"')
    await workers.wake(chat)
    assert (await workers.log(chat))[-1] == EventType.CODE_RUN_RESULT.value
    at_rest = await workers.status(chat)

    # No pass of the sweeper takes the finished run for a worker's death, and no later wake looks at it again.
    recovered = []
    for _ in range(4):
        recovered.append(await workers.swept(chat))
        await workers.wake(chat)
    await workers.its_browser_is_handed_back(chat)
    await workers.wake(chat)

    assert (workers.ran, workers.requests) == (["_handle_code_command"], [])
    assert recovered == [False] * 4
    assert (at_rest, await workers.status(chat)) == ("completed", "completed")
    assert EventType.SESSION_FAIL.value not in await workers.log(chat)


async def test_a_message_sent_during_a_coding_run_is_answered_and_the_sweeper_does_not_fail_the_chat(workers, monkeypatch):
    chat = await workers.chat()

    async def run(*, store, session, agent, started_metadata, **_):
        await store.emit_event(session.id, EventType.CODE_RUN_STARTED, {"run_id": "run-1", "agent": agent, **started_metadata})
        await workers.says(chat, "And Q1?")
        result = await store.emit_event(
            session.id, EventType.CODE_RUN_RESULT,
            {"run_id": "run-1", "agent": agent, "final_message": "The totals are fixed.", "error": None},
        )
        return CodingRunOutcome(status="ok", result_event_id=result)

    monkeypatch.setattr("surogates.coding_agents.run_core.execute_coding_run", run)
    workers.sandbox_pool = SimpleNamespace()
    await workers.says(chat, '/code claude "Fix the totals"')
    await workers.wake(chat)

    # The wake the message queued is lost: every pass of the sweeper, and the wake it would queue.
    recovered = []
    for _ in range(4):
        recovered.append(await workers.swept(chat))
        await workers.wake(chat)

    assert recovered == [True, False, False, False]
    [conversation] = workers.requests
    assert {"role": "user", "content": "And Q1?"} in conversation
    assert (await workers.status(chat), EventType.SESSION_FAIL.value in await workers.log(chat)) == ("completed", False)
    assert workers.ran == ["_handle_code_command"]


async def test_a_coding_run_whose_worker_died_is_left_as_its_death_left_it(workers, monkeypatch):
    async def run(*, store, session, agent, started_metadata, **_):
        await store.emit_event(session.id, EventType.CODE_RUN_STARTED, {"run_id": "run-1", "agent": agent, **started_metadata})
        raise asyncio.CancelledError

    monkeypatch.setattr("surogates.coding_agents.run_core.execute_coding_run", run)
    workers.sandbox_pool = SimpleNamespace()
    chat = await workers.chat()
    await workers.says(chat, '/code claude "Fix the totals"')
    with pytest.raises(asyncio.CancelledError):
        await workers.wake(chat)

    # The run is started once, and the wake that recovers the chat neither starts it again nor
    # takes the chat for one whose command was answered: the sweeper still sees a worker's death.
    assert await workers.swept(chat)
    await workers.wake(chat)

    assert (await workers.log(chat)).count(EventType.CODE_RUN_STARTED.value) == 1
    assert (await workers.status(chat), await workers.nothing_waits(chat)) == ("active", False)


async def test_a_commands_end_releases_what_a_turns_end_releases(workers, monkeypatch):
    async def run(*, store, session, agent, started_metadata, **_):
        await store.emit_event(session.id, EventType.CODE_RUN_STARTED, {"run_id": "run-1", "agent": agent, **started_metadata})
        result = await store.emit_event(
            session.id, EventType.CODE_RUN_RESULT,
            {"run_id": "run-1", "agent": agent, "final_message": "The totals are fixed.", "error": None},
        )
        return CodingRunOutcome(status="ok", result_event_id=result)

    monkeypatch.setattr("surogates.coding_agents.run_core.execute_coding_run", run)
    released, destroyed, settled = [], [], []

    async def release_for_session(session_id, **_):
        released.append(session_id)
        return "the-runs-sandbox"

    async def destroy_released(sandbox_id, session_id):
        destroyed.append(sandbox_id)

    async def allowance_debit(agent_id, **hold):
        settled.append(hold)

    workers.sandbox_pool = SimpleNamespace(release_for_session=release_for_session, destroy_released=destroy_released)
    chat = await workers.chat()
    # The message route held the turn against its user's allowance.
    hold = {"allowance_id": "allowance-1", "reservation_id": "reservation-1", "reserved_tokens": 500}
    await workers.store.append_session_config_list(chat, "allowance_reservations", hold)
    await workers.says(chat, '/code claude "Fix the totals"')
    worker = workers.worker()
    worker._platform_client = SimpleNamespace(allowance_debit=allowance_debit)
    await worker.wake(chat)

    # The sandbox the run made is let go, and the hold goes back with nothing of the agent's spent.
    assert (released, destroyed) == ([str(chat)], ["the-runs-sandbox"])
    assert settled == [{"allowance_id": "allowance-1", "reserved_tokens": 500, "actual_tokens": 0, "reservation_id": "reservation-1"}]
    assert "allowance_reservations" not in (await workers.session(chat)).config

    # A later wake of the chat at rest releases nothing again.
    await workers.its_browser_is_handed_back(chat)
    await workers.wake(chat)
    assert (released, len(settled)) == ([str(chat)], 1)


# -- A routine's run --


async def test_a_routines_run_whose_prompt_is_a_command_ends_and_its_routine_goes_on(workers):
    state = workers.api.app.state
    routines = ScheduledSessionStore(state.session_factory)
    chat = await workers.chat()
    # A routine with no interval: each run says when the next one is, or the sweep gives it the fallback delay.
    await workers.types(chat, "/loop /goal status")
    [routine] = await workers.routines()
    run = await materialize_scheduled_run(
        routine, session_store=workers.store, scheduled_store=routines,
        storage=state.storage, settings=state.settings, redis=state.redis,
    )
    workers.ran.clear()

    await workers.wake(run)

    assert (workers.ran, (await workers.said(run))[-1]) == (["_handle_goal_command"], "No active outcome. Set one with /goal <text>.")
    assert await workers.status(run) == "completed"
    # The run is not taken for one whose worker died, to be queued again at every sweep ...
    stalled = await routines.find_retryable_stalled_dynamic_loop_runs(agent_id=AGENT_ID, stale_seconds=0)
    assert routine.id not in [r.id for r in stalled]
    # ... and its routine is given its next run.
    given = await routines.recover_stalled_dynamic_loops(agent_id=AGENT_ID, stale_seconds=0)
    assert routine.id in [r.id for r in given]
