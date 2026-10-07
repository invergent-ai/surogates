"""A project's threads each work on a copy of its files, in a pod of their own."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.governance.saga import SagaOrchestrator
from surogates.harness import tool_exec
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry
from tests.test_steer_loop import _final_response
from tests.thread_pods import ThreadPods

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_turn_sagas import a_turn, calling, saga_events, stop
from .test_workstream_threads import start
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture()
def pods(tmp_path) -> ThreadPods:
    pods = ThreadPods(tmp_path)
    (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    (pods.project / "notes.txt").write_text("v1 notes\n")
    return pods


async def a_thread(api, title="Draft A", master=None):
    """A thread of a new project, or of *master*'s, whose goal names no file."""
    master = master or await master_of(api, await create(api))
    return await start(api, master, title=title, goal="Work on the report.")


def in_snapshot(pods: ThreadPods, thread, commit: str) -> list[str]:
    """The files of *thread*'s snapshot *commit*."""
    repo = next((pods.root).glob("*/home/.surogates/history/*"))
    return subprocess.run(
        ["git", f"--git-dir={repo}", "ls-tree", "-r", "--name-only", commit],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()


async def test_each_step_of_a_threads_turn_has_the_copy_it_started_from(api, monkeypatch, pods):
    thread = await a_thread(api)
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "threads/Draft A/outline.md", "content": "outline"})),
        calling(("terminal", {"command": "printf ' edited' >> Report.docx"})),
        calling(("read_file", {"path": "notes.txt"})),
        calling(("patch", {"path": "notes.txt", "old_string": "v1", "new_string": "v2"})),
        _final_response("Drafted the outline."),
    ], saga=False, pool=SandboxPool(pods))
    calls = {e.data["name"]: e.data for e in await api.app.state.session_store.get_events(thread.id, types=[EventType.TOOL_CALL])}
    # A thread runs its saga whatever saga.enabled says, and every step but a read starts from a snapshot.
    assert "checkpoint_hash" not in calls["read_file"]
    write, terminal, patch = (calls[name]["checkpoint_hash"] for name in ("write_file", "terminal", "patch"))
    assert "threads/Draft A/outline.md" not in in_snapshot(pods, thread, write)
    assert "threads/Draft A/outline.md" in in_snapshot(pods, thread, terminal)
    assert len({write, terminal, patch}) == 3
    steps = [d for t, d in await saga_events(api, thread.id) if t == EventType.SAGA_STEP_BEGIN.value]
    assert [s["checkpoint_hash"] for s in steps] == [write, terminal, patch]


async def test_a_call_refused_before_it_runs_takes_no_snapshot(monkeypatch):
    taken = []

    async def snapshot(*args, reason, **kwargs):
        taken.append(reason)

    monkeypatch.setattr(tool_exec, "_snapshot_copy", snapshot)
    call = {"id": "c1", "function": {"name": "write_file", "arguments": json.dumps({"path": "a.md", "content": "a"})}}
    malformed = {"id": "c2", "function": {"name": "write_file", "arguments": '{"path": "a.md", "content": '}}
    # Not offered to the model, not on the session's allow-list, or its arguments not JSON.
    for call, config, offered in (
        (call, {}, frozenset({"terminal"})), (call, {"tool_allow_list": ["terminal"]}, None), (malformed, {}, None),
    ):
        await tool_exec._run_single_tool(
            call, session=SimpleNamespace(id=uuid4(), config={"workstream_role": "thread", **config}),
            lease=MagicMock(), store=AsyncMock(), tools=ToolRegistry(), tenant=MagicMock(),
            saga=SagaOrchestrator(), sandbox_pool=MagicMock(), offered_tools=offered,
        )
    assert taken == []


async def test_stopping_a_thread_puts_its_copy_back_and_leaves_the_real_files(api, monkeypatch, pods):
    thread = await a_thread(api)
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "threads/Draft A/outline.md", "content": "outline"})),
        calling(("terminal", {"command": "printf ' edited' >> Report.docx"})),
        calling(("memory", {"action": "add", "content": "The memo is for the board."})),
        _final_response("Drafted."),
    ], saga=False, pool=SandboxPool(pods), during=stop)
    copy = pods.copies[str(thread.id)]
    # The copy is back where the turn started, the file it made included.
    assert (copy / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert not (copy / "threads" / "Draft A" / "outline.md").exists()
    # The real files never changed.
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "notes.txt"]
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    *_, (kind, done) = await saga_events(api, thread.id)
    assert (kind, done["status"]) == (EventType.SAGA_COMPLETE.value, "completed")
