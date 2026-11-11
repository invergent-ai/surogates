"""Background commands and monitors when a session's turns run on more than one worker.

Each worker has its own watch and its own sandbox pool: the sandbox a command
runs in is reachable only from the worker that started it (the executor's
token is that worker's).  The watches share their state in Redis, so a turn of
the same session on another worker lists the command, stops it through the
watch that reads it, counts the session's monitors and their rate, and leaves
the sandbox that watch holds alone; and a watch whose worker is gone has its
commands' sessions told by another worker's sweep.  Here two watches, over two
pools of sandboxes that are the real executor code run in this process.
"""

from __future__ import annotations

import asyncio
import json
import time
from uuid import UUID

import pytest
import pytest_asyncio

from surogates.harness import background_watch as watch_module
from surogates.harness.agent_news import notification_note
from surogates.harness.background_watch import BackgroundWatch
from surogates.sandbox.base import SandboxSpec
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.tools.builtin import agents as agents_module

from .test_agent_tool import PARITY
from .test_background_completions import Daemon, daemon_registry  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def until(check, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = await check()
        if found:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError("never happened")


async def notes(store, session) -> list[dict]:
    return [e.data for e in await store.get_events(session, types=[EventType.AGENT_NOTIFICATION])]


class Worker:
    """One worker's watch and sandbox pool."""

    def __init__(self, api, tmp_path, name: str) -> None:  # noqa: F811
        self.pool = SandboxPool(Daemon(tmp_path / name))
        (tmp_path / name).mkdir()
        self.watch = BackgroundWatch(poll_seconds=0.05)
        self.watch.configure(store=api.app.state.session_store, redis=api.app.state.redis, sandbox_pool=self.pool)
        self.api = api

    async def starts(self, session: UUID, command: str) -> str:
        """Start *command* in this worker's sandbox for *session*, as ``terminal`` does, and watch it."""
        owner = str(session)
        await self.pool.ensure(owner, SandboxSpec(env={}))
        started = json.loads(await self.pool.execute(owner, "terminal", json.dumps({
            "command": command, "background": True, "notify_on_complete": True,
        })))
        await self.watch.watch(owner=owner, proc_id=started["session_id"], session_id=session, command=command)
        return started["session_id"]

    def tool_kwargs(self, session: UUID) -> dict:
        return {"session_store": self.api.app.state.session_store, "session_id": str(session),
                "redis": self.api.app.state.redis, "sandbox_pool": self.pool,
                "session_factory": self.api.app.state.session_factory}


@pytest_asyncio.fixture(loop_scope="session")
async def two_workers(api, daemon_registry, tmp_path, monkeypatch):  # noqa: F811
    # Other tests' commands may sit in the shared keys too: each assertion here reads only this test's sessions.
    first, second = Worker(api, tmp_path, "a"), Worker(api, tmp_path, "b")
    yield first, second
    await first.watch.close()
    await second.watch.close()


async def crashes(worker: Worker) -> None:
    """The worker dies: its tasks stop, and its heartbeat is left to lapse (a crash ends no heartbeat)."""
    tasks = list(worker.watch._tasks.values()) + ([worker.watch._sweeper] if worker.watch._sweeper else [])
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    worker.watch._tasks.clear()


async def a_session(api) -> UUID:  # noqa: F811
    created = await api.client.post("/v1/sessions", json={"config": PARITY}, headers=api.auth())
    return UUID(created.json()["id"])


async def test_a_command_started_on_one_worker_is_listed_and_stopped_from_another(api, two_workers, monkeypatch):  # noqa: F811
    first, second = two_workers
    store = api.app.state.session_store
    chat = await a_session(api)
    proc = await first.starts(chat, "sleep 30")
    # The session's next turn runs on the second worker: it lists the command ...
    monkeypatch.setattr(watch_module, "background_watch", second.watch)
    listed = json.loads(await agents_module._list_agents_handler({}, **second.tool_kwargs(chat)))
    assert [c["id"] for c in listed["background_commands"]] == [proc]
    # ... and stops it: the first worker's watch, which reads it, kills it.
    stopped = json.loads(await agents_module._stop_agent_handler({"agent_id": proc}, **second.tool_kwargs(chat)))
    assert stopped["status"] == "stopping"
    [end] = await until(lambda: notes(store, chat))
    assert end["agent_id"] == proc and end["status"] == "killed"
    assert json.loads(await agents_module._list_agents_handler({}, **second.tool_kwargs(chat)))["background_commands"] == []


async def test_a_stop_from_another_session_is_refused_on_every_worker(api, two_workers, monkeypatch):  # noqa: F811
    first, second = two_workers
    chat, stranger = await a_session(api), await a_session(api)
    proc = await first.starts(chat, "sleep 3")
    monkeypatch.setattr(watch_module, "background_watch", second.watch)
    refused = json.loads(await agents_module._stop_agent_handler({"agent_id": proc}, **second.tool_kwargs(stranger)))
    assert refused["error"].startswith("no background command")
    assert json.loads(await agents_module._list_agents_handler({}, **second.tool_kwargs(stranger)))["background_commands"] == []


async def test_a_worker_is_presumed_gone_only_once_its_heartbeat_has_lapsed_long_enough(api, two_workers, monkeypatch):  # noqa: F811
    first, second = two_workers
    monkeypatch.setattr(watch_module, "ALIVE_SECONDS", 0.3)
    chat = await a_session(api)
    await first.starts(chat, "sleep 1")
    assert await second.watch.worker_alive(first.watch.id)
    # Its heartbeat lost (a Redis restart, say): still alive for a while, as it beats again within a sweep.
    await api.app.state.redis.delete(f"{watch_module.SHARED_PREFIX}:alive:{first.watch.id}")
    assert await second.watch.worker_alive(first.watch.id)
    await asyncio.sleep(0.15)  # it beats again at its next poll
    assert await second.watch.worker_alive(first.watch.id)
    await crashes(first)
    await asyncio.sleep(0.6)
    # Its heartbeat has lapsed: from the first look that finds it missing, the lapse must last a while more.
    assert await second.watch.worker_alive(first.watch.id)
    await asyncio.sleep(0.6)
    assert not await second.watch.worker_alive(first.watch.id)
    assert await second.watch.worker_alive(second.watch.id)


async def test_a_sessions_monitors_are_counted_and_rated_on_every_worker(api, two_workers, monkeypatch):  # noqa: F811
    first, second = two_workers
    store = api.app.state.session_store
    chat = await a_session(api)
    owner = str(chat)
    async def spec(*args, **kwargs):
        return SandboxSpec(env={})

    monkeypatch.setattr("surogates.harness.tool_exec._build_session_sandbox_spec", spec)
    for worker in (first, second):
        await worker.pool.ensure(owner, SandboxSpec(env={}))
    # Two monitors on each worker: the session runs four.
    loud = "for i in $(seq 40); do echo tick $i; sleep 0.05; done"
    monkeypatch.setattr(watch_module, "MAX_NOTES_PER_SESSION_MINUTE", 3)
    started = []
    for worker in (first, first, second, second):
        monkeypatch.setattr(watch_module, "background_watch", worker.watch)
        answer = json.loads(await agents_module._monitor_handler(
            {"command": loud, "description": "Ticks", "max_notifications_per_minute": 30}, **worker.tool_kwargs(chat),
        ))
        assert answer.get("status") == "watching", answer
        started.append(answer["monitor_id"])
    # A fifth is refused on either worker.
    # Each worker's watch counts the four, wherever they run.
    assert await first.watch.monitors_running(chat) == await second.watch.monitors_running(chat) == 4
    refused = json.loads(await agents_module._monitor_handler(
        {"command": "sleep 1", "description": "One more"}, **second.tool_kwargs(chat),
    ))
    assert "monitors already run" in refused["error"]

    async def all_ended():
        found = await notes(store, chat)
        return found if {n["agent_id"] for n in found if n["status"] != "output"} >= set(started) else None

    found = await until(all_ended, 30)
    # Within the minute, three line notes for the session, whichever worker's watch sent them; every line came.
    assert len([n for n in found if n["status"] == "output"]) == 3
    assert sum(n["lines"] + n.get("dropped", 0) for n in found) == 160


async def test_the_commands_of_a_crashed_worker_are_told_by_another_workers_sweep(api, two_workers, monkeypatch):  # noqa: F811
    first, second = two_workers
    monkeypatch.setattr(watch_module, "ALIVE_SECONDS", 0.3)
    store = api.app.state.session_store
    chat = await a_session(api)
    proc = await first.starts(chat, "sleep 30")
    await crashes(first)
    second.watch.start_sweeping(every=0.1)
    [lost] = await until(lambda: notes(store, chat))
    assert lost["agent_id"] == proc and lost["status"] == "lost"
    assert notification_note(lost)["content"].startswith(f"[Background process {proc} cannot be watched any more")
    # Told once: the claim keeps every other sweep from telling it again.
    await asyncio.sleep(0.5)
    assert len(await notes(store, chat)) == 1
    from surogates.tools.utils.process_registry import process_registry

    process_registry.kill_process(proc)


async def test_a_users_stop_on_one_worker_stops_the_sessions_commands_on_another(api, two_workers):  # noqa: F811
    first, second = two_workers
    store = api.app.state.session_store
    chat = await a_session(api)
    proc = await first.starts(chat, "sleep 30")
    # The interrupt or /clear path of the second worker's turn.
    await second.watch.stop_session_elsewhere(chat)
    [end] = await until(lambda: notes(store, chat))
    assert end["agent_id"] == proc and end["status"] == "killed"


async def test_list_and_stop_answer_from_this_worker_when_redis_does_not(api, two_workers, monkeypatch):  # noqa: F811
    first, _ = two_workers
    chat = await a_session(api)
    proc = await first.starts(chat, "sleep 1")

    async def down(*args, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(first.watch._shared, "of_session", down)
    monkeypatch.setattr(first.watch._shared, "entry", down)
    assert [c["id"] for c in await first.watch.running_for(chat)] == [proc]
    assert await first.watch.stop_elsewhere("proc_000000000000", chat) is None


async def test_the_docker_sweep_of_a_session_spares_a_live_worker_and_takes_a_crashed_ones(api, two_workers, monkeypatch):  # noqa: F811
    from tests.test_docker_sandbox import FakeDocker, _backend

    first, second = two_workers
    monkeypatch.setattr(watch_module, "ALIVE_SECONDS", 0.3)
    chat = await a_session(api)
    await first.starts(chat, "sleep 1")  # the first worker beats
    docker = FakeDocker()
    docker._containers["parents"] = {"running": True, "labels": {
        "app": "surogates-sandbox", "surogates.session_id": str(chat), "surogates.worker": first.watch.id,
    }}

    backend = _backend(docker, None)
    # As the worker wires it: its watch's id and liveness.
    backend.claim_as(second.watch.id, second.watch.worker_alive)
    await backend.destroy_for_session(str(chat))
    assert "parents" in docker._containers
    await crashes(first)
    await asyncio.sleep(0.6)
    await backend.destroy_for_session(str(chat))  # the first look starts the lapse
    await asyncio.sleep(0.6)
    await backend.destroy_for_session(str(chat))
    assert "parents" not in docker._containers
    await backend.aclose()
