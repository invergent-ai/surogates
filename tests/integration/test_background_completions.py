"""A background command the agent asked to hear the end of reaches it as a notification.

``terminal(background=true, notify_on_complete=true)`` returns at once.  The
command runs on in the sandbox's executor daemon, which outlives the call;
the worker's watch asks the daemon for its end and writes it to the session
that started it, which reads it at its next request, or, at rest, is woken
by it.  Here the daemon is the real executor code run in this process, over
a temporary workspace.
"""

from __future__ import annotations

import asyncio
import json
import time
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from surogates.harness import background_watch as watch_module
from surogates.harness.agent_news import notification_note
from surogates.harness.background_watch import HERE, BackgroundWatch, background_watch
from surogates.sandbox import executor_server
from surogates.sandbox.base import SandboxStatus
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.tools.builtin import terminal
from surogates.tools.registry import ToolRegistry
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.test_steer_loop import _final_response

from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_workstream_threads import queued

pytestmark = pytest.mark.asyncio(loop_scope="session")

QUICK = 0.05


class Daemon:
    """A sandbox backend whose sandboxes are the executor daemon's code, run here: a call the daemon keeps
    runs on a thread, any other as the daemon's fork would, and both see this process's registry."""

    def __init__(self, workspace) -> None:
        self.workspace = str(workspace)
        self.provisioned: list[str] = []
        self.destroyed: list[str] = []

    async def provision(self, spec) -> str:
        sandbox_id = uuid4().hex
        self.provisioned.append(sandbox_id)
        return sandbox_id

    async def status(self, sandbox_id: str) -> SandboxStatus:
        return SandboxStatus.STOPPED if sandbox_id in self.destroyed else SandboxStatus.RUNNING

    async def destroy(self, sandbox_id: str) -> None:
        self.destroyed.append(sandbox_id)

    async def execute(self, sandbox_id: str, name: str, input: str, *, timeout: float | None = None) -> str:
        assert sandbox_id not in self.destroyed, "a call reached a sandbox that was taken down"
        args = json.loads(input) if input else {}
        args.pop("_trace_context", None)
        return await asyncio.to_thread(executor_server.run_tool, name, args, self.workspace, None, None)


@pytest.fixture()
def daemon_registry(monkeypatch):
    registry = ToolRegistry()
    terminal.register(registry)
    monkeypatch.setattr(executor_server, "_REGISTRY", registry)
    return registry


@pytest_asyncio.fixture(loop_scope="session")
async def watched(workers, daemon_registry, tmp_path, monkeypatch):  # noqa: F811
    """The worker's watch, polling quickly, over a pool of daemon sandboxes: the pool the chats' turns use."""
    daemon = Daemon(tmp_path)
    workers.sandbox_pool = SandboxPool(daemon)
    monkeypatch.setattr(background_watch, "_poll_seconds", QUICK)
    background_watch.configure(store=workers.store, redis=workers.api.app.state.redis, sandbox_pool=workers.sandbox_pool)
    yield workers, daemon
    await background_watch.close()
    background_watch.configure(store=None, redis=None, sandbox_pool=None)


def call(name: str, call_id: str, **arguments) -> tuple[dict, dict]:
    """A reply of the model's that calls *name* once."""
    return (
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]},
        {"model": "test-model", "finish_reason": "tool_calls", "input_tokens": 1, "output_tokens": 1},
    )


async def notifications(store, chat: UUID) -> list[dict]:
    return [event.data for event in await store.get_events(chat, types=[EventType.AGENT_NOTIFICATION])]


async def until(check, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = await check()
        if found:
            return found
        await asyncio.sleep(QUICK)
    raise AssertionError("never happened")


def started_proc(request: list[dict]) -> str:
    """The process id the last ``terminal`` result in *request* names."""
    result = next(m for m in reversed(request) if m.get("role") == "tool")
    return json.loads(result["content"])["session_id"]


async def test_a_command_that_ends_after_its_turn_wakes_the_chat_with_its_end(watched):
    chats, daemon = watched
    chat = await chats.chat(talk=())
    command = "echo compiling; sleep 3; echo built; exit 2"
    chats.replies = [
        call("terminal", "call_bg", command=command, background=True, notify_on_complete=True),
        _final_response("The build runs in the background."),
    ]

    async def at_the_first_request():
        async def at_the_second_request():
            # The model asks ``process`` about the command it started: the daemon has it.
            proc_id = started_proc(chats.requests[-1])
            chats.replies.insert(0, call("process", "call_poll", action="poll", session_id=proc_id))

        chats.during_the_request = at_the_second_request

    chats.during_the_request = at_the_first_request
    await chats.says(chat, "Build it in the background, and tell me when it is done.")
    await chats.wake(chat)

    polled = json.loads(next(m for m in chats.requests[-1] if m.get("tool_call_id") == "call_poll")["content"])
    assert polled["status"] == "running"
    # The turn has ended, the command runs on: its sandbox stays up.
    assert await chats.status(chat) == "completed"
    assert daemon.destroyed == []

    [ended] = await until(lambda: notifications(chats.store, chat))
    assert ended["kind"] == "process" and ended["status"] == "failed" and ended["exit_code"] == 2
    assert ended["summary"] == command and "built" in ended["result"]
    assert await queued(chats.api, await chats.session(chat))
    # The sandbox is kept for the turn the end gives: what the command left there is still to be read.
    assert daemon.destroyed == []

    before = len(chats.requests)
    chats.replies = [
        call("process", "call_log", action="log", session_id=ended["agent_id"]),
        _final_response("The build failed with exit code 2."),
    ]
    await chats.wake(chat)
    request = chats.requests[before]
    assert request[-1] == notification_note(ended)
    assert f"<summary>Background process {ended['agent_id']} finished with exit code 2: {command}</summary>" in request[-1]["content"]
    log = json.loads(next(m for m in chats.requests[-1] if m.get("tool_call_id") == "call_log")["content"])
    assert "compiling" in log["output"] and "built" in log["output"]
    # Once that turn has ended and nothing of the chat runs, the sandbox goes.
    await until(lambda: asyncio.sleep(0, result=daemon.destroyed == daemon.provisioned))
    assert not background_watch.holds(str(chat))
    # Its next turn rebuilds that request from the log, byte for byte.
    await chats.says(chat, "Thanks.")
    await chats.wake(chat)
    assert chats.requests[-1][: len(request)] == request


async def test_an_end_that_lands_during_a_turn_is_read_at_its_next_request_and_replayed_alike(watched):
    chats, daemon = watched
    chat = await chats.chat(talk=())
    chats.replies = [
        call("terminal", "call_bg", command="sleep 0.2; echo ready", background=True, notify_on_complete=True),
        # A foreground command long enough for the background one to end meanwhile.
        call("terminal", "call_wait", command="sleep 1.5; echo waited"),
        _final_response("It is ready."),
    ]
    await chats.says(chat, "Start the server and wait for it.")
    await chats.wake(chat)

    [ended] = await notifications(chats.store, chat)
    assert ended["status"] == "completed" and ended["exit_code"] == 0
    third = chats.requests[-1]
    # The request after the waiting call reads the end, after that call's result.
    assert third[-2]["tool_call_id"] == "call_wait"
    assert third[-1] == notification_note(ended)
    # The turn read it: the chat rests, nobody is woken for it again.
    assert await chats.status(chat) == "completed"
    await chats.says(chat, "And now?")
    await chats.wake(chat)
    assert chats.requests[-1][: len(third)] == third
    # Once its turns have ended the sandbox goes: nothing is watched, nothing kept.
    await until(lambda: asyncio.sleep(0, result=not background_watch.holds(str(chat))))
    await until(lambda: asyncio.sleep(0, result=daemon.destroyed == daemon.provisioned))


async def test_a_session_hears_only_the_ends_of_its_own_commands(api):  # noqa: F811
    store, redis = api.app.state.session_store, api.app.state.redis
    created = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    other = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    mine, theirs = UUID(created.json()["id"]), UUID(other.json()["id"])
    watch = BackgroundWatch(poll_seconds=QUICK)
    watch.configure(store=store, redis=redis, sandbox_pool=None)
    wio = LocalWorkspaceIO(workspace_path=None)
    try:
        started = await wio.start(
            "sleep 0.2; exit 0", workdir=None, task_id="t", pty=False, notify_on_complete=True, watcher_interval=None,
        )
        await watch.watch(owner=HERE, proc_id=started["session_id"], session_id=mine, command="sleep 0.2; exit 0")
        [ended] = await until(lambda: notifications(store, mine))
        assert ended["agent_id"] == started["session_id"] and ended["status"] == "completed"
        assert await notifications(store, theirs) == []
    finally:
        await watch.close()


class Pool:
    """A sandbox pool whose drain answers as *answer* says."""

    def __init__(self, answer: str, *, copy: bool = False) -> None:
        self.answer, self.copy, self.asked = answer, copy, 0

    async def execute(self, owner, name, input, *, timeout=None):
        assert name == watch_module.DRAIN
        self.asked += 1
        if self.answer == "went":
            raise ValueError(f"No sandbox provisioned for session {owner}")
        if self.answer == "too-old":
            return json.dumps({"exit_code": 1, "output": "", "error": "Unknown tool: _background"})
        # A transient failure: the client's result for an HTTP error, or a timeout.
        return json.dumps({"exit_code": -1, "stdout": "", "stderr": "Executor daemon error (HTTP 502)"})

    def holds_copy(self, owner):
        return self.copy

    def sandbox_of(self, owner):
        return None

    async def release_for_session(self, owner, *, only=None):
        return None


async def a_session(api) -> UUID:  # noqa: F811
    created = await api.client.post("/v1/sessions", json={}, headers=api.auth())
    return UUID(created.json()["id"])


@pytest.mark.parametrize("sandbox, status, reason, said", [
    ("went", "killed", "its sandbox ended", "was stopped before it finished: make serve (its sandbox ended)"),
    (
        "too-old", "lost", "this sandbox cannot report the ends of background commands; it may still be running",
        "cannot be watched any more: make serve (this sandbox cannot report the ends of background commands; it may still be running)",
    ),
])
async def test_a_command_whose_sandbox_is_gone_or_cannot_tell_is_reported_so(api, sandbox, status, reason, said):  # noqa: F811
    store, redis = api.app.state.session_store, api.app.state.redis
    chat = await a_session(api)
    watch = BackgroundWatch(poll_seconds=QUICK)
    watch.configure(store=store, redis=redis, sandbox_pool=Pool(sandbox))
    try:
        await watch.watch(owner=str(chat), proc_id="proc_gone", session_id=chat, command="make serve")
        [ended] = await until(lambda: notifications(store, chat))
        assert ended == {
            "agent_id": "proc_gone", "kind": "process", "status": status, "summary": "make serve",
            "result": "", "reason": reason, "format": "task-notification",
        }
        assert f"<summary>Background process proc_gone {said}</summary>" in notification_note(ended)["content"]
        # Nothing of it is kept: no sandbox is mapped to it.
        await until(lambda: asyncio.sleep(0, result=not watch.holds(str(chat))))
    finally:
        await watch.close()


async def test_a_sandbox_that_fails_to_answer_a_while_is_no_end_until_it_gives_up(api, monkeypatch):  # noqa: F811
    store, redis = api.app.state.session_store, api.app.state.redis
    chat = await a_session(api)
    monkeypatch.setattr(watch_module, "MAX_FAILED_ASKS", 5)
    pool = Pool("error")
    watch = BackgroundWatch(poll_seconds=QUICK)
    watch.configure(store=store, redis=redis, sandbox_pool=pool)
    try:
        await watch.watch(owner=str(chat), proc_id="proc_x", session_id=chat, command="make serve")
        await until(lambda: asyncio.sleep(0, result=pool.asked >= 3))
        # A few failed asks tell nobody anything.
        assert await notifications(store, chat) == [] and watch.holds(str(chat))
        [ended] = await until(lambda: notifications(store, chat))
        assert pool.asked >= 5
        assert (ended["status"], ended["reason"]) == ("lost", "its sandbox stopped answering; it may still be running")
    finally:
        await watch.close()


async def test_a_command_is_watched_for_at_most_its_limit_then_killed(api, monkeypatch):  # noqa: F811
    store, redis = api.app.state.session_store, api.app.state.redis
    chat = await a_session(api)
    monkeypatch.setattr(watch_module, "MAX_WATCH_SECONDS", 0.3)
    watch = BackgroundWatch(poll_seconds=QUICK)
    watch.configure(store=store, redis=redis, sandbox_pool=None)
    started = await LocalWorkspaceIO(workspace_path=None).start(
        "sleep 30", workdir=None, task_id="t", pty=False, notify_on_complete=True, watcher_interval=None,
    )
    try:
        await watch.watch(owner=HERE, proc_id=started["session_id"], session_id=chat, command="sleep 30")
        [ended] = await until(lambda: notifications(store, chat))
        assert ended["status"] == "killed"
        assert ended["reason"].startswith("it ran longer than 0 minutes")
        from surogates.tools.utils.process_registry import process_registry

        await until(lambda: asyncio.sleep(0, result=process_registry.poll(started["session_id"])["status"] != "running"))
    finally:
        await watch.close()


async def test_a_threads_pod_is_never_kept_past_its_turn(api):  # noqa: F811
    store, redis = api.app.state.session_store, api.app.state.redis
    chat = await a_session(api)
    watch = BackgroundWatch(poll_seconds=60)
    watch.configure(store=store, redis=redis, sandbox_pool=Pool("error", copy=True))
    try:
        await watch.watch(owner=str(chat), proc_id="proc_t", session_id=chat, command="make serve")
        assert not watch.holds(str(chat))
        assert await watch.running_for(chat) == [{"id": "proc_t", "command": "make serve", "status": "running"}]
    finally:
        await watch.close()
