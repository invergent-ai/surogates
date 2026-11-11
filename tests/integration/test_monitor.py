"""``monitor``: each line a background command prints reaches its session as a notification, at a bounded rate."""

from __future__ import annotations

import asyncio
import json
import time
from uuid import UUID

import pytest
import pytest_asyncio

from surogates.harness import background_watch as watch_module
from surogates.harness.agent_news import notification_note
from surogates.harness.background_watch import HERE, BackgroundWatch
from surogates.session.events import EventType
from surogates.tools.builtin import agents as agents_module
from tests.test_steer_loop import _final_response

from .test_agent_tool import PARITY, a_chat, calling, chats  # noqa: F401  (chats is a fixture)
from .test_background_completions import Daemon, daemon_registry  # noqa: F401  (a fixture)
from .test_command_wake_once import workers  # noqa: F401  (a fixture)
from .test_devices import api  # noqa: F401  (a fixture)
from .test_workstream_threads import queued

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def notes(store, session) -> list[dict]:
    return [e.data for e in await store.get_events(session, types=[EventType.AGENT_NOTIFICATION])]


async def until(check, seconds: float = 20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = await check()
        if found:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError("never happened")


@pytest_asyncio.fixture(loop_scope="session")
async def here_watch(api, monkeypatch):  # noqa: F811
    watch = BackgroundWatch(poll_seconds=0.05)
    watch.configure(store=api.app.state.session_store, redis=api.app.state.redis, sandbox_pool=None)
    monkeypatch.setattr(watch_module, "background_watch", watch)
    yield watch
    await watch.close()


async def start(api, session, command, **arguments):  # noqa: F811
    return json.loads(await agents_module._monitor_handler(
        {"command": command, "description": "Watch the ticks", **arguments},
        session_store=api.app.state.session_store, session_id=str(session),
    ))


async def a_session(api) -> UUID:  # noqa: F811
    created = await api.client.post("/v1/sessions", json={"config": PARITY}, headers=api.auth())
    return UUID(created.json()["id"])


async def test_each_line_reaches_the_session_then_the_end(api, here_watch):  # noqa: F811
    store = api.app.state.session_store
    chat = await a_session(api)
    started = await start(api, chat, "echo first; sleep 0.5; echo second; sleep 0.5; echo third; exit 3")
    assert started["status"] == "watching" and started["monitor_id"].startswith("proc_")
    ended = await until(lambda: notes_until_end(store, chat))
    outputs = [n for n in ended if n["status"] == "output"]
    said = [line for n in ended for line in n["result"].splitlines()]
    assert said == ["first", "second", "third"]
    assert outputs and all(n["kind"] == "monitor" and n["summary"] == "Watch the ticks" for n in ended)
    end = ended[-1]
    assert end["status"] == "failed" and end["exit_code"] == 3
    assert notification_note(end)["content"].startswith(f"[Monitor {started['monitor_id']} (Watch the ticks) ended: its command exited with code 3")
    assert notification_note(outputs[0])["content"].startswith(f"[Monitor {started['monitor_id']} (Watch the ticks): 1 new line]\n```\nfirst\n```")
    assert await queued(api, await store.get_session(chat))


async def notes_until_end(store, chat):
    found = await notes(store, chat)
    return found if found and found[-1]["status"] != "output" else None


async def test_lines_past_the_rate_wait_for_a_later_notification(api, here_watch):  # noqa: F811
    store = api.app.state.session_store
    chat = await a_session(api)
    started = await start(
        api, chat, "for i in 1 2 3 4 5; do echo tick $i; sleep 0.3; done", max_notifications_per_minute=1,
    )
    ended = await until(lambda: notes_until_end(store, chat))
    # One notification of lines in the minute; the rest came with its end, none lost.
    assert [n["status"] for n in ended] == ["output", "completed"]
    assert [line for n in ended for line in n["result"].splitlines()] == [f"tick {i}" for i in range(1, 6)]
    assert started["monitor_id"] == ended[-1]["agent_id"]


async def test_a_monitor_past_its_timeout_is_killed_and_says_so(api, here_watch):  # noqa: F811
    store = api.app.state.session_store
    chat = await a_session(api)
    from surogates.tools.workspace_io.local import LocalWorkspaceIO

    started = await LocalWorkspaceIO(workspace_path=None).start(
        "while true; do echo tick; sleep 0.2; done", workdir=None, task_id="t", pty=False,
        notify_on_complete=True, watcher_interval=None, watch_lines=True,
    )
    here_watch.watch(owner=HERE, proc_id=started["session_id"], session_id=chat, command="ticks",
                     monitor=("Watch the ticks", 0.8, 30))
    ended = await until(lambda: notes_until_end(store, chat))
    assert ended[-1]["status"] == "killed" and ended[-1]["reason"] == "its timeout passed"
    assert notification_note(ended[-1])["content"].startswith(
        f"[Monitor {started['session_id']} (Watch the ticks) stopped: its timeout passed",
    )
    from surogates.tools.utils.process_registry import process_registry

    await until(lambda: asyncio.sleep(0, result=process_registry.poll(started["session_id"])["status"] != "running"))


async def test_stop_agent_ends_a_monitor(api, here_watch):  # noqa: F811
    store = api.app.state.session_store
    chat = await a_session(api)
    started = await start(api, chat, "while true; do echo tick; sleep 0.3; done")
    stopped = json.loads(await agents_module._stop_agent_handler(
        {"agent_id": started["monitor_id"]}, session_store=store, session_id=str(chat),
    ))
    assert stopped["agent_id"] == started["monitor_id"]
    ended = await until(lambda: notes_until_end(store, chat))
    assert ended[-1]["status"] == "killed"


async def test_a_chat_is_offered_monitor_and_woken_by_its_lines(chats, daemon_registry, tmp_path, monkeypatch):  # noqa: F811
    from surogates.harness.background_watch import background_watch
    from surogates.sandbox.pool import SandboxPool

    daemon = Daemon(tmp_path)
    chats.sandbox_pool = SandboxPool(daemon)
    monkeypatch.setattr(background_watch, "_poll_seconds", 0.05)
    background_watch.configure(store=chats.store, redis=chats.api.app.state.redis, sandbox_pool=chats.sandbox_pool)
    from .test_agent_tool import REAL

    monkeypatch.setattr("tests.integration.test_agent_tool.REAL", REAL | {"monitor"})
    try:
        chat = await a_chat(chats, PARITY)
        chats.replies = [
            calling("monitor", "call_mon", command="sleep 1; echo deploy finished", description="Wait for the deploy"),
            _final_response("Watching the deploy."),
        ]
        await chats.says(chat, "Tell me when the deploy is done.")
        await chats.wake(chat)
        assert "monitor" in chats.offered[0]
        ended = await until(lambda: notes_until_end(chats.store, chat))
        assert [line for n in ended for line in n["result"].splitlines()] == ["deploy finished"]
        before = len(chats.requests)
        chats.replies = [_final_response("The deploy is done.")]
        await chats.wake(chat)
        request = chats.requests[before]
        assert [m for m in request if m["role"] == "user"][-len(ended):] == [notification_note(n) for n in ended]
    finally:
        await background_watch.close()
        background_watch.configure(store=None, redis=None, sandbox_pool=None)
