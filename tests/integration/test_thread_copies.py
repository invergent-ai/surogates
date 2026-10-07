"""A project's threads each work on a copy of its files, in a pod of their own."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text

from surogates.governance.saga import SagaOrchestrator
import surogates.harness.loop as loop_module
from surogates.harness import landing as landing_module
from surogates.harness import loop_artifact_completion, tool_exec
from surogates.runtime import SlashCommandConfig
from surogates.harness.loop_context_replay import worker_note
from surogates.harness.tool_exec import _build_session_sandbox_spec
from surogates.sandbox.history import History
from surogates.sandbox.base import SandboxSpec
from surogates.session.provisioning import create_child_session
from surogates.tools.utils import checkpoint_manager
from surogates.sandbox.pool import SandboxPool, sandbox_session_key
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry
from tests.test_steer_loop import _final_response
from tests.thread_pods import ThreadPods
from tests.test_wake_slash_command_gate import _harness as a_waking_harness

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_turn_sagas import a_turn, calling, saga_events, stop
from .test_workstream_threads import harness_of, start
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


QUICK = SimpleNamespace(default_step_timeout=30, default_max_retries=0, retry_delay=0)


async def reports(api, master) -> list[dict]:
    return [e.data for e in await api.app.state.session_store.get_events(master.id, types=[EventType.WORKER_COMPLETE])]


async def open_pod(pool, thread) -> None:
    """*thread*'s pod, opened now: its copy is the real files as they are."""
    tenant = SimpleNamespace(org_id=thread.org_id, user_id=thread.user_id)
    owner = sandbox_session_key(thread)
    await pool.ensure(owner, await _build_session_sandbox_spec(thread, tenant, owner))


def repo_of(pods: ThreadPods, thread) -> Path:
    """The history in *thread*'s latest pod."""
    return next(pods.copies[str(thread.id)].parent.glob("home/.surogates/history/*"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", f"--git-dir={repo}", *args], capture_output=True, text=True, check=True).stdout.strip()


async def test_two_threads_change_one_docx_and_the_second_leaves_it_to_the_first(api, monkeypatch, pods):
    (pods.project / "Budget.xlsx").write_bytes(b"budget v1")
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await open_pod(pool, second)  # two pods at once: B's copy is made before A lands

    await a_turn(api, monkeypatch, first, [
        calling(("terminal", {"command": "printf ' by A' >> Report.docx"})),
        calling(("write_file", {"path": "threads/Draft A/sources.md", "content": "sources"})),
        _final_response("Edited the report."),
    ], pool=pool)
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1 by A"
    assert (pods.project / "threads" / "Draft A" / "sources.md").read_text() == "sources"

    await a_turn(api, monkeypatch, second, [
        calling(("terminal", {"command": "printf ' by B' >> Report.docx && printf ' by B' >> Budget.xlsx"})),
        _final_response("Edited the report and the budget."),
    ], pool=pool)
    # The newer docx stays; B's other file lands.
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1 by A"
    assert (pods.project / "Budget.xlsx").read_bytes() == b"budget v1 by B"

    a, b = await reports(api, master)
    assert [(f["ref"], f["landing"]) for f in a["files"]] == [("Report.docx", "landed"), ("threads/Draft A/sources.md", "landed")]
    assert [(f["ref"], f["landing"], f.get("reason")) for f in b["files"]] == [
        ("Budget.xlsx", "landed", None), ("Report.docx", "not_merged", "changed"),
    ]
    # The master reads that B's docx was not applied.
    note = worker_note(EventType.WORKER_COMPLETE.value, b)["content"]
    assert note.endswith(
        "Files: Budget.xlsx\n"
        "Not merged, because the project's file changed after the thread started (the newer file was kept): Report.docx"
    )


async def test_a_landing_is_a_merge_of_the_turn_carrying_who_made_it(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "threads/Draft A/outline.md", "content": "outline"})),
        _final_response("Outlined."),
    ], pool=pool)
    *_, last = await store.get_events(thread.id, types=[EventType.TOOL_CALL])
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Add the sources."})
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "threads/Draft A/sources.md", "content": "sources"})),
        _final_response("Added the sources."),
    ], pool=pool)
    repo = repo_of(pods, thread)
    landing = git(repo, "rev-parse", "refs/heads/main")
    turn = git(repo, "rev-parse", f"{landing}^2")
    assert git(repo, "log", "-1", "--format=%an <%ae>", landing) == f"Draft A <thread:{thread.id}@surogate>"
    trailers = dict(line.split(": ", 1) for line in git(repo, "log", "-1", "--format=%(trailers:only)", landing).splitlines())
    # The second landing names the second turn's calls alone.
    calls = await store.get_events(thread.id, after=last.id, types=[EventType.TOOL_CALL])
    tool_saga = [d for t, d in await saga_events(api, thread.id) if t == EventType.SAGA_START.value][-1]["saga_id"]
    assert trailers == {
        "Surogate-Project": thread.config["workstream_id"],
        "Surogate-Thread": str(thread.id),
        "Surogate-Agent": thread.agent_id,
        "Surogate-User": str(thread.user_id),
        "Surogate-Saga": trailers["Surogate-Saga"],
        "Surogate-Tool-Saga": tool_saga,
        "Surogate-Events": f"{calls[0].id}-{calls[-1].id}",
        "Surogate-Kind": "landing",
    }
    assert trailers["Surogate-Saga"].startswith("saga:") and trailers["Surogate-Saga"] != tool_saga
    assert git(repo, "log", "-1", "--format=%(trailers:key=Surogate-Kind,valueonly)", turn) == "turn"
    assert not (pods.copies[str(thread.id)] / ".git").exists()


async def test_an_apply_that_fails_on_the_third_of_five_files_puts_the_first_two_back(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    apply = History.apply

    def a_save_lands_first(self, path, before, after):
        if path == "c.md":
            (self.project / "c.md").write_text("saved by you just now")
        return apply(self, path, before, after)

    # The pod forks a child per call, which inherits this.
    monkeypatch.setattr(History, "apply", a_save_lands_first)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "for f in a b c d e; do echo $f > $f.md; done"})),
        _final_response("Wrote five notes."),
    ], pool=SandboxPool(pods), saga_settings=QUICK)
    # All or nothing: the real files are as they were before the landing.
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "c.md", "notes.txt"]
    assert (pods.project / "c.md").read_text() == "saved by you just now"
    [report] = await reports(api, master)
    assert report["landing"] == "compensated"
    assert {f["landing"] for f in report["files"]} == {"not_merged"}
    assert worker_note(EventType.WORKER_COMPLETE.value, report)["content"].endswith(
        "Files: none\n"
        "Not landed, and the project's files are as they were: a.md, b.md, c.md, d.md, e.md"
    )


async def test_a_rolled_back_landing_takes_away_the_folders_it_made_and_leaves_the_users(api, monkeypatch, pods):
    (pods.project / "Reports").mkdir()  # the user's, still empty
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    apply = History.apply

    def a_save_lands_first(self, path, before, after):
        if path == "c.md":
            (self.project / "c.md").write_text("saved by you just now")
        return apply(self, path, before, after)

    monkeypatch.setattr(History, "apply", a_save_lands_first)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "mkdir -p Drafts/2026 Reports && echo a > Drafts/2026/a.md && echo q > Reports/q1.md && echo c > c.md"})),
        _final_response("Wrote three notes."),
    ], pool=SandboxPool(pods), saga_settings=QUICK)
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "Reports", "c.md", "notes.txt"]
    assert not any((pods.project / "Reports").iterdir())


class LosesAReply(SandboxPool):
    """A pool whose pod writes ``b.md``, but whose reply to that apply never comes back."""

    async def execute(self, session_id, name, input):
        result = await super().execute(session_id, name, input)
        args = json.loads(input or "{}")
        if name == "_history" and (args.get("action"), args.get("path")) == ("apply", "b.md"):
            raise TimeoutError("the reply was lost")
        return result


async def test_an_apply_whose_reply_is_lost_is_put_back(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "for f in a b c; do echo $f > $f.md; done"})),
        _final_response("Wrote three notes."),
    ], pool=LosesAReply(pods), saga_settings=QUICK)
    # The pod wrote b.md, but the landing never heard: it is put back with a.md.
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "notes.txt"]
    [report] = await reports(api, master)
    assert report["landing"] == "compensated"


class CancelledOnTheThird(SandboxPool):
    """A pool whose pod applies ``c.md``, and whose turn is then cancelled, as a lost lease detaches it."""

    async def execute(self, session_id, name, input):
        result = await super().execute(session_id, name, input)
        args = json.loads(input or "{}")
        if name == "_history" and (args.get("action"), args.get("path")) == ("apply", "c.md"):
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        return result


class CancelledWhilePuttingBack(SandboxPool):
    """A pool whose turn is cancelled, as a lost lease detaches it, once the landing starts putting files back."""

    turn: asyncio.Task | None = None

    async def execute(self, session_id, name, input):
        if name == "_history" and json.loads(input or "{}").get("action") == "unapply" and self.turn is not None:
            self.turn.cancel()
            self.turn = None
            await asyncio.sleep(0)
        return await super().execute(session_id, name, input)


async def test_a_put_back_a_cancel_reaches_still_finishes(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    apply = History.apply

    def a_save_lands_first(self, path, before, after):
        if path == "c.md":
            (self.project / "c.md").write_text("saved by you just now")
        return apply(self, path, before, after)

    monkeypatch.setattr(History, "apply", a_save_lands_first)
    pool = CancelledWhilePuttingBack(pods)
    pool.turn = turn = asyncio.create_task(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "for f in a b c d e; do echo $f > $f.md; done"})),
        _final_response("Wrote five notes."),
    ], pool=pool, saga_settings=QUICK))
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "c.md", "notes.txt"]
    # Its pod goes: no later turn takes up the copy whose writes were put back.
    assert (pool.holds_copy(str(thread.id)), pods.pods) == (False, {})


async def test_a_cancelled_landing_puts_its_files_back(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    turn = asyncio.create_task(a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "for f in a b c d e; do echo $f > $f.md; done"})),
        _final_response("Wrote five notes."),
    ], pool=CancelledOnTheThird(pods), saga_settings=QUICK))
    with pytest.raises(asyncio.CancelledError):
        await turn
    # a.md and b.md were applied, and c.md written before its reply was read: all go back.
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "notes.txt"]
    assert pods.pods == {}


class FailsToCommit(SandboxPool):
    """A pool whose pod cannot commit the turn: the landing knows no file of it."""

    async def execute(self, session_id, name, input):
        if name == "_history" and json.loads(input or "{}").get("action") == "commit":
            return json.dumps({"error": "git add failed: Input/output error"})
        return await super().execute(session_id, name, input)


async def landing_told(api, monkeypatch, pool) -> tuple[dict, str]:
    """A thread's turn that writes a.md, ended through *pool*: its report and the master's note."""
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "a.md", "content": "a"})),
        _final_response("I wrote a.md."),
    ], pool=pool, saga_settings=QUICK)
    [report] = await reports(api, master)
    return report, worker_note(EventType.WORKER_COMPLETE.value, report)["content"]


async def test_a_landing_that_never_knew_its_files_still_tells_the_master(api, monkeypatch, pods):
    report, note = await landing_told(api, monkeypatch, FailsToCommit(pods))
    assert report["landing"] == "compensated"
    assert note.endswith("Files: none\nNot landed, and the project's files are as they were: a.md")
    assert not (pods.project / "a.md").exists()


async def test_a_landing_that_raised_tells_the_master_it_failed(api, monkeypatch, pods):
    async def raising(**_):
        raise RuntimeError("the lock's connection dropped")

    monkeypatch.setattr(loop_artifact_completion, "land_turn", raising)
    report, note = await landing_told(api, monkeypatch, SandboxPool(pods))
    assert report["landing"] == "failed"
    assert note.endswith("Files: none\nNot saved, because the landing failed: a.md")
    assert not (pods.project / "a.md").exists()


async def test_an_excluded_file_a_turn_made_is_named_in_its_report(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo scratch > notes.tmp && mkdir -p node_modules && echo x > node_modules/x.js && echo kept > kept.md && rm notes.txt"})),
        _final_response("Kept a note."),
    ], pool=SandboxPool(pods))
    # The deletion waits: the turn wrote files history leaves out, and it may be a move into one.
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "kept.md", "notes.txt"]
    [report] = await reports(api, master)
    assert report["excluded"] == ["node_modules/", "notes.tmp"]
    assert worker_note(EventType.WORKER_COMPLETE.value, report)["content"].endswith(
        "Files: kept.md\n"
        "Not merged, because they go with a change that was not merged (a move lands whole or not at all): notes.txt\n"
        "Not saved, because the project's history leaves them out: node_modules/, notes.tmp"
    )


async def test_a_landed_deletion_is_named_apart_in_its_report(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "echo kept > kept.md && rm notes.txt"})),
        _final_response("Kept a note."),
    ], pool=SandboxPool(pods))
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "kept.md"]
    [report] = await reports(api, master)
    # A deletion is named apart: the master must not read it as a file to open.
    assert worker_note(EventType.WORKER_COMPLETE.value, report)["content"].endswith(
        "Files: kept.md\nDeleted: notes.txt"
    )


async def test_a_folder_inside_a_git_repository_a_turn_wrote_into_is_named_as_not_landed(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "git init -q clone && echo x > clone/x.md && echo kept > kept.md"})),
        _final_response("Kept a note."),
    ], pool=SandboxPool(pods))
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "kept.md", "notes.txt"]
    [report] = await reports(api, master)
    assert (report["repositories"], "excluded" in report) == (["clone/"], False)
    assert worker_note(EventType.WORKER_COMPLETE.value, report)["content"].endswith(
        "Files: kept.md\n"
        "Not landed, because they are inside a git repository: clone/"
    )


async def test_a_turn_that_never_used_its_pod_lands_nothing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    await a_turn(api, monkeypatch, thread, [_final_response("Nothing to do.")], pool=SandboxPool(pods))
    assert pods.pods == {} and str(thread.id) not in pods.copies
    [report] = await reports(api, master)
    assert "landing" not in report and "excluded" not in report


async def turn_end(api, pool, thread) -> None:
    """*thread*'s turn ends, as a worker ends it."""
    store = api.app.state.session_store
    harness = harness_of(api)
    harness._sandbox_pool = pool
    lease = await store.try_acquire_lease(thread.id, f"worker-{thread.id}", ttl_seconds=60)
    await harness._complete_session(
        thread, [{"role": "assistant", "content": "Done."}], lease, reason="completed", turn_id="turn-1",
    )
    await store.release_lease(thread.id, lease.lease_token)


async def until(check, seconds: float = 20.0) -> None:
    """Wait until *check* answers true."""
    for _ in range(int(seconds / 0.05)):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{check.__name__} never became true")


async def test_two_landings_at_once_take_turns(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    for thread, by in ((first, "A"), (second, "B")):
        await open_pod(pool, thread)
        await pool.execute(str(thread.id), "terminal", json.dumps({"command": f"printf ' by {by}' >> Report.docx"}))
    commit_turn, release = History.commit_turn, pods.root / "release"

    def the_first_holds_its_commit(self, **kwargs):
        # The pod forks a child per call, which inherits this.
        (pods.root / f"commit {self.thread}").touch()
        while self.thread == str(first.id) and not release.exists():
            time.sleep(0.05)
        return commit_turn(self, **kwargs)

    async def the_first_is_landing():
        return (pods.root / f"commit {first.id}").exists()

    async def the_second_waits_for_the_lock():
        async with api.app.state.session_factory() as db:
            waiting = await db.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"))
            return waiting.scalar() > 0

    monkeypatch.setattr(History, "commit_turn", the_first_holds_its_commit)
    landing = asyncio.create_task(turn_end(api, pool, first))
    await until(the_first_is_landing)
    waiting = asyncio.create_task(turn_end(api, pool, second))
    try:
        await until(the_second_waits_for_the_lock)
        assert not (pods.root / f"commit {second.id}").exists()
    finally:
        release.touch()
    await asyncio.gather(landing, waiting)
    # The first landed the docx; the second found it changed and left it, rolling back nothing.
    a, b = await reports(api, master)
    assert "landing" not in a and "landing" not in b
    assert [(f["ref"], f["landing"]) for f in a["files"]] == [("Report.docx", "landed")]
    assert [(f["ref"], f["landing"]) for f in b["files"]] == [("Report.docx", "not_merged")]
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1 by A"


async def test_a_report_says_why_each_file_was_not_merged():
    def a(name: str, **more: str) -> dict:
        return {"kind": "file", "label": name, "ref": name, "landing": "not_merged", **more}

    files = [
        {"kind": "file", "label": "kept.md", "ref": "kept.md", "landing": "landed"},
        a("Final.docx", reason="changed"), a("Draft.docx", reason="with"), a("notes", reason="shape"),
        a("old.docx"),  # a report from before reasons were given
    ]
    note = worker_note(EventType.WORKER_COMPLETE.value, {"worker_id": "w", "title": "Draft A", "result": "Done.", "files": files})
    assert note["content"].endswith(
        "Files: kept.md\n"
        "Not merged, because the project's file changed after the thread started (the newer file was kept): Final.docx, old.docx\n"
        "Not merged, because the project has a folder where the thread made a file, or a file where it made a folder: notes\n"
        "Not merged, because they go with a change that was not merged (a move lands whole or not at all): Draft.docx"
    )


async def test_a_reports_excluded_files_and_repositories_are_cut_at_ten_and_say_how_many_more():
    data = {
        "worker_id": "w", "title": "Draft A", "result": "Done.", "files": [],
        "excluded": [f"n{i:02}.tmp" for i in range(12)], "repositories": [f"r{i:02}/" for i in range(11)],
    }
    note = worker_note(EventType.WORKER_COMPLETE.value, data)["content"]
    assert note.endswith(
        "Not saved, because the project's history leaves them out: "
        + ", ".join(f"n{i:02}.tmp" for i in range(10)) + ", and 2 more\n"
        "Not landed, because they are inside a git repository: "
        + ", ".join(f"r{i:02}/" for i in range(10)) + ", and 1 more"
    )


async def test_a_thread_never_works_on_or_restores_its_real_files(api, monkeypatch, pods, tmp_path):
    (pods.project / "Budget.xlsx").write_bytes(b"budget v1")
    monkeypatch.setattr(checkpoint_manager, "CHECKPOINT_BASE", tmp_path / "checkpoints")
    thread = await a_thread(api)
    pool = SandboxPool(pods)
    # Under the thread's key, a pod with the real files at /workspace, as a
    # helper's own spec made it before.
    await pool.ensure(str(thread.id), SandboxSpec(env={}))

    async def the_user_saves_then_stops(harness):
        (pods.project / "Budget.xlsx").write_bytes(b"budget v2, saved by you")
        harness.interrupt("stopped by the user")

    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "printf 'report by the thread' > Report.docx"})),
        calling(("memory", {"action": "add", "content": "Name: Ana"})),
        _final_response("Done."),
    ], pool=pool, during=the_user_saves_then_stops)
    # Its step was refused there, and its Stop restored nothing over the real files.
    events = await api.app.state.session_store.get_events(thread.id, types=[EventType.TOOL_RESULT])
    [refused] = [json.loads(e.data["content"]) for e in events if e.data["name"] == "terminal"]
    assert refused["error"] == "sandbox_unavailable", refused
    assert (pods.project / "Budget.xlsx").read_bytes() == b"budget v2, saved by you"
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"


async def test_a_threads_helper_on_another_worker_makes_the_threads_own_copy(api, pods):
    thread = await a_thread(api)
    store = api.app.state.session_store
    helper = await create_child_session(store=store, parent=thread, channel="api")
    owner = sandbox_session_key(helper)
    spec = await _build_session_sandbox_spec(helper, SimpleNamespace(org_id=helper.org_id, user_id=helper.user_id), owner)
    # The pod follows its root, the thread: whoever provisions it, it holds the thread's copy.
    other_worker = SandboxPool(pods)
    await other_worker.ensure(owner, spec)
    assert (owner, other_worker.holds_copy(owner), spec.env["HISTORY_THREAD"]) == (str(thread.id), True, str(thread.id))


async def test_a_thread_whose_pod_was_remade_mid_turn_is_told_its_edits_are_gone(api, monkeypatch, pods):
    thread = await a_thread(api)

    async def the_pod_goes(harness):  # past its deadline
        await pods.destroy(next(iter(pods.pods)))

    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "a.md", "content": "a"})),
        calling(("memory", {"action": "add", "content": "Name: Ana"})),
        calling(("write_file", {"path": "b.md", "content": "b"})),
        _final_response("Done."),
    ], pool=SandboxPool(pods), during=the_pod_goes)
    events = await api.app.state.session_store.get_events(thread.id, types=[EventType.TOOL_RESULT])
    first, second = [e.data["content"] for e in events if e.data["name"] == "write_file"]
    assert "was made again" not in first
    # It says what is known, not why the copy was made again.
    assert second.startswith(
        "[This thread's copy of the project's files was made again from the project's files. "
        "Changes this thread made after its last landed turn are not in it. "
        "Check the files before making any of those changes again.]\n\n"
    ), second
    # What it wrote before is gone with the old copy; what it wrote after lands.
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "b.md", "notes.txt"]


async def test_a_reports_excluded_files_are_capped_in_its_payload_and_counted(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    await a_turn(api, monkeypatch, thread, [
        calling(("terminal", {"command": "for i in $(seq -w 1 250); do echo x > n$i.tmp; done && echo kept > kept.md"})),
        _final_response("Kept a note."),
    ], pool=SandboxPool(pods))
    [report] = await reports(api, master)
    assert (len(report["excluded"]), report["excluded_count"]) == (200, 250)
    note = worker_note(EventType.WORKER_COMPLETE.value, report)["content"]
    assert note.endswith(", n010.tmp, and 240 more")


async def test_a_thread_cannot_hand_work_to_a_helper_yet(api, monkeypatch, pods):
    thread = await a_thread(api)
    # Bounded: a delegation let through waits on its helper, and would hang the test.
    await asyncio.wait_for(a_turn(api, monkeypatch, thread, [
        calling(("delegate_task", {"goal": "Summarise the report."})),
        _final_response("Summarised it myself."),
    ], pool=SandboxPool(pods)), 60)
    store = api.app.state.session_store
    [answer] = [json.loads(e.data["content"]) for e in await store.get_events(thread.id, types=[EventType.TOOL_RESULT])
                if e.data["name"] == "delegate_task"]
    assert answer == {"error": "A thread can't start delegate_task yet: do this step in the thread itself."}
    # A call that never runs takes no snapshot of the copy.
    [call] = await store.get_events(thread.id, types=[EventType.TOOL_CALL])
    assert "checkpoint_hash" not in call.data
    # No helper started, and no pod was set up for a call that never ran.
    async with api.app.state.session_factory() as db:
        helpers = (await db.execute(text("SELECT count(*) FROM sessions WHERE parent_id = :id"), {"id": thread.id})).scalar()
    assert (helpers, pods.pods) == (0, {})


@pytest.mark.parametrize("cut", [asyncio.CancelledError, RuntimeError])
async def test_a_thread_turn_cut_off_outside_its_landing_lets_its_copy_go(api, monkeypatch, pods, cut):
    thread = await a_thread(api)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Edit the report."})
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    harness = a_waking_harness(store, SlashCommandConfig())
    harness._compressor.prune_stale_browser_states.side_effect = lambda messages: messages
    harness._redis, harness._session_factory, harness._sandbox_pool = api.app.state.redis, api.app.state.session_factory, pool

    async def a_turn_cut_off(session, *_, **__):
        await open_pod(pool, session)  # its first step made its copy
        raise cut("the turn's lease went to another worker" if cut is RuntimeError else None)

    harness._run_loop = a_turn_cut_off
    with pytest.raises(cut):
        await harness.wake(thread.id)
    # No later turn on this worker goes on with a copy of a turn that never landed.
    assert (pool.holds_copy(str(thread.id)), pods.pods) == (False, {})


async def test_any_step_after_a_copy_remade_tells_the_thread(api, monkeypatch, pods):
    thread = await a_thread(api)
    gone: list = []

    async def the_pod_goes_once(harness):
        if not gone:
            gone.append(await pods.destroy(next(iter(pods.pods))))

    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "a.md", "content": "a"})),
        calling(("memory", {"action": "add", "content": "Name: Ana"})),
        calling(("memory", {"action": "add", "content": "City: Iasi"})),  # a harness tool, not the pod's
        _final_response("Done."),
    ], pool=SandboxPool(pods), during=the_pod_goes_once)
    events = await api.app.state.session_store.get_events(thread.id, types=[EventType.TOOL_RESULT])
    first, second = [e.data["content"] for e in events if e.data["name"] == "memory"]
    assert not first.startswith("[This thread's copy") and second.startswith("[This thread's copy")


async def test_a_crashed_turns_retry_is_told_its_copy_was_made_fresh(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Edit the report."})
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    harness = a_waking_harness(store, SlashCommandConfig())
    harness._compressor.prune_stale_browser_states.side_effect = lambda messages: messages
    harness._redis, harness._session_factory, harness._sandbox_pool = api.app.state.redis, api.app.state.session_factory, pool

    async def crashes_after_its_first_step(session, *_, **__):
        await open_pod(pool, session)
        (pods.copies[str(session.id)] / "a.md").write_text("a")
        await a_step_ran(store, session)
        raise RuntimeError("the worker crashed mid-step")

    harness._run_loop = crashes_after_its_first_step
    with pytest.raises(RuntimeError):
        await harness.wake(thread.id)
    # The retry starts from a copy made afresh, and its first result says so.
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "a.md", "content": "a"})),
        _final_response("Done."),
    ], pool=pool)
    first = [e.data["content"] for e in await store.get_events(thread.id, types=[EventType.TOOL_RESULT])][-1]
    assert first.startswith("[This thread's copy of the project's files was made again"), first


async def test_a_thread_refuses_every_tool_that_starts_a_session():
    from surogates.harness.tool_exec import SESSION_STARTING_TOOLS, THREAD_REFUSED_TOOLS

    # Derived from the harness's own list: a new spawning tool is refused by default.
    assert THREAD_REFUSED_TOOLS == SESSION_STARTING_TOOLS - {"send_worker_message", "unblock_task", "message_thread"}
    assert {"delegate_task", "spawn_worker", "spawn_task", "dispatch_experiments", "cron_create"} <= THREAD_REFUSED_TOOLS


async def test_a_threads_loop_starts_no_run_to_edit_a_copy_never_landed(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "/loop 5m Add a line to Report.docx."})
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    harness = a_waking_harness(store, SlashCommandConfig())
    harness._compressor.prune_stale_browser_states.side_effect = lambda messages: messages
    harness._redis, harness._session_factory, harness._sandbox_pool = api.app.state.redis, api.app.state.session_factory, pool
    await asyncio.wait_for(harness.wake(thread.id), 60)
    [answer] = [e.data["message"]["content"] for e in await store.get_events(thread.id, types=[EventType.LLM_RESPONSE])]
    assert answer == "A thread can't start /loop yet: do this step in the thread itself."
    async with api.app.state.session_factory() as db:
        runs = (await db.execute(text("SELECT count(*) FROM sessions WHERE parent_id = :id"), {"id": thread.id})).scalar()
    assert (runs, pods.pods) == (0, {})
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"


async def a_step_ran(store, session) -> None:
    """A step of *session*'s turn, in its log as the harness writes one: its call, taken after a snapshot, then its result."""
    call = {"tool_call_id": "c1", "name": "write_file", "arguments": {}, "checkpoint_hash": "0" * 40}
    await store.emit_event(session.id, EventType.TOOL_CALL, call)
    await store.emit_event(session.id, EventType.TOOL_RESULT, {"tool_call_id": "c1", "name": "write_file", "content": "{}"})


async def last_writes(store, thread) -> list[str]:
    return [e.data["content"] for e in await store.get_events(thread.id, types=[EventType.TOOL_RESULT])
            if e.data["name"] == "write_file"]


def a_waking_thread_harness(api, monkeypatch, pool, turn):
    """A worker whose wake runs for real over *pool*, its turn *turn*."""
    monkeypatch.setattr(loop_module, "resolve_agent_def", AsyncMock(return_value=None))
    harness = a_waking_harness(api.app.state.session_store, SlashCommandConfig())
    harness._compressor.prune_stale_browser_states.side_effect = lambda messages: messages
    harness._redis, harness._session_factory, harness._sandbox_pool = api.app.state.redis, api.app.state.session_factory, pool
    harness._run_loop = turn
    return harness


async def test_a_retried_wake_that_ends_normally_keeps_its_copy(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Edit the report."})

    async def ends_keeping_its_pod(session, *_, **__):  # as a turn the provider failed does
        await open_pod(pool, session)

    harness = a_waking_thread_harness(api, monkeypatch, pool, ends_keeping_its_pod)
    try:
        raise RuntimeError("the first attempt crashed")
    except RuntimeError:
        await harness.wake(thread.id)  # the dispatcher retries inside its handler
    assert pool.holds_copy(str(thread.id))


async def test_a_cut_off_turns_slow_put_back_finishes_before_its_pod_goes(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, pool = api.app.state.session_store, CancelledOnTheThird(pods)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Write five notes."})
    monkeypatch.setattr(landing_module, "_PUT_BACK_BOUND", 0.2)
    unapply = History.unapply

    def slowly(self, *args, **kwargs):  # in the pod's child: each put-back outlasts the bound
        time.sleep(1)
        return unapply(self, *args, **kwargs)

    monkeypatch.setattr(History, "unapply", slowly)

    async def writes_five_and_lands(session, messages, system_prompt, lease, **_):
        await open_pod(pool, session)
        for name in "abcde":
            (pods.copies[str(session.id)] / f"{name}.md").write_text(name)
        await harness._complete_session(session, messages, lease, reason="completed")

    harness = a_waking_thread_harness(api, monkeypatch, pool, writes_five_and_lands)
    harness._saga_settings = QUICK
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.create_task(harness.wake(thread.id)), 60)
    async with asyncio.timeout(30):  # the put-back, then its pod's going
        while landing_module._PUTTING_BACK or landing_module._TEARDOWNS:
            await asyncio.sleep(0.1)
    # Every applied file was put back: no half-landed turn, and then the pod went.
    assert sorted(p.name for p in pods.project.iterdir()) == ["Report.docx", "notes.txt"]
    assert pods.pods == {}


async def test_a_turn_resumed_on_another_worker_is_told_its_copy_is_fresh_and_a_later_turn_is_not(api, monkeypatch, pods):
    thread = await a_thread(api)
    store = api.app.state.session_store
    first_worker, second_worker = SandboxPool(pods), SandboxPool(pods)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Edit the report."})

    async def a_step_then_the_lease_goes(session, *_, **__):
        await open_pod(first_worker, session)
        (pods.copies[str(session.id)] / "a.md").write_text("a")
        await a_step_ran(store, session)
        raise asyncio.CancelledError  # the lease went to another worker

    with pytest.raises(asyncio.CancelledError):
        await a_waking_thread_harness(api, monkeypatch, first_worker, a_step_then_the_lease_goes).wake(thread.id)

    async def last_result() -> str:
        return [e.data["content"] for e in await store.get_events(thread.id, types=[EventType.TOOL_RESULT])][-1]

    # The worker that took the lease resumes the turn on a copy made afresh, and is told.
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "b.md", "content": "b"})), _final_response("Done."),
    ], pool=second_worker)
    assert (await last_result()).startswith("[This thread's copy of the project's files was made again")
    # A later turn of the thread, on the first worker, has lost nothing, and is not told.
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Now the budget."})
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "c.md", "content": "c"})), _final_response("Done."),
    ], pool=first_worker)
    assert not (await last_result()).startswith("[This thread's copy")


PLAN_FIRST = [
    calling(("skill_view", {"name": "docx"})),
    calling(("todo", {"todos": [{"id": "1", "content": "Write it", "status": "in_progress"}]})),
]


async def test_a_turn_that_reads_and_plans_before_it_writes_is_not_told_its_copy_is_fresh(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    # The thread's first turn, then a turn after one that landed.
    for n, name in enumerate(("a.md", "b.md")):
        if n:
            await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "And another."})
        await a_turn(api, monkeypatch, thread, [
            *PLAN_FIRST, calling(("write_file", {"path": name, "content": name})), _final_response("Done."),
        ], pool=pool)
        assert not (await last_writes(store, thread))[-1].startswith("[This thread's copy")


async def test_a_thread_is_told_only_of_work_since_its_last_landed_turn(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    for n, name in enumerate(("a.md", "b.md")):  # two turns that land
        if n:
            await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "And another."})
        await a_turn(api, monkeypatch, thread, [
            calling(("write_file", {"path": name, "content": name})), _final_response("Done."),
        ], pool=pool)
    gone: list = []

    async def the_pod_goes_once(harness):
        if not gone:
            gone.append(await pods.destroy(next(iter(pods.pods))))

    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Two more."})
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "c.md", "content": "c"})),
        calling(("memory", {"action": "add", "content": "Name: Ana"})),
        calling(("write_file", {"path": "d.md", "content": "d"})),
        _final_response("Done."),
    ], pool=pool, during=the_pod_goes_once)
    # Its first step lost nothing the last landed turn did not hold; its step after the pod went did.
    first, after_the_pod_went = (await last_writes(store, thread))[-2:]
    assert not first.startswith("[This thread's copy") and after_the_pod_went.startswith("[This thread's copy")


async def test_a_turn_after_a_landing_that_rolled_back_is_told_its_copy_lacks_that_work(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    apply = History.apply

    def a_save_lands_first(self, path, before, after):
        if path == "c.md":
            (self.project / "c.md").write_text("saved by you just now")
        return apply(self, path, before, after)

    with monkeypatch.context() as patch:
        patch.setattr(History, "apply", a_save_lands_first)
        await a_turn(api, monkeypatch, thread, [
            calling(("terminal", {"command": "for f in a b c; do echo $f > $f.md; done"})),
            _final_response("Wrote three notes."),
        ], pool=pool, saga_settings=QUICK)
    [report] = await reports(api, master)
    assert report["landing"] == "compensated"
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Try again."})
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "x.md", "content": "x"})), _final_response("Done."),
    ], pool=pool)
    assert (await last_writes(store, thread))[-1].startswith("[This thread's copy")


async def test_a_threads_code_command_runs_no_coding_agent_on_a_copy_never_landed(api, monkeypatch, pods):
    thread = await a_thread(api)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "/code claude Add a line to notes.txt."})
    ran: list = []

    async def a_coding_run(session, cmd, lease, all_events):  # the CLI's edit, through the pod
        ran.append(cmd)
        await open_pod(pool, session)
        await pool.execute(sandbox_session_key(session), "terminal", json.dumps({"command": "echo by the agent >> notes.txt"}))

    harness = a_waking_thread_harness(api, monkeypatch, pool, AsyncMock())
    harness._run_code_agent = a_coding_run
    await asyncio.wait_for(harness.wake(thread.id), 60)
    [answer] = [e.data["message"]["content"] for e in await store.get_events(thread.id, types=[EventType.LLM_RESPONSE])]
    assert answer == "A thread can't start /code yet: do this step in the thread itself."
    assert (ran, pods.pods) == ([], {})
    assert (pods.project / "notes.txt").read_text() == "v1 notes\n"
