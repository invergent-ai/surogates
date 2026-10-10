"""A turn of a project's thread bound with a copy of its own opens the copy at its start; one with nowhere to work sends nothing more.

Each scene runs a worker's real wake: the prompt read from the thread's
folder through its computer, the turn run by the loop, its artifacts and its
end written as a worker writes them.  Only the model is a script.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import text

import surogates.devices.operations as operations_module
import surogates.harness.loop as loop_module
from surogates.devices.binding import THREAD_KINDS
from surogates.devices.history import NO_COPY, NOWHERE, OPEN_TRIES, thread_copy
from surogates.devices.operations import OperationRequest
from surogates.devices.workspace import DeviceOperationError
from surogates.harness.landing import TURN_ENDS
from surogates.harness.tool_exec import _open_local_copy
from surogates.runtime import SlashCommandConfig
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.tools.builtin import delegate as delegate_module
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from surogates.workstreams.threads import stop_thread
from tests.test_steer_loop import _final_response
from tests.test_wake_slash_command_gate import _harness as a_waking_harness

from .test_device_sessions import is_bound
from .test_devices import FOLDER, NONCE, api, eventually, link_url  # noqa: F401  (api and link_url are fixtures)
from .test_local_history_threads import computer, made_with_copy, picture  # noqa: F401  (computer is a fixture)
from .test_local_threads import begin, begun_local, confirmed, journal, laptop  # noqa: F401  (laptop is a fixture)
from .test_turn_sagas import calling
from .test_workstream_threads import children_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: Each session's model, as the scene scripts it: the loop asks it by the session it runs.
SCRIPTS: dict[UUID, list] = {}


async def begun_with_copy(api, computer, *, nonce: str = NONCE):
    """A project, its master and a thread made on *computer*, bound with a copy of its own by its app, which is connected, and begun."""
    project, master, thread = await made_with_copy(api, computer.device_id)
    computer.app.prepare(nonce, FOLDER)
    await journal(api).bind(session_id=thread.id, device_id=UUID(computer.device_id), folder=FOLDER, nonce=nonce, history=thread.id)
    if not computer.app.connected:
        await computer.app.connect()
    await eventually(lambda: is_bound(api, str(thread.id)))
    assert (await begin(api, project, str(thread.id))).status_code == 201
    return project, master, await api.app.state.session_store.get_session(thread.id)


def a_worker(api, monkeypatch, session, replies, *, during=None):
    """A worker whose wake of *session* runs for real, its model giving *replies* in order, then once more for the file a goal names.

    The prompt is the real one, with the folder's own context read through
    its computer; so are the replay, the loop, a fenced artifact's
    promotion and the turn's end.  The ``memory`` tool runs *during* (with
    the harness) instead of writing memory.
    """
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    state = api.app.state
    harness = a_waking_harness(state.session_store, SlashCommandConfig())
    del harness._rebuild_messages, harness._build_system_prompt, harness._engineer_context
    harness._prompt.build.return_value, harness._prompt.has_agents = "system", False
    harness._compressor = SimpleNamespace(
        context_length=200_000, _context_window=200_000, prune_stale_browser_states=lambda messages: messages,
        should_compress=lambda *_, **__: False,
    )
    harness._redis, harness._session_factory, harness._sandbox_pool = state.redis, state.session_factory, None
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    dispatch = registry.dispatch

    async def tool(name, arguments, **kwargs):
        if name != "memory":
            return await dispatch(name, arguments, **kwargs)
        if during is not None:
            await during(harness)
        return '{"ok": true}'

    monkeypatch.setattr(registry, "dispatch", tool)
    harness._tools = registry
    monkeypatch.setitem(SCRIPTS, session.id, [*replies, *[_final_response("That is all.")] * 2])
    harness.model_asked = 0

    async def model(**kwargs):
        asked = kwargs["session"]
        if asked.id == session.id:
            harness.model_asked += 1
        message, usage = SCRIPTS[asked.id].pop(0)
        if kwargs.get("on_tool_call_complete") is not None:
            for call in message.get("tool_calls") or []:
                kwargs["on_tool_call_complete"](call)
        return message, usage

    monkeypatch.setattr(loop_module, "call_llm_with_retry", model)
    return harness


async def woken(api, monkeypatch, session, replies=(), *, said: str | None = None, during=None):
    """One wake of *session* by a worker (``a_worker``), after its user said *said* if anything; the worker."""
    if said is not None:
        await api.app.state.session_store.emit_event(session.id, EventType.USER_MESSAGE, {"content": said})
    harness = a_worker(api, monkeypatch, session, replies, during=during)
    await harness.wake(session.id)
    return harness


def asked(computer, kind: str | None = None) -> list[tuple[str, str]]:
    """The thread kinds *computer*'s app ran, each as (invocation, action), in order."""
    return [(invocation, action) for invocation, _, asked_kind, action in computer.app.places.asked if kind in (None, asked_kind)]


async def opens_of(api, session) -> list[tuple[str, str]]:
    """Each open the journal holds for *session*, oldest first: its name, and how it ended (``ok``, its refusal's type, or ``open``)."""
    async with api.app.state.session_factory() as db:
        rows = (await db.execute(text(
            "SELECT invocation_id, outcome FROM device_operations"
            " WHERE calling_session_id = :id AND kind = 'history' AND args->>'action' = 'open' ORDER BY created_at"
        ), {"id": session.id})).all()
    return [(name, "open" if outcome is None else "ok" if "ok" in outcome else outcome["error"]["type"]) for name, outcome in rows]


async def operations_of(api, session) -> list[tuple[str, str]]:
    """Every operation the journal holds for *session*'s folder, oldest first, as (kind, invocation)."""
    return [(kind, name) for kind, name, *_ in await journal_of(api, session)]


async def journal_of(api, session, *, since=None) -> list[tuple]:
    """Every operation the journal holds for *session*'s folder (since the database's time *since*), oldest first:
    its kind, its invocation, when it was recorded, when it ended, and its outcome."""
    async with api.app.state.session_factory() as db:
        rows = (await db.execute(text(
            "SELECT kind, invocation_id, created_at, completed_at, outcome FROM device_operations"
            " WHERE root_session_id = :id AND (CAST(:since AS timestamptz) IS NULL OR created_at > :since)"
            " ORDER BY created_at, ordinal"
        ), {"id": session.id, "since": since})).all()
    return [tuple(row) for row in rows]


async def database_now(api):
    async with api.app.state.session_factory() as db:
        return (await db.execute(text("SELECT clock_timestamp()"))).scalar_one()


def sent_after_the_open(operations: list[tuple]) -> list[str]:
    """The operations of one turn, each of which reached the computer after the turn's open was answered a copy: their kinds.

    Fails where any was recorded before that answer, or where the turn has none.
    """
    opens = [op for op in operations if op[0] == "history" and op[1].startswith("open:")]
    answered = [op for op in opens if op[4] is not None and "ok" in op[4]]
    assert answered, operations
    rest = [op for op in operations if op not in opens]
    early = [op[:2] for op in rest if op[2] <= answered[-1][3]]
    assert not early, f"recorded before the open was answered: {early}"
    return [op[0] for op in rest]


async def turn_end(api, session) -> int:
    """The id of *session*'s last turn end: the next turn's name."""
    return (await api.app.state.session_store.last_event(session.id, *TURN_ENDS)).id


async def failure(api, session) -> dict:
    """How *session*'s last turn failed, as its end says it."""
    [*_, failed] = await api.app.state.session_store.get_events(session.id, types=[EventType.SESSION_FAIL])
    return failed.data


async def steps_of(api, session, *, after: int = 0) -> list[str]:
    """The tools *session*'s steps called since event *after*."""
    return [e.data["name"] for e in await api.app.state.session_store.get_events(session.id, after=after, types=[EventType.TOOL_CALL])]


async def status_of(api, session) -> str:
    return (await api.app.state.session_store.get_session(session.id)).status


async def lease_let_go(api, session) -> bool:
    """Whether another worker can take *session*'s lease now: the last worker let it go."""
    store = api.app.state.session_store
    taken = await store.try_acquire_lease(session.id, "the-next-worker", ttl_seconds=60)
    if taken is not None:
        await store.release_lease(session.id, taken.lease_token)
    return taken is not None


def refusing_opens(computer, times: int, error: dict | None = None) -> None:
    """*computer* refuses its next *times* opens, as its guest does for a history that did not answer in time."""
    left = [times]
    error = error or {"type": "history", "code": "no_answer", "message": "This folder's history did not answer"}

    def refuse(frame, outcome):
        if (frame["kind"], frame["args"].get("action")) == ("history", "open") and left[0]:
            left[0] -= 1
            return {"error": error}
        return outcome

    computer.app.lie = refuse


def holding_opens(computer, thread):
    """*computer* takes *thread*'s opens and answers none until let go; the frames it holds, and how to let them go."""
    held: list = []
    handle = computer.app._handle

    async def holding(frame, ws) -> None:
        if frame["kind"] == "history" and frame["session_id"] == str(thread.id) and frame["args"].get("action") == "open":
            held.append((frame, ws))
            return
        await handle(frame, ws)

    async def let_go() -> None:
        computer.app._handle = handle
        for frame, ws in held:
            await handle(frame, ws)

    computer.app._handle = holding
    return held, let_go


WRITES = calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}), ("terminal", {"command": "echo made > made.txt"}))
#: An answer that calls no tool, with a fence its turn's end makes an artifact of in the folder.
LOGO = _final_response("Here is the logo.\n\n```svg\n<svg xmlns=\"http://www.w3.org/2000/svg\"><circle r=\"4\"/></svg>\n```\n")


# -- a turn's open, at its start ----------------------------------------------------------------------------------


async def test_each_turn_opens_its_threads_copy_at_its_start_before_its_prompt_or_any_step_reaches_its_computer(api, computer, monkeypatch):
    (computer.folder / "AGENTS.md").write_text("Instructions v1\n")
    _, _, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))

    async def you_save(harness) -> None:
        # You save two files while the thread's turn runs: within a turn the thread sees only its own changes.
        (computer.folder / "Report.docx").write_bytes(b"PK report v2, by you")
        (computer.folder / "AGENTS.md").write_text("Instructions v2, by you\n")

    since = await database_now(api)
    first = await woken(api, monkeypatch, thread, [
        calling(("read_file", {"path": "Report.docx"})),
        calling(("memory", {"action": "add", "content": "The report is the user's."})),
        calling(("read_file", {"path": "Report.docx"})),
        _final_response("Read it."),
    ], during=you_save)
    assert (copy / "Report.docx").read_bytes() == b"PK report v1" and "Instructions v1" in first._prompt.folder_context
    # The prompt's context, the turn's mark and every step reached the computer after the turn's open was answered.
    assert {"resolve", "read", "write"} <= set(sent_after_the_open(await journal_of(api, thread, since=since)))
    ended = await turn_end(api, thread)
    since = await database_now(api)
    second = await woken(api, monkeypatch, thread, [calling(("read_file", {"path": "Report.docx"})), _final_response("Read it.")], said="Again.")
    # Its next turn starts from the folder as it is now: its files, and the instructions its prompt carries.
    assert (copy / "Report.docx").read_bytes() == b"PK report v2, by you"
    assert "Instructions v2, by you" in second._prompt.folder_context
    sent_after_the_open(await journal_of(api, thread, since=since))
    # Once a turn, under the turn's own name.
    assert asked(computer, "history") == [("open:0", "open"), (f"open:{ended}", "open")]
    assert await opens_of(api, thread) == [("open:0", "ok"), (f"open:{ended}", "ok")]
    assert await status_of(api, thread) == "completed"


async def test_a_turn_that_calls_no_tool_writes_its_artifact_in_the_copy_only_after_its_open(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    folder = picture(computer.folder)
    since = await database_now(api)
    await woken(api, monkeypatch, thread, [LOGO], said="Draw the logo.")
    artifacts = computer.app.places.copy(str(thread.id)) / ".surogates-results" / "artifacts"
    assert list(artifacts.rglob("v1.json")), "no artifact was made"
    assert "write" in sent_after_the_open(await journal_of(api, thread, since=since))
    assert picture(computer.folder) == folder and await steps_of(api, thread) == []
    assert asked(computer, "history") == [("open:0", "open")]


@pytest.mark.parametrize("code", ["no_answer", "no_whole_copy", "move_unfinished", "record_unfinished"])
async def test_an_open_refused_in_a_way_asking_again_passes_is_asked_again_at_once_and_nothing_goes_before_its_answer(api, computer, monkeypatch, code):
    _, _, thread = await begun_with_copy(api, computer)
    refusing_opens(computer, 2, {"type": "history", "code": code, "message": "Not now"})
    since = await database_now(api)
    await woken(api, monkeypatch, thread, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("The totals are in Budget.xlsx."),
    ])
    assert await opens_of(api, thread) == [("open:0", "history"), ("open:0:1", "history"), ("open:0:2", "ok")]
    sent_after_the_open(await journal_of(api, thread, since=since))
    assert (computer.app.places.copy(str(thread.id)) / "Budget.xlsx").read_text() == "Total,42\n"
    assert not (computer.folder / "Budget.xlsx").exists() and await status_of(api, thread) == "completed"


async def test_a_turn_whose_open_is_never_answered_asks_it_a_bounded_number_of_times_then_fails_before_its_prompt(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    refusing_opens(computer, 100)
    worker = await woken(api, monkeypatch, thread, [WRITES])
    # Three askings, then the turn ends: its model is never asked, and nothing else reaches the computer.
    assert await opens_of(api, thread) == [(f"open:0{f':{n}' if n else ''}", "history") for n in range(OPEN_TRIES)]
    assert worker.model_asked == 0 and [kind for kind, _ in await operations_of(api, thread)] == ["bind", *["history"] * OPEN_TRIES]
    failed = await failure(api, thread)
    assert (failed["reason"], failed["why"], failed["code"], failed["error_title"], failed["retryable"]) == (
        "nowhere_to_work", "refused", "no_answer", NOWHERE["refused"], True,
    )
    assert await status_of(api, thread) == "failed"


async def test_a_turns_open_waits_for_a_computer_that_is_away_and_the_whole_turn_waits_with_it(api, computer, monkeypatch):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    _, _, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))
    await computer.app.disconnect()
    worker = a_worker(api, monkeypatch, thread, [calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"}))])
    turn = asyncio.create_task(worker.wake(thread.id))

    async def waiting() -> bool:
        return await opens_of(api, thread) == [("open:0", "open")]

    await eventually(waiting)
    await asyncio.sleep(0.3)
    # Nothing else of the turn went: no prompt read, no model asked, no step.
    assert not turn.done() and worker.model_asked == 0 and await operations_of(api, thread) == [("bind", "bind"), ("history", "open:0")]
    await computer.app.connect()
    await asyncio.wait_for(turn, 30)
    assert asked(computer, "history") == [("open:0", "open")]
    assert (copy / "Budget.xlsx").read_text() == "Total,42\n" and not (computer.folder / "Budget.xlsx").exists()


async def test_a_turn_whose_worker_was_killed_in_its_open_waits_for_that_asking_and_its_computer_hears_it_once(api, computer, monkeypatch):
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    _, _, thread = await begun_with_copy(api, computer)
    await computer.app.disconnect()
    # As the worker recorded it before it waited, and was killed with nothing run after; its computer has it, unanswered.
    await journal(api)._record(OperationRequest(
        device_id=UUID(computer.device_id), root_session_id=thread.id, calling_session_id=thread.id,
        invocation_id="open:0", ordinal=1, kind="history", args={"action": "open"},
    ))
    held, let_go = holding_opens(computer, thread)
    await computer.app.connect()

    async def holds() -> bool:
        return bool(held)

    await eventually(holds)
    worker = a_worker(api, monkeypatch, thread, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"}))])
    turn = asyncio.create_task(worker.wake(thread.id))
    await asyncio.sleep(0.5)
    # The next worker's turn waits for that asking: nothing else of it goes before its answer.
    assert not turn.done() and worker.model_asked == 0 and await operations_of(api, thread) == [("bind", "bind"), ("history", "open:0")]
    await let_go()
    await asyncio.wait_for(turn, 30)
    assert asked(computer, "history") == [("open:0", "open")] and await opens_of(api, thread) == [("open:0", "ok")]
    assert (computer.app.places.copy(str(thread.id)) / "Notes.md").read_text() == "notes\n"


async def test_a_call_a_worker_left_unanswered_is_resumed_only_after_the_turns_open(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    store = api.app.state.session_store
    # The worker that began the turn answered its model's call with a tool.call, and stopped.
    call = calling(("write_file", {"path": "Notes.md", "content": "notes\n"}))[0]["tool_calls"][0]
    await store.emit_event(thread.id, EventType.LLM_RESPONSE, {"message": {"role": "assistant", "content": "", "tool_calls": [call]}})
    await store.emit_event(thread.id, EventType.TOOL_CALL, {"tool_call_id": call["id"], "name": "write_file", "arguments": {}})
    worker = a_worker(api, monkeypatch, thread, [])
    resume = worker._resume_unanswered_calls
    seen: list = []

    async def resumed(*args, **kwargs):
        seen.append(await opens_of(api, thread))
        return await resume(*args, **kwargs)

    worker._resume_unanswered_calls = resumed
    await worker.wake(thread.id)
    # The resume began with the turn's open answered, as the prompt and every step did after it.
    assert seen == [[("open:0", "ok")]]


async def test_a_helper_a_thread_delegates_to_works_only_after_the_threads_open_and_opens_none(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    store = api.app.state.session_store
    await store.update_session_config_key(thread.id, "coordinator", True)  # offered the tools that start helpers
    thread = await store.get_session(thread.id)
    monkeypatch.setattr(delegate_module, "_poll_child_completion", AsyncMock(return_value={"status": "failed", "reason": "not run here"}))
    since = await database_now(api)
    await woken(api, monkeypatch, thread, [calling(("delegate_task", {"goal": "Write the notes."})), _final_response("Started.")])
    [helper] = await children_of(api, thread)
    await woken(api, monkeypatch, helper, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted.")])
    assert (computer.app.places.copy(str(thread.id)) / "Notes.md").read_text() == "notes\n"
    # Its operations came after the thread's open was answered, and it asked none of its own.
    sent_after_the_open(await journal_of(api, thread, since=since))
    assert await opens_of(api, helper) == [] and asked(computer, "history") == [("open:0", "open")]


# -- a Stop -------------------------------------------------------------------------------------------------------


async def stopped_while_its_open_waits(api, computer, monkeypatch, stop):
    """A thread's wake whose open waits for its computer, away, when *stop* (with the worker) stops it; the thread and the worker."""
    monkeypatch.setattr(operations_module, "WAIT_GRACE_S", 0.1)
    _, _, thread = await begun_with_copy(api, computer)
    await computer.app.disconnect()
    worker = a_worker(api, monkeypatch, thread, [WRITES])
    turn = asyncio.create_task(worker.wake(thread.id))

    async def waiting() -> bool:
        return await opens_of(api, thread) == [("open:0", "open")]

    await eventually(waiting)
    await stop(thread, worker)
    # Ended promptly, though its computer is away.
    await asyncio.wait_for(turn, 5)
    return thread, worker


async def test_a_stop_by_its_user_while_a_turns_open_waits_closes_the_open_and_ends_the_turn_with_nothing_more_sent(api, computer, monkeypatch):
    async def by_its_user(thread, worker) -> None:
        # The pause route closes the open: the turn reads that, interrupt or none.
        assert (await api.client.post(f"/v1/sessions/{thread.id}/pause", headers=api.auth())).status_code == 200

    thread, worker = await stopped_while_its_open_waits(api, computer, monkeypatch, by_its_user)
    assert await opens_of(api, thread) == [("open:0", "cancelled")]
    assert await operations_of(api, thread) == [("bind", "bind"), ("history", "open:0")] and worker.model_asked == 0
    assert await status_of(api, thread) == "paused" and await lease_let_go(api, thread)
    # Back, its computer is sent nothing of the stopped turn.
    await computer.app.connect()
    await asyncio.sleep(0.3)
    assert asked(computer) == []


async def test_a_stop_that_leaves_device_operations_alone_still_ends_a_turns_open_that_waits(api, computer, monkeypatch):
    async def by_its_master(thread, worker) -> None:
        # As the project's master stops a thread: the thread paused, its operations left as they are, its turn interrupted.
        state = api.app.state
        await stop_thread(
            thread, reason="Stopped by its master.", interrupt="stopped by the master", session_store=state.session_store,
            session_factory=state.session_factory, redis=None,
        )
        worker.interrupt("stopped by the master")

    thread, worker = await stopped_while_its_open_waits(api, computer, monkeypatch, by_its_master)
    assert await opens_of(api, thread) == [("open:0", "cancelled")]
    assert await operations_of(api, thread) == [("bind", "bind"), ("history", "open:0")] and worker.model_asked == 0
    assert await status_of(api, thread) == "paused" and await lease_let_go(api, thread)


async def test_a_stop_just_before_a_turns_open_is_asked_records_no_open(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    worker = a_worker(api, monkeypatch, thread, [WRITES])
    name = worker._name_the_turn

    async def stopped_first(session) -> None:
        # The pause lands after the wake read the thread as working, and before its open.
        assert (await api.client.post(f"/v1/sessions/{thread.id}/pause", headers=api.auth())).status_code == 200
        await name(session)

    worker._name_the_turn = stopped_first
    await asyncio.wait_for(worker.wake(thread.id), 10)
    assert await operations_of(api, thread) == [("bind", "bind")] and worker.model_asked == 0
    assert await status_of(api, thread) == "paused" and await lease_let_go(api, thread) and asked(computer) == []


async def test_a_stop_after_a_turns_open_was_answered_ends_the_turn_with_no_step_run(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    worker = a_worker(api, monkeypatch, thread, [WRITES])
    model = loop_module.call_llm_with_retry

    async def stopped_while_it_thinks(**kwargs):
        assert (await api.client.post(f"/v1/sessions/{thread.id}/pause", headers=api.auth())).status_code == 200
        worker.interrupt("stopped by the user")
        return await model(**kwargs)

    monkeypatch.setattr(loop_module, "call_llm_with_retry", stopped_while_it_thinks)
    await asyncio.wait_for(worker.wake(thread.id), 10)
    assert await opens_of(api, thread) == [("open:0", "ok")] and await steps_of(api, thread) == []
    assert await status_of(api, thread) == "paused" and await lease_let_go(api, thread)
    assert not (computer.app.places.copy(str(thread.id)) / "Budget.xlsx").exists()


# -- a thread with nowhere to work --------------------------------------------------------------------------------


async def test_a_folder_with_no_history_fails_every_turn_of_its_thread_before_its_prompt_with_nothing_sent(api, computer, monkeypatch):
    # A file whose name history cannot keep: the folder's own history finds it has none.
    os.close(os.open(os.fsencode(computer.folder) + b"/caf\xe9.txt", os.O_CREAT | os.O_WRONLY, 0o644))
    _, _, thread = await begun_with_copy(api, computer)
    folder, place = picture(computer.folder), picture(computer.app.places.place)
    first = await woken(api, monkeypatch, thread, [WRITES])
    ended = await turn_end(api, thread)
    # A turn that would call no tool fails the same: its prompt is not built, and its model is not asked.
    second = await woken(api, monkeypatch, thread, [LOGO], said="Draw the logo.")
    assert (first.model_asked, second.model_asked) == (0, 0)
    assert picture(computer.folder) == folder and picture(computer.app.places.place) == place
    assert not computer.app.places.copy(str(thread.id)).exists() and await steps_of(api, thread) == []
    assert await operations_of(api, thread) == [("bind", "bind"), ("history", "open:0"), ("history", f"open:{ended}")]
    failed = await failure(api, thread)
    assert (failed["reason"], failed["why"], failed["error_title"], failed["retryable"]) == ("nowhere_to_work", "names", NOWHERE["names"], False)
    assert await status_of(api, thread) == "failed"


async def test_an_app_that_keeps_no_copy_for_the_thread_gets_nothing_of_its_turn_not_even_its_artifact(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    copy = computer.app.places.copy(str(thread.id))
    await woken(api, monkeypatch, thread, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted.")])
    assert (copy / "Notes.md").read_text() == "notes\n"
    # Its computer's app now has the thread bound to the folder itself, as an app older than copies binds it.
    del computer.app.places.threads[str(thread.id)]
    folder, kept, before = picture(computer.folder), picture(copy), await operations_of(api, thread)
    turn = await turn_end(api, thread)
    worker = await woken(api, monkeypatch, thread, [LOGO], said="Draw the logo.")
    # Nothing of the turn reached the folder, nor the copy: its computer was asked the turn's open alone.
    assert picture(computer.folder) == folder and picture(copy) == kept and worker.model_asked == 0
    assert (await operations_of(api, thread))[len(before):] == [("history", f"open:{turn}")]
    failed = await failure(api, thread)
    assert (failed["reason"], failed["why"], failed["code"], failed["error_title"], failed["retryable"]) == (
        "nowhere_to_work", NO_COPY, NO_COPY, NOWHERE[NO_COPY], False,
    )


async def test_a_folder_too_large_to_copy_in_time_has_its_threads_turn_fail_saying_why_once_its_app_says_so(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    copied = [0]
    too_large = {"error": {
        "type": "history_off",
        "message": "This folder is too large for a thread of the project to have a copy of its own on this computer: its copy could "
                   "not be made within the time a copy may take, twice. Choose a folder inside it that holds less",
    }}

    def cut_twice(frame, outcome):
        # As the app answers a turn's open: two makings of the copy cut short by their bound, then no copy for the thread.
        if frame["args"].get("action") != "open":
            return outcome
        copied[0] += 1
        return {"error": {"type": "history", "code": "no_answer", "message": "Cut short"}} if copied[0] <= 2 else too_large

    computer.app.lie = cut_twice
    worker = await woken(api, monkeypatch, thread, [WRITES])
    assert worker.model_asked == 0 and await steps_of(api, thread) == []
    assert [name for name, _ in await opens_of(api, thread)] == ["open:0", "open:0:1", "open:0:2"]
    failed = await failure(api, thread)
    assert (failed["why"], failed["code"], failed["error_title"], failed["retryable"]) == (
        "history_off", "history_off", NOWHERE["history_off"], False,
    )


@pytest.mark.parametrize(("case", "lie", "why", "code"), [
    ("a history that refuses the project's", {"error": {"type": "history", "code": "history_refused", "message": "no"}}, "refused", "history_refused"),
    ("what is no answer", {"ok": {"copy": "/etc", "session": "another"}}, "refused", "not_an_answer"),
    ("a folder no longer there", {"error": {"type": "folder_unavailable", "message": "The folder is gone"}}, "refused", "folder_unavailable"),
])
async def test_an_open_refused_in_a_way_asking_again_would_not_pass_fails_the_turn_before_its_prompt(
    api, computer, monkeypatch, case, lie, why, code,
):
    _, _, thread = await begun_with_copy(api, computer)
    computer.app.lie = lambda frame, outcome: lie if frame["args"].get("action") == "open" else outcome
    folder = picture(computer.folder)
    worker = await woken(api, monkeypatch, thread, [WRITES])
    assert worker.model_asked == 0 and await steps_of(api, thread) == [], case
    assert picture(computer.folder) == folder and asked(computer) == [("open:0", "open")]
    failed = await failure(api, thread)
    assert (failed["why"], failed["code"], failed["error_title"], failed["retryable"]) == (why, code, NOWHERE[why], True)


# -- only a thread's own turn opens -------------------------------------------------------------------------------


async def test_only_a_threads_own_turn_opens_its_copy_and_no_other_sessions_turn_waits_for_it(api, computer, monkeypatch):
    _, _, thread = await begun_with_copy(api, computer)
    held, let_go = holding_opens(computer, thread)
    worker = a_worker(api, monkeypatch, thread, [calling(("read_file", {"path": "Report.docx"}))])
    opening = asyncio.create_task(worker.wake(thread.id))

    async def holds() -> bool:
        return bool(held)

    await eventually(holds)
    # Its helper's turn works in the thread's copy, opens none, and waits for no open of the thread's.
    store = api.app.state.session_store
    helper = await create_child_session(store=store, parent=thread, channel="worker")
    await asyncio.wait_for(woken(api, monkeypatch, helper, [
        calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted."),
    ], said="Write the notes."), 30)
    assert (computer.app.places.copy(str(thread.id)) / "Notes.md").read_text() == "notes\n"
    # Another thread on the folder opens its own copy, under its own turn, and works in it.
    _, _, other = await begun_with_copy(api, computer, nonce="second-thread-nonce-0002")
    await asyncio.wait_for(woken(api, monkeypatch, other, [
        calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("Done."),
    ]), 30)
    assert (computer.app.places.copy(str(other.id)) / "Budget.xlsx").read_text() == "Total,42\n"
    assert not opening.done() and worker.model_asked == 0
    await let_go()
    await asyncio.wait_for(opening, 30)
    # Each thread opened its own, as itself; no session under one, nor any other, asked any.
    async with api.app.state.session_factory() as db:
        opens = (await db.execute(text(
            "SELECT calling_session_id, root_session_id, invocation_id FROM device_operations WHERE kind = 'history'"
            " AND root_session_id IN (:a, :b) ORDER BY created_at"
        ), {"a": thread.id, "b": other.id})).all()
    assert [tuple(row) for row in opens] == [(thread.id, thread.id, "open:0"), (other.id, other.id, "open:0")]
    assert not (computer.folder / "Notes.md").exists() and not (computer.folder / "Budget.xlsx").exists()


async def test_a_thread_or_a_chat_bound_to_its_folder_itself_asks_no_open_and_works_there_as_before(api, laptop, monkeypatch):
    store = api.app.state.session_store
    _, _, thread = await begun_local(api, laptop)
    await woken(api, monkeypatch, thread, [calling(("write_file", {"path": "Budget.xlsx", "content": "Total,42\n"})), _final_response("Done.")])
    laptop.app.prepare("chat-binding-nonce-0001", FOLDER)
    created = await api.client.post(
        "/v1/sessions", json={"execution": confirmed(laptop.device_id, nonce="chat-binding-nonce-0001")}, headers=api.auth(),
    )
    assert created.status_code == 201, created.text
    await eventually(lambda: is_bound(api, created.json()["id"]))
    chat = await store.get_session(UUID(created.json()["id"]))
    await woken(api, monkeypatch, chat, [calling(("write_file", {"path": "Notes.md", "content": "notes\n"})), _final_response("Noted.")], said="Note it.")
    for session, name, data in ((thread, "Budget.xlsx", "Total,42\n"), (chat, "Notes.md", "notes\n")):
        assert (laptop.folder / name).read_text() == data
        assert (await store.get_session(session.id)).status == "completed"
        # Its computer was asked none of a thread's own kinds, and nothing of its turn waited for one.
        assert [kind for kind, _ in await operations_of(api, session) if kind in THREAD_KINDS] == []
    assert not set(laptop.app.ran) & THREAD_KINDS


# -- the open asked alone -----------------------------------------------------------------------------------------


async def test_a_worker_that_holds_its_threads_lease_no_more_records_no_open(api, computer):
    _, _, thread = await begun_with_copy(api, computer)
    store, factory, redis = api.app.state.session_store, api.app.state.session_factory, api.app.state.redis
    lost = await store.try_acquire_lease(thread.id, "worker-a", ttl_seconds=60)
    await store.release_lease(thread.id, lost.lease_token)
    taker = await store.try_acquire_lease(thread.id, "worker-b", ttl_seconds=60)
    with pytest.raises(DeviceOperationError, match="Another worker runs this session now"):
        await _open_local_copy(thread, store, lost, session_factory=factory, redis=redis)
    assert await opens_of(api, thread) == [] and asked(computer) == []
    # Asked per step by a worker that holds the lease, it answers the turn's recorded open, and its computer hears it once.
    first = await _open_local_copy(thread, store, taker, session_factory=factory, redis=redis)
    again = await _open_local_copy(thread, store, taker, session_factory=factory, redis=redis)
    assert first[1:] == again[1:] == (0, {"copy": "made"}) and asked(computer) == [("open:0", "open")]
    await store.release_lease(thread.id, taker.lease_token)


async def test_a_paused_threads_open_and_its_files_and_commands_reach_its_computer_none_of_them(api, computer):
    _, _, thread = await begun_with_copy(api, computer)
    await api.app.state.session_store.update_session_status(thread.id, "paused")
    copy = thread_copy(thread, session_factory=api.app.state.session_factory, redis=api.app.state.redis, lease_token=None)
    # An open is a turn's new work: none is recorded for a thread its Stop paused, so none waits on after it.
    with pytest.raises(DeviceOperationError, match="This session was stopped"):
        await asyncio.wait_for(copy.opened(0), 30)
    for kind, args in [("write", {"key": f"{computer.folder}/a.txt", "data": ""}), ("run", {"command": "ls"})]:
        with pytest.raises(DeviceOperationError, match="This session was stopped"):
            await journal(api).run(OperationRequest(
                device_id=UUID(computer.device_id), root_session_id=thread.id, calling_session_id=thread.id,
                invocation_id="17:call_1", ordinal=1, kind=kind, args=args,
            ))
    assert asked(computer) == [] and await operations_of(api, thread) == [("bind", "bind")]
    assert not computer.app.places.copy(str(thread.id)).exists()
