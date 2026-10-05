"""What a session on the user's computer has read: its own, apart from its root's, its sub-agents' and its experts',
and its dedup forgotten once its history is compacted or cleared, on any worker.  A cloud session's tracker is left
as it was."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.devices.sandbox import DeviceCall, device_call_for
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from surogates.tools.builtin import file_ops
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import InProcessRunner
from tests.test_loop_ordering import _drive, _harness, _resp, _tool_resp
from tests.test_steer_loop import _make_loop_harness, _make_session
from tests.test_wake_slash_command_gate import _harness as _wake_harness
from tests.test_wake_slash_command_gate import _permissive, _stub_store

ROOT, CHILD = str(uuid4()), str(uuid4())
READ = ("/home/me/notes/a.txt", 1, 2000)


@pytest.fixture(autouse=True)
def _fresh_tracker():
    file_ops._read_tracker.clear()
    yield
    file_ops._read_tracker.clear()


@pytest.fixture
def folder(tmp_path):
    root = (tmp_path / "laptop").resolve()
    root.mkdir()
    (root / "a.txt").write_text("alpha\n")
    return root


def call_of(folder, session_id: str) -> DeviceCall:
    """A tool call of *session_id*, ROOT's or a sub-agent's, on *folder*: the root's task, the session's reads."""
    tools = ToolRegistry()
    ToolRuntime(tools).register_builtins()
    wio = DeviceWorkspaceIO(InProcessRunner(LocalWorkspaceIO(str(folder))), root=str(folder))
    return DeviceCall(tools=tools, workspace_io=wio, task_id=ROOT, read_tracker_id=session_id)


async def run(call: DeviceCall, name: str, **args) -> dict:
    return json.loads(await call.dispatch(name, args))


async def test_a_sub_agents_read_does_not_turn_its_roots_into_a_reference(folder):
    child, root = call_of(folder, CHILD), call_of(folder, ROOT)
    assert (await run(child, "read_file", path="a.txt"))["content"] == "alpha\n"
    # That result is in the sub-agent's conversation, not the root's.
    assert (await run(root, "read_file", path="a.txt"))["content"] == "alpha\n"
    assert (await run(root, "read_file", path="a.txt"))["dedup"] is True


async def test_each_session_counts_its_own_repeated_reads(folder):
    child, root = call_of(folder, CHILD), call_of(folder, ROOT)
    for second in range(3):
        # Changed each time, so each read shows it again.
        os.utime(folder / "a.txt", (second, second))
        last = await run(child, "read_file", path="a.txt")
    assert "_warning" in last
    first = await run(root, "read_file", path="a.txt")
    assert first["content"] == "alpha\n" and "_warning" not in first


async def test_a_sub_agent_reads_a_file_before_it_overwrites_it(folder):
    child, root = call_of(folder, CHILD), call_of(folder, ROOT)
    await run(root, "read_file", path="a.txt")
    refused = await run(child, "write_file", path="a.txt", content="beta\n")
    assert refused["error"].startswith("Refusing to overwrite")
    await run(child, "read_file", path="a.txt")
    assert (await run(child, "write_file", path="a.txt", content="beta\n"))["status"] == "ok"


async def run_for_harness_tool(call: DeviceCall, name: str, **args) -> dict:
    """A tool call a harness tool makes through the call, as an expert's tool loop does."""
    return json.loads(await call.execute(CHILD, name, json.dumps(args)))


async def test_an_experts_read_is_not_a_reference_to_its_sessions(folder):
    call = call_of(folder, CHILD)
    await run(call, "read_file", path="a.txt")
    # The expert's transcript is another conversation: it has not seen the session's result.
    assert (await run_for_harness_tool(call, "read_file", path="a.txt"))["content"] == "alpha\n"


async def test_a_sessions_read_is_not_a_reference_to_its_experts(folder):
    call = call_of(folder, CHILD)
    await run_for_harness_tool(call, "read_file", path="a.txt")
    # The session's model sees only the expert's answer.
    assert (await run(call, "read_file", path="a.txt"))["content"] == "alpha\n"


async def test_an_experts_reads_last_as_long_as_its_call(folder):
    call = call_of(folder, CHILD)
    await run_for_harness_tool(call, "read_file", path="a.txt")
    assert (await run_for_harness_tool(call, "write_file", path="a.txt", content="beta\n"))["status"] == "ok"
    del call
    assert file_ops._read_tracker == {}


class Recording:
    """A tool registry that keeps what each dispatch was given."""

    def __init__(self) -> None:
        self.given: list[dict] = []

    async def dispatch(self, name, args, **kwargs):
        self.given.append(kwargs)
        return "{}"


async def test_a_call_keeps_its_sessions_reads_under_its_roots_task():
    child = SimpleNamespace(id=uuid4(), parent_id=uuid4(), config={
        "execution": {"kind": "device", "device_id": str(uuid4())},
        "workspace_path": "/home/me/notes",
        "sandbox_root_session_id": ROOT,
    })
    tools = Recording()
    call = device_call_for(
        child, tools=tools, invocation_id="1:call_1", lease_token=None, session_factory=None, redis=None,
    )
    await call.dispatch("read_file", {"path": "a.txt"})
    # Background processes belong to the root's binding; what was read, to the session.
    [given] = tools.given
    assert (given["task_id"], given["read_tracker_id"]) == (ROOT, str(child.id))


def device_session(**config):
    return _make_session().model_copy(update={"config": {
        "execution": {"kind": "device", "device_id": str(uuid4())}, "workspace_path": "/home/me/notes", **config,
    }})


def read_by(owner: str) -> None:
    """*owner* read a.txt: its dedup entry and its read record."""
    tracked = file_ops._init_task_data(owner)
    tracked["dedup"][READ] = 1.0
    tracked["read_timestamps"][READ[0]] = 1.0


def dedup_of(owner: str) -> dict:
    return file_ops._init_task_data(owner)["dedup"]


async def test_resuming_a_sessions_calls_forgets_its_own_reads_only(monkeypatch):
    harness = _make_loop_harness(session_store=AsyncMock())
    harness._tool_call_kwargs = MagicMock(return_value={})
    monkeypatch.setattr("surogates.harness.loop.replay_unanswered", AsyncMock(return_value=None))
    child = device_session(sandbox_root_session_id=ROOT)
    for owner in (ROOT, str(child.id)):
        read_by(owner)
    await harness._resume_unanswered_calls(child, SimpleNamespace(lease_token="t"), [], [])
    assert not file_ops.has_read(READ[0], str(child.id))
    assert file_ops.has_read(READ[0], ROOT)


def compressing(harness, compressed: list[dict]) -> None:
    harness._compressor = SimpleNamespace(
        context_length=1000,
        _context_window=200_000,
        should_compress=lambda *a, **k: True,
        compress=AsyncMock(return_value=(compressed, {
            "strategy": "summary", "original_token_estimate": 2, "compressed_token_estimate": 1,
        })),
    )
    harness._memory_snapshot_cache = {}


HISTORY = [{"role": "user", "content": f"message {number}"} for number in range(8)]


@pytest.mark.parametrize("cloud", [False, True], ids=["local folder", "cloud"])
async def test_compress_forgets_a_device_sessions_dedup_and_keeps_what_it_read(cloud):
    harness = _make_loop_harness(session_store=AsyncMock())
    compressing(harness, HISTORY[:2])
    session = _make_session() if cloud else device_session()
    read_by(str(session.id))
    await harness._handle_compress_command(
        session, [*HISTORY, {"role": "user", "content": "/compress"}], "system", SimpleNamespace(lease_token="t"),
    )
    assert bool(dedup_of(str(session.id))) is cloud
    assert file_ops.has_read(READ[0], str(session.id))


async def test_a_compaction_for_a_rejected_request_forgets_a_device_sessions_dedup():
    harness = _make_loop_harness(session_store=AsyncMock())
    compressing(harness, HISTORY[:2])
    session = device_session()
    read_by(str(session.id))
    # The loop harness's own is a stand-in.
    retry = AgentHarness._compress_context_callback(
        harness, session, list(HISTORY), "system", SimpleNamespace(lease_token="t"),
        build_api_messages=AsyncMock(return_value=[]),
    )
    assert await retry([]) == []
    assert dedup_of(str(session.id)) == {}
    assert file_ops.has_read(READ[0], str(session.id))


async def test_a_compaction_in_the_loop_forgets_a_device_sessions_dedup(monkeypatch):
    harness = _harness()
    compressing(harness, [{"role": "user", "content": "q"}])
    session = device_session()
    read_by(str(session.id))
    await _drive(harness, [_tool_resp("c1"), _resp("Done.")], monkeypatch, session=session)
    harness._compressor.compress.assert_awaited()
    assert dedup_of(str(session.id)) == {}
    assert file_ops.has_read(READ[0], str(session.id))


async def test_a_compaction_at_wake_forgets_a_device_sessions_dedup():
    harness = _make_loop_harness(session_store=AsyncMock())
    compressing(harness, HISTORY[:2])
    harness._prompt = SimpleNamespace(build=lambda: "system")
    session = device_session()
    read_by(str(session.id))
    assert await harness._engineer_context(session, [], list(HISTORY)) == HISTORY[:2]
    assert dedup_of(str(session.id)) == {}
    assert file_ops.has_read(READ[0], str(session.id))


async def test_clear_forgets_a_device_sessions_dedup():
    harness = _make_loop_harness(session_store=AsyncMock())
    compressing(harness, [])
    session = device_session()
    read_by(str(session.id))
    await harness._handle_clear_command(session, SimpleNamespace(lease_token="t"))
    assert dedup_of(str(session.id)) == {}
    assert file_ops.has_read(READ[0], str(session.id))


def event(event_id: int, kind: EventType, **data) -> SimpleNamespace:
    return SimpleNamespace(id=event_id, type=kind.value, data=data)


async def wake(monkeypatch, session, earlier: list) -> None:
    """Wake *session* on "test-worker" for a message after *earlier*, up to its loop."""
    monkeypatch.setattr("surogates.harness.loop.resolve_agent_def", AsyncMock(return_value=None))
    events = [*earlier, event(len(earlier) + 1, EventType.USER_MESSAGE, content="read a.txt again")]
    harness = _wake_harness(_stub_store(session, events), _permissive())
    harness._compressor.prune_stale_browser_states = lambda messages: messages
    harness._run_loop = AsyncMock()
    await harness.wake(session.id)


# Another worker woke the session last and compacted its history there.
ELSEWHERE = [
    event(1, EventType.HARNESS_WAKE, worker_id="another-worker", cursor=0),
    event(2, EventType.CONTEXT_COMPACT, compacted_messages=[]),
]


async def test_a_wake_after_another_workers_forgets_the_dedup_this_worker_kept(monkeypatch, folder):
    session = device_session()
    call = call_of(folder, str(session.id))
    await run(call, "read_file", path="a.txt")
    await wake(monkeypatch, session, ELSEWHERE)
    assert (await run(call, "read_file", path="a.txt"))["content"] == "alpha\n"


async def test_a_wake_after_this_workers_own_keeps_the_dedup(monkeypatch, folder):
    session = device_session()
    call = call_of(folder, str(session.id))
    await run(call, "read_file", path="a.txt")
    await wake(monkeypatch, session, [event(1, EventType.HARNESS_WAKE, worker_id="test-worker", cursor=0)])
    assert (await run(call, "read_file", path="a.txt"))["dedup"] is True


async def test_a_cloud_wake_after_another_workers_keeps_the_tracker(monkeypatch):
    session = _make_session()
    read_by(str(session.id))
    await wake(monkeypatch, session, ELSEWHERE)
    assert dedup_of(str(session.id))
