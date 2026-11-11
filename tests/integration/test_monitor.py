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
    await here_watch.watch(owner=HERE, proc_id=started["session_id"], session_id=chat, command="ticks",
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
    assert notification_note(ended[-1])["content"].startswith(
        f"[Monitor {started['monitor_id']} (Watch the ticks) stopped: its command was killed",
    )


async def test_a_line_with_no_end_comes_in_pieces_of_bounded_size(api, here_watch):  # noqa: F811
    store = api.app.state.session_store
    chat = await a_session(api)
    await start(api, chat, "head -c 20000 /dev/zero | tr '\\0' x; sleep 0.5")
    ended = await until(lambda: notes_until_end(store, chat))
    said = [line for n in ended for line in n["result"].splitlines()]
    assert len(said) >= 2 and all(set(line) == {"x"} and len(line) <= 2000 for line in said)
    # In pieces, none of it lost.
    assert sum(len(line) for line in said) == 20000


async def test_a_session_runs_a_bounded_number_of_monitors(api, here_watch):  # noqa: F811
    chat = await a_session(api)
    started = [await start(api, chat, "sleep 2") for _ in range(watch_module.MAX_MONITORS_PER_SESSION)]
    assert all(s.get("status") == "watching" for s in started)
    refused = await start(api, chat, "sleep 2")
    assert "monitors already run" in refused["error"]
    # Another session has its own.
    assert (await start(api, await a_session(api), "sleep 0.1"))["status"] == "watching"


async def test_a_stopped_sessions_monitor_is_killed_and_sends_no_more_lines(api, here_watch):  # noqa: F811
    store = api.app.state.session_store
    chat = await a_session(api)
    started = await start(api, chat, "while true; do echo tick; sleep 0.2; done")
    await until(lambda: notes(store, chat))
    paused = await api.client.post(f"/v1/sessions/{chat}/pause", headers=api.auth())
    assert paused.status_code == 200, paused.text
    ended = await until(lambda: notes_until_end(store, chat))
    assert ended[-1]["status"] == "killed" and ended[-1]["reason"] == "its session was stopped"
    from surogates.tools.utils.process_registry import process_registry

    await until(lambda: asyncio.sleep(0, result=process_registry.poll(started["monitor_id"])["status"] != "running"))
    count = len(await notes(store, chat))
    await asyncio.sleep(0.5)
    assert len(await notes(store, chat)) == count


async def test_a_sub_agent_and_a_thread_are_not_offered_monitor_and_may_not_start_one(api, here_watch):  # noqa: F811
    from surogates.harness.subagents import DISPATCH_KEY, PARITY_KEY, apply_parity

    for config in ({DISPATCH_KEY: {"mode": "background"}}, {"workstream_role": "thread"}, {"under_thread": True}):
        offered = apply_parity({"terminal", "delegate_task"}, {PARITY_KEY: True, **config})
        assert "monitor" not in offered, config
    assert "monitor" in apply_parity({"terminal", "delegate_task"}, {PARITY_KEY: True})
    # Nor to any session with a parent: a helper's, a worker's or a routine run's end is its parent's news.
    assert "monitor" not in apply_parity({"terminal", "delegate_task"}, {PARITY_KEY: True}, root=False)
    store = api.app.state.session_store
    child = await a_session(api)
    await store.update_session_config_key(child, DISPATCH_KEY, {"mode": "background"})
    refused = await start(api, child, "sleep 0.1")
    assert refused["error"].startswith("monitor is for the main session")
    thread = await a_session(api)
    await store.update_session_config_key(thread, "workstream_role", "thread")
    assert (await start(api, thread, "sleep 0.1"))["error"].startswith("monitor is for the main session")
    from surogates.session.provisioning import create_child_session

    helper = await create_child_session(store=store, parent=await store.get_session(await a_session(api)), channel="delegation")
    assert (await start(api, helper.id, "sleep 0.1"))["error"].startswith("monitor is for the main session")


async def test_a_sandbox_made_for_one_turn_runs_no_monitor(api, here_watch, daemon_registry, tmp_path, monkeypatch):  # noqa: F811
    from surogates.sandbox.pool import SandboxPool

    pool = SandboxPool(Daemon(tmp_path))
    monkeypatch.setattr(pool, "holds_copy", lambda owner: True, raising=False)
    chat = await a_session(api)
    refused = json.loads(await agents_module._monitor_handler(
        {"command": "sleep 0.1", "description": "d"},
        session_store=api.app.state.session_store, session_id=str(chat), sandbox_pool=pool,
    ))
    assert refused["error"].startswith("monitor is for the main session")


async def test_all_of_a_sessions_monitors_share_one_rate(api, here_watch, monkeypatch):  # noqa: F811
    monkeypatch.setattr(watch_module, "MAX_NOTES_PER_SESSION_MINUTE", 3)
    store = api.app.state.session_store
    chat = await a_session(api)
    loud = "for i in $(seq 30); do echo tick $i; sleep 0.05; done"
    first = await start(api, chat, loud, max_notifications_per_minute=30)
    second = await start(api, chat, loud, max_notifications_per_minute=30)
    ended = {first["monitor_id"]: False, second["monitor_id"]: False}

    async def both_ended():
        found = await notes(store, chat)
        done = {n["agent_id"] for n in found if n["status"] != "output"}
        return found if set(ended) <= done else None

    found = await until(both_ended)
    # Within the minute, three line notes for the two of them; every line came, the rest with the ends.
    assert len([n for n in found if n["status"] == "output"]) == 3
    assert sum(n["lines"] + n.get("dropped", 0) for n in found) == 60


async def test_a_daemon_answer_of_any_shape_never_stops_the_watch(api, monkeypatch):  # noqa: F811
    store = api.app.state.session_store
    chat = await a_session(api)
    owner = str(chat)
    answers = [
        {"events": [], "known": ["proc_bad"], "lines": {"proc_bad": {"lines": "x", "dropped": "y"}, "other": 5}},
        {"events": [], "known": ["proc_bad"], "lines": {"proc_bad": {"lines": [{"a": 1}, "ok"], "dropped": -3}}},
        {"events": [{"session_id": "proc_bad", "exit_code": "9" * 5000, "output": "done"}], "known": ["proc_bad"]},
    ]

    class Pool:
        async def execute(self, sandbox, name, payload):
            return json.dumps(answers.pop(0) if answers else {"events": [], "known": []})

        def sandbox_of(self, key):
            return None

        def holds_copy(self, key):
            return False

    watch = BackgroundWatch(poll_seconds=0.05)
    watch.configure(store=store, redis=api.app.state.redis, sandbox_pool=Pool())
    try:
        await watch.watch(owner=owner, proc_id="proc_bad", session_id=chat, command="c", monitor=("d", 60.0, 30))
        ended = await until(lambda: notes_until_end(store, chat))
        end = ended[-1]
        assert end["status"] == "completed" and "exit_code" not in end
        assert [line for n in ended for line in n["result"].splitlines()] == ["{'a': 1}", "ok"]
        await until(lambda: asyncio.sleep(0, result=not watch.holds(owner)))
    finally:
        await watch.close()


async def test_a_monitors_notes_never_name_more_lines_than_they_show():
    monitor = watch_module._Monitor(description="d", deadline=0.0, cap=1)
    payload = watch_module.monitor_payload("proc_x", monitor, lines=["y" * 1000] * 50)
    assert payload["lines"] == len(payload["result"].split("\n")) == 9
    assert payload["dropped"] == 41 and len(payload["result"]) <= 10_000
    # At its end, every line held past the note's count is counted as left out.
    monitor.buffered = [f"l{i}" for i in range(120)]
    item = watch_module._Watched(session_id=UUID(int=1), command="c", monitor=monitor)
    end = watch_module.monitor_end("proc_x", item, None, status="killed", reason="r")
    assert end["lines"] == 50 and end["dropped"] == 70


async def test_a_registrys_answer_for_a_monitor_is_read_with_no_trust_in_its_shape():
    read = watch_module._read_lines
    assert read({"lines": "abc", "dropped": "x"}) == ([], 0)
    assert read(["a"]) == ([], 0)
    assert read({"lines": [1, "y" * 3000], "dropped": -4}) == (["1", "y" * 2000], 0)


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
            calling("monitor", "call_mon", command="sleep 4; echo deploy finished", description="Wait for the deploy"),
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
