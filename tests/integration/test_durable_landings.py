"""A landing's durable record, and a landing a killed worker left running."""

from __future__ import annotations

import asyncio
import json
import time
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from surogates.db.models import WorkstreamHistory
from surogates.governance.saga import SagaOrchestrator
from surogates.harness import landing as landing_module
from surogates.sandbox.history import History
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.workstreams import history as rows_module
from tests.test_steer_loop import _final_response
from tests.thread_pods import ThreadPods

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_thread_copies import a_step_ran, a_thread, git, last_writes, open_pod, pods, reports  # noqa: F401  (pods is a fixture)
from .test_turn_sagas import a_turn, calling
from .test_workstream_threads import harness_of
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: A step timeout of a second: the fence a killed worker's landing is waited out for.
FENCED = SimpleNamespace(default_step_timeout=1, default_max_retries=0, retry_delay=0)


async def rows(api, thread) -> list[WorkstreamHistory]:
    async with api.app.state.session_factory() as db:
        return list((await db.execute(
            select(WorkstreamHistory).where(WorkstreamHistory.thread_id == thread.id).order_by(WorkstreamHistory.id)
        )).scalars())


async def ends(api, pool, thread, *, failed: bool = False, settings=FENCED) -> None:
    """*thread*'s turn ends, or fails, as a worker ends it."""
    store = api.app.state.session_store
    harness = harness_of(api)
    harness._sandbox_pool, harness._saga_settings, harness._storage = pool, settings, api.app.state.storage
    harness._tenant = SimpleNamespace(org_id=thread.org_id, user_id=thread.user_id)
    lease = await store.try_acquire_lease(thread.id, f"worker-{thread.id}", ttl_seconds=60)
    messages = [{"role": "assistant", "content": "Done."}]
    if failed:
        await harness._fail_session(thread, messages, lease, reason="provider_error")
    else:
        await harness._complete_session(thread, messages, lease, reason="completed", turn_id="turn-1")
    await store.release_lease(thread.id, lease.lease_token)


def stored(api, thread, tmp_path) -> ThreadPods:
    """Pods over *thread*'s project's files where the storage keeps them, as a pod's geesefs mounts them."""
    storage = api.app.state.storage
    project = storage._resolve(thread.config["storage_bucket"], boundary_workspace_prefix(thread.config, thread, thread.id))
    pods = ThreadPods(tmp_path, project=project)
    (pods.project / "Report.docx").write_bytes(b"PK\x03\x04 report v1")
    (pods.project / "notes.txt").write_text("v1 notes\n")
    return pods


async def edited(pool, thread, command: str) -> None:
    """*thread*'s pod, opened now, and *command* run in its copy."""
    await open_pod(pool, thread)
    await pool.execute(str(thread.id), "terminal", json.dumps({"command": command}))


async def test_the_history_table_is_made_with_its_indexes(api):
    async with api.app.state.session_factory() as db:
        names = set((await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'workstream_history'"))).scalars())
    assert {"idx_workstream_history_workstream", "idx_workstream_history_files", "idx_workstream_history_running"} <= names


async def test_a_landings_row_is_its_saga_written_as_it_runs(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "printf ' by A' >> Report.docx && echo a > a.md")
    seen: list[tuple[str, list[str]]] = []
    save = rows_module.save_landing

    async def watched(session_factory, row, saga, **kwargs):
        await save(session_factory, row, saga, **kwargs)
        async with session_factory() as db:  # another connection sees it at once
            written = await db.get(WorkstreamHistory, row)
            seen.append((written.saga_state, [s["state"] for s in written.steps]))

    monkeypatch.setattr(landing_module, "save_landing", watched)
    await ends(api, pool, thread)
    # Before the first apply, the steps as fixed; then each step's state as it changes.
    assert seen[:2] == [("running", ["committed"]), ("running", ["committed", "pending", "pending"])]
    # The record, fixed before its first try.
    assert ("running", ["committed", "committed", "committed", "pending"]) in seen
    assert seen[-1][0] == "completed"
    [row] = await rows(api, thread)
    assert (row.kind, row.saga_state, str(row.workstream_id)) == ("landing", "completed", thread.config["workstream_id"])
    assert [(s["tool_name"], s["state"]) for s in row.steps] == [
        ("history.commit", "committed"), ("history.apply", "committed"), ("history.apply", "committed"),
        ("history.record", "committed"),
    ]
    assert sorted((f["path"], f["merged"]) for f in row.files) == [("Report.docx", True), ("a.md", True)]
    assert row.commit == git(pods.project / "_history", "rev-parse", "refs/heads/main")
    assert git(pods.project / "_history", "log", "-1", "--format=%(trailers:key=Surogate-Saga,valueonly)", row.commit) == row.saga_id


async def test_a_thread_lands_over_three_turns_in_three_pods(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    store = api.app.state.session_store
    for n in range(3):
        await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": f"Add part {n}."})
        await a_turn(api, monkeypatch, thread, [
            calling(("terminal", {"command": f"echo part {n} >> Report.docx && echo {n} > part{n}.md"})),
            _final_response(f"Added part {n}."),
        ], pool=SandboxPool(pods))  # a pool, and a pod, per turn
    # Each turn started from the last one's landing: the report has all three parts.
    assert (pods.project / "Report.docx").read_bytes().endswith(b"part 0\npart 1\npart 2\n")
    assert pods.real_names() == ["Report.docx", "notes.txt", "part0.md", "part1.md", "part2.md"]
    landings = git(pods.project / "_history", "rev-list", "--first-parent", "refs/heads/main").splitlines()
    assert [r.commit for r in await rows(api, thread)] == landings[:3][::-1]
    assert all(f["landing"] == "landed" for report in await reports(api, master) for f in report["files"])


async def test_a_landing_whose_push_answer_was_lost_counts_and_is_not_put_back(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    call = landing_module._call

    async def the_push_answer_is_lost(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "record":
            raise landing_module.LandingStepError("the pod's step timed out")
        return result

    monkeypatch.setattr(landing_module, "_call", the_push_answer_is_lost)
    await ends(api, pool, thread)
    assert pods.real_names() == ["Report.docx", "a.md", "notes.txt"]
    [row] = await rows(api, thread)
    assert (row.saga_state, row.commit) == ("completed", git(pods.project / "_history", "rev-parse", "refs/heads/main"))
    [report] = await reports(api, master)
    assert "landing" not in report and [(f["ref"], f["landing"]) for f in report["files"]] == [("a.md", "landed")]


#: A try of a second, one retry, two seconds' wait before it.
WAITS = SimpleNamespace(default_step_timeout=1, default_max_retries=1, retry_delay=2)


async def test_a_landing_cancelled_while_it_waits_to_retry_a_push_whose_answer_was_lost_counts(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    call, pushed = landing_module._call, asyncio.Event()

    async def the_push_answer_is_lost(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "record":
            pushed.set()
            await asyncio.sleep(5)  # the try times out
        return result

    monkeypatch.setattr(landing_module, "_call", the_push_answer_is_lost)
    turn = asyncio.create_task(ends(api, pool, thread, settings=WAITS))
    await pushed.wait()
    await asyncio.sleep(1.5)  # past the try, in the wait before the retry
    turn.cancel()  # the worker shuts down, or its lease goes
    with pytest.raises(asyncio.CancelledError):
        await turn
    # It pushed: it counts, and nothing is put back.
    assert pods.real_names() == ["Report.docx", "a.md", "notes.txt"]
    [row] = await rows(api, thread)
    assert (row.saga_state, row.commit) == ("completed", git(pods.project / "_history", "rev-parse", "refs/heads/main"))


async def test_a_landing_whose_main_moved_without_its_saga_is_put_back(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    call = landing_module._call

    async def another_lands_first(sandbox_pool, owner, action, **arguments):
        if action == "record":
            # main moved, by no push of this landing's: a lock lost, or a thread's command.
            refs = pods.project / "_history" / "packed-refs"
            main = git(pods.project / "_history", "rev-parse", "refs/heads/main")
            other = git(pods.project / "_history", "commit-tree", f"{main}^{{tree}}", "-p", main, "-m", "another")
            refs.write_text(refs.read_text().replace(f"{main} refs/heads/main", f"{other} refs/heads/main"))
        return await call(sandbox_pool, owner, action, **arguments)

    monkeypatch.setattr(landing_module, "_call", another_lands_first)
    await ends(api, pool, thread)
    # It never counts as landed: main does not carry its saga.
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    [row] = await rows(api, thread)
    assert (row.saga_state, row.commit) == ("compensated", None)


async def test_running_landings_are_the_projects_own_oldest_first_with_how_long_each_is_quiet(api):
    master = await master_of(api, await create(api))
    first, second, elsewhere = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master), await a_thread(api)
    factory = api.app.state.session_factory

    async def a_landing(thread) -> tuple[int, object]:
        saga = SagaOrchestrator().create_saga(thread.id, kind="landing")
        return await rows_module.start_landing(
            factory, saga, workstream_id=thread.config["workstream_id"], thread_id=thread.id,
            agent_id=str(thread.agent_id), user_id=thread.user_id, tool_saga_id=None, events=None,
        ), saga

    (older, _), (done, saga), (newer, _) = await a_landing(first), await a_landing(first), await a_landing(second)
    await a_landing(elsewhere)  # another project's
    await rows_module.save_landing(factory, done, saga, state="completed")
    async with factory() as db:
        await db.execute(text("UPDATE workstream_history SET updated_at = now() - interval '10 seconds' WHERE id = :id"), {"id": older})
        await db.commit()
    running = await rows_module.running_landings(factory, first.config["workstream_id"])
    assert [row.id for row, _ in running] == [older, newer]
    assert running[0][1] >= 10 > running[1][1]
    await rows_module.touch_landing(factory, older)
    [(_, quiet), _] = await rows_module.running_landings(factory, first.config["workstream_id"])
    assert quiet < 10


async def a_landing_killed(api, monkeypatch, pool, thread, *, after: str) -> None:
    """*thread*'s landing as a SIGKILL leaves it, once the pod has answered *after*: nothing more written, nothing put back.

    *after* is an action, or ``apply <path>``.
    """
    call, save, touch = landing_module._call, landing_module.save_landing, landing_module.touch_landing
    killed = asyncio.Event()

    async def dies(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if owner == str(thread.id) and after in (action, f"{action} {arguments.get('path')}"):
            killed.set()
            raise asyncio.CancelledError  # a kill the worker never comes back from
        return result

    async def until_killed(write, *args, **kwargs):
        if not killed.is_set():
            await write(*args, **kwargs)

    async def never_settles(*args, **kwargs):
        await asyncio.Event().wait()

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "_call", dies)
        patch.setattr(landing_module, "save_landing", partial(until_killed, save))
        patch.setattr(landing_module, "touch_landing", partial(until_killed, touch))
        patch.setattr(landing_module, "_settle", never_settles)
        patch.setattr(landing_module, "_PUT_BACK_BOUND", 0.1)
        with pytest.raises(asyncio.CancelledError):
            await ends(api, pool, thread)
    landing_module._PUTTING_BACK.pop(str(thread.id)).cancel()
    async with api.app.state.session_factory() as db:  # its lease runs out, as a dead worker's does
        await db.execute(text("DELETE FROM session_leases WHERE session_id = :id"), {"id": thread.id})
        await db.commit()


async def test_a_worker_killed_after_two_applies_is_put_back_by_the_next_landing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "for f in a b c d; do echo $f > $f.md; done")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply b.md")
    assert pods.real_names() == ["Report.docx", "a.md", "b.md", "notes.txt"]  # half landed
    [row] = await rows(api, first)
    # b.md was written, and the row still has its apply pending: the kill came before its state was.
    assert row.saga_state == "running"
    assert [s["state"] for s in row.steps if s["tool_name"] == "history.apply"] == ["committed", "pending", "pending", "pending"]

    await edited(pool, second, "echo by B > B.md")
    started = time.monotonic()
    await ends(api, pool, second)
    # The killed landing's row was waited out, then put back.
    assert time.monotonic() - started >= FENCED.default_step_timeout
    assert pods.real_names() == ["B.md", "Report.docx", "notes.txt"]
    [row] = await rows(api, first)
    assert row.saga_state == "compensated"
    # Its turn stays on its branch, to land with the thread's next turn.
    assert git(pods.project / "_history", "ls-tree", "--name-only", f"refs/heads/threads/{first.id}").splitlines() == [
        "Report.docx", "a.md", "b.md", "c.md", "d.md", "notes.txt",
    ]


async def test_a_worker_killed_right_after_its_push_is_completed_and_its_thread_told(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    await a_landing_killed(api, monkeypatch, pool, thread, after="record")
    pushed = git(pods.project / "_history", "rev-parse", "refs/heads/main")
    [row] = await rows(api, thread)
    assert [(s["tool_name"], s["state"]) for s in row.steps][-1] == ("history.record", "pending")

    # The thread's next turn, in a pod of its own, settles it first.
    await edited(pool, thread, "echo more > more.md")
    await ends(api, pool, thread)
    killed, _ = await rows(api, thread)
    assert (killed.saga_state, killed.commit) == ("completed", pushed)
    assert [(f["path"], f["merged"]) for f in killed.files] == [("a.md", True)]
    assert pods.real_names() == ["Report.docx", "a.md", "more.md", "notes.txt"]
    [report] = await reports(api, master)
    assert sorted((f["ref"], f["landing"]) for f in report["files"]) == [("a.md", "landed"), ("more.md", "landed")]


async def test_a_worker_killed_while_putting_back_is_put_back_again_by_the_next_landing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo more >> notes.txt && echo z > z.md")
    call, compensate = landing_module._call, landing_module.compensate_step
    save, touch = landing_module.save_landing, landing_module.touch_landing
    killed = asyncio.Event()
    put_back: list[tuple[str, str]] = []

    async def z_fails(sandbox_pool, owner, action, **arguments):
        if owner == str(first.id) and action == "apply" and arguments["path"] == "z.md":
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    async def dies_before_a(it, *, sandbox_pool, session_id):
        if session_id == str(first.id) and it.arguments.get("path") == "a.md":
            killed.set()
            raise asyncio.CancelledError  # notes.txt is put back; the row says a.md's put-back is under way
        if it.tool_name == "history.apply":
            put_back.append((session_id, it.arguments["path"]))
        return await compensate(it, sandbox_pool=sandbox_pool, session_id=session_id)

    async def until_killed(write, *args, **kwargs):
        if not killed.is_set():
            await write(*args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", z_fails)
    monkeypatch.setattr(landing_module, "compensate_step", dies_before_a)
    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "save_landing", partial(until_killed, save))
        patch.setattr(landing_module, "touch_landing", partial(until_killed, touch))
        with pytest.raises(asyncio.CancelledError):
            await ends(api, pool, first)
    async with api.app.state.session_factory() as db:  # its lease runs out, as a dead worker's does
        await db.execute(text("DELETE FROM session_leases WHERE session_id = :id"), {"id": first.id})
        await db.commit()
    [row] = await rows(api, first)
    assert row.saga_state == "running"
    assert [(s["tool_name"], s["arguments"].get("path"), s["state"]) for s in row.steps] == [
        ("history.commit", None, "committed"), ("history.apply", "a.md", "compensating"),
        ("history.apply", "notes.txt", "compensated"), ("history.apply", "z.md", "failed"),
    ]
    assert pods.real_names() == ["Report.docx", "a.md", "notes.txt"]
    assert (pods.project / "notes.txt").read_text() == "v1 notes\n"

    await edited(pool, second, "echo by B > B.md")
    await ends(api, pool, second)
    # The put-back the kill cut off runs again, and the one done does not: the landing is undone whole.
    assert put_back == [(str(first.id), "notes.txt"), (str(second.id), "a.md")]
    [row] = await rows(api, first)
    assert row.saga_state == "compensated"
    assert pods.real_names() == ["B.md", "Report.docx", "notes.txt"]
    assert (pods.project / "notes.txt").read_text() == "v1 notes\n"


#: Both workers' saga settings: tries of a second, three retries; a fence of two seconds.
RETRYING = SimpleNamespace(default_step_timeout=1, default_max_retries=3, retry_delay=0)
#: Both workers' saga settings: one try of three seconds; a fence of four.
SLOW = SimpleNamespace(default_step_timeout=3, default_max_retries=0, retry_delay=0)


async def lose_the_lock(api, thread) -> None:
    """*thread*'s project's lock lost unseen, as a failover or a pooler restart loses it: its connection ends."""
    async with api.app.state.session_factory() as db:
        await db.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND objid::text::bigint = (hashtext(:key)::bigint & 4294967295)"
        ), {"key": f"workstream:{thread.config['workstream_id']}"})
        await db.commit()


async def test_a_landing_that_lost_its_lock_while_a_step_retried_is_waited_for_and_stops(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "for f in a b c d; do echo $f > $f.md; done")
    await edited(pool, second, "echo by B > B.md")
    call, put_back = landing_module._call, landing_module._put_back
    tries, through = [], []

    async def slow_to_answer(sandbox_pool, owner, action, **arguments):
        if owner == str(first.id) and action == "apply" and arguments["path"] == "b.md" and len(tries) < 3:
            tries.append(owner)
            await asyncio.sleep(10)  # cut off by the try's timeout
        return await call(sandbox_pool, owner, action, **arguments)

    async def watched(saga, orchestrator, sandbox_pool, owner, *args, **kwargs):
        through.append(owner)
        return await put_back(saga, orchestrator, sandbox_pool, owner, *args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", slow_to_answer)
    monkeypatch.setattr(landing_module, "_put_back", watched)
    a = asyncio.create_task(ends(api, pool, first, settings=RETRYING))
    while not tries:
        await asyncio.sleep(0.05)
    await lose_the_lock(api, first)
    await asyncio.wait_for(ends(api, pool, second, settings=RETRYING), 30)
    await asyncio.wait_for(a, 30)
    [a_row], [b_row] = await rows(api, first), await rows(api, second)
    # A wrote its row at each try, so B waited; A found its lock gone, and put itself back.
    assert len(tries) == 3 and through == [str(first.id)]
    assert (a_row.saga_state, a_row.commit) == ("compensated", None)
    assert b_row.created_at >= a_row.updated_at
    assert (b_row.saga_state, pods.real_names()) == ("completed", ["B.md", "Report.docx", "notes.txt"])


async def test_a_landing_that_lost_its_lock_never_writes_over_the_next_landing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "printf ' by A' >> Report.docx && echo a > a.md")
    await edited(pool, second, "printf ' by B' >> Report.docx")
    stalled, release = pods.root / "stalled", pods.root / "release"
    put = History._put

    def stalls(self, path, blob):
        # A's write of the report, slow after its check, as on a stalled mount: in the pod's own process.
        if self.thread == str(first.id) and path == "Report.docx":
            stalled.touch()
            while not release.exists():
                time.sleep(0.05)
        return put(self, path, blob)

    monkeypatch.setattr(History, "_put", stalls)
    a = asyncio.create_task(ends(api, pool, first, settings=SLOW))
    while not stalled.exists():
        await asyncio.sleep(0.05)
    await lose_the_lock(api, first)
    b = asyncio.create_task(ends(api, pool, second, settings=SLOW))
    await asyncio.sleep(0.5)
    release.touch()  # within A's try
    await asyncio.wait_for(asyncio.gather(a, b), 30)
    [a_row], [b_row] = await rows(api, first), await rows(api, second)
    assert (a_row.saga_state, a_row.commit) == ("compensated", None)
    assert b_row.saga_state == "completed"
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1 by B"


async def test_a_failed_turns_work_is_on_its_branch_at_the_next_turn_and_lands_then(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, "printf ' half made' >> Report.docx")
    await ends(api, pool, thread, failed=True)
    # Not landed, kept on its branch; and its pod is gone.
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{thread.id}:Report.docx") == "PK\x03\x04 report v1 half made"
    assert not pool.holds_copy(str(thread.id))
    # Its next turn uses no tool, and lands it all the same.
    await ends(api, SandboxPool(pods), thread)
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1 half made"
    [report] = await reports(api, master)
    assert [(f["ref"], f["landing"]) for f in report["files"]] == [("Report.docx", "landed")]


async def test_a_turn_that_never_used_its_pod_and_has_nothing_waiting_opens_none(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    await ends(api, SandboxPool(pods), thread)
    assert pods.pods == {}


async def test_a_thread_whose_failed_turn_was_kept_is_not_told_its_work_is_gone(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "a.md", "content": "a"})),
        _final_response("Done."),
    ], pool=pool, saga_settings=FENCED)
    store = api.app.state.session_store
    # A turn that ran a step, then failed: its copy is kept.
    await edited(pool, thread, "echo b > b.md")
    await a_step_ran(store, thread)
    await ends(api, pool, thread, failed=True)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Go on."})
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "c.md", "content": "c"})),
        _final_response("Done."),
    ], pool=SandboxPool(pods), saga_settings=FENCED)
    # The next turn's first step, on a copy made again: it has the failed turn's work.
    first = (await last_writes(store, thread))[-1]
    assert not first.startswith("[This thread's copy"), first
    assert pods.real_names() == ["Report.docx", "a.md", "b.md", "c.md", "notes.txt"]


async def test_a_put_back_goes_on_when_its_rows_cannot_be_written(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo b > b.md")
    call, save, down = landing_module._call, landing_module.save_landing, []

    async def b_fails_and_the_database_goes(sandbox_pool, owner, action, **arguments):
        if action == "apply" and arguments["path"] == "b.md":
            down.append(True)
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    async def unwritten(*args, **kwargs):
        if down:
            raise ConnectionError("the database is down")
        await save(*args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", b_fails_and_the_database_goes)
    monkeypatch.setattr(landing_module, "save_landing", unwritten)
    await ends(api, pool, thread)
    # All or nothing all the same: a.md is put back, and the landing is rolled back, not escalated.
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    [report] = await reports(api, master)
    assert report["landing"] == "compensated"


async def test_a_row_write_that_fails_once_never_fails_a_put_back_that_worked(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo b > b.md")
    call, compensate, save, putting_back = landing_module._call, landing_module.compensate_step, landing_module.save_landing, []

    async def the_lock_goes_after_a(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "apply" and arguments["path"] == "a.md":
            await lose_the_lock(api, thread)  # nothing failed: the landing stops before b.md
        return result

    async def watched(it, **kwargs):
        putting_back.append(True)
        return await compensate(it, **kwargs)

    async def fails_once(*args, **kwargs):
        if putting_back == [True]:
            putting_back.append(False)
            raise ConnectionError("the database blinked")
        await save(*args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", the_lock_goes_after_a)
    monkeypatch.setattr(landing_module, "compensate_step", watched)
    monkeypatch.setattr(landing_module, "save_landing", fails_once)
    await ends(api, pool, thread)
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    [row] = await rows(api, thread)
    assert row.saga_state == "compensated"
    assert {s["state"] for s in row.steps if s["tool_name"] == "history.apply"} == {"compensated", "pending"}
    assert putting_back[:2] == [True, False]  # the write after a.md's put-back failed


async def test_each_put_back_is_in_the_row_before_it_runs(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo b > b.md && echo c > c.md")
    call, compensate, seen = landing_module._call, landing_module.compensate_step, []

    async def c_fails(sandbox_pool, owner, action, **arguments):
        if action == "apply" and arguments["path"] == "c.md":
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    async def watched(it, **kwargs):
        if it.tool_name == "history.apply":
            [row] = await rows(api, thread)  # from another connection, as the next lock holder reads it
            seen.append(next((s["arguments"]["path"], s["state"]) for s in row.steps if s["step_id"] == it.step_id))
        return await compensate(it, **kwargs)

    monkeypatch.setattr(landing_module, "_call", c_fails)
    monkeypatch.setattr(landing_module, "compensate_step", watched)
    await ends(api, pool, thread)
    # A put-back the row shows done is done: a worker killed in one leaves it compensating, to run again.
    assert seen == [("b.md", "compensating"), ("a.md", "compensating")]


@pytest.mark.parametrize("fails", ["the lock's connection", "the row's write"])
async def test_a_cancel_the_database_fails_under_stays_a_cancel_and_its_files_go_back(api, monkeypatch, pods, fails):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo b > b.md")
    call, save, down = landing_module._call, landing_module.save_landing, []

    async def cancelled_after_a(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "apply" and arguments["path"] == "a.md":
            if fails == "the lock's connection":
                await lose_the_lock(api, thread)
            else:
                down.append(True)
            asyncio.current_task().cancel()  # the turn's lease went to another worker
            await asyncio.sleep(0)
        return result

    async def unwritten(*args, **kwargs):
        if down:
            raise ConnectionError("the database is down")
        await save(*args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", cancelled_after_a)
    monkeypatch.setattr(landing_module, "save_landing", unwritten)
    turn = asyncio.create_task(ends(api, pool, thread))
    # The worker stops ending a turn whose lease has moved.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, 30)
    assert pods.real_names() == ["Report.docx", "notes.txt"]


async def test_the_locks_check_fails_once_its_block_has_ended(api):
    async with rows_module.project_lock(api.app.state.session_factory, uuid4()) as held:
        await held()
    # A put-back that outlives the block is never told the lock is still its own.
    with pytest.raises(RuntimeError, match="let go"):
        await held()


async def test_a_put_back_that_failed_before_its_worker_died_is_tried_again_by_the_next_landing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md && echo z > z.md")
    call, compensate = landing_module._call, landing_module.compensate_step
    save, touch = landing_module.save_landing, landing_module.touch_landing
    killed = asyncio.Event()

    async def z_fails(sandbox_pool, owner, action, **arguments):
        if owner == str(first.id) and action == "apply" and arguments["path"] == "z.md":
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    async def b_fails_then_the_worker_dies(it, *, sandbox_pool, session_id):
        if session_id == str(first.id) and it.arguments.get("path") == "b.md":
            raise landing_module.LandingStepError("the pod's step timed out")  # b.md stays as the turn wrote it
        result = await compensate(it, sandbox_pool=sandbox_pool, session_id=session_id)
        if session_id == str(first.id) and it.arguments.get("path") == "a.md":
            killed.set()
            raise asyncio.CancelledError  # before the row says escalated
        return result

    async def until_killed(write, *args, **kwargs):
        if not killed.is_set():
            await write(*args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", z_fails)
    monkeypatch.setattr(landing_module, "compensate_step", b_fails_then_the_worker_dies)
    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "save_landing", partial(until_killed, save))
        patch.setattr(landing_module, "touch_landing", partial(until_killed, touch))
        with pytest.raises(asyncio.CancelledError):
            await ends(api, pool, first)
    async with api.app.state.session_factory() as db:  # its lease runs out, as a dead worker's does
        await db.execute(text("DELETE FROM session_leases WHERE session_id = :id"), {"id": first.id})
        await db.commit()
    [row] = await rows(api, first)
    assert (row.saga_state, [s["state"] for s in row.steps]) == ("running", ["committed", "compensating", "compensation_failed", "failed"])
    assert pods.real_names() == ["Report.docx", "b.md", "notes.txt"]

    await edited(pool, second, "echo by B > B.md")
    await ends(api, pool, second)
    # b.md's put-back is tried again, and the landing is undone whole.
    [row] = await rows(api, first)
    assert row.saga_state == "compensated"
    assert pods.real_names() == ["B.md", "Report.docx", "notes.txt"]
