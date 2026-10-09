"""A landing's durable record, and a landing a killed worker left running."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest
from asyncpg.exceptions import InternalClientError
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from surogates.db.models import WorkstreamHistory
from surogates.governance.saga import SagaOrchestrator
from surogates.harness import landing as landing_module
from surogates.harness import loop_artifact_completion
from surogates.harness.loop_context_replay import not_handed_back, worker_note
from surogates.harness.tool_exec import _build_session_sandbox_spec
from surogates.sandbox.history import History
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.session.provisioning import create_child_session
from surogates.storage.tenant import boundary_workspace_prefix
from surogates.tools.builtin.delegate import _poll_child_completion
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
    # Its steps are written whole, so not at each: once fixed, before the first apply; with the record, before its first try; and at its end.
    assert seen == [
        ("running", ["committed", "pending", "pending"]),
        ("running", ["committed", "committed", "committed", "pending"]),
        ("completed", ["committed", "committed", "committed", "committed"]),
    ]
    [row] = await rows(api, thread)
    assert (row.kind, row.saga_state, str(row.workstream_id)) == ("landing", "completed", thread.config["workstream_id"])
    assert [(s["tool_name"], s["state"]) for s in row.steps] == [
        ("history.commit", "committed"), ("history.apply", "committed"), ("history.apply", "committed"),
        ("history.record", "committed"),
    ]
    assert sorted((f["path"], f["merged"]) for f in row.files) == [("Report.docx", True), ("a.md", True)]
    assert row.commit == git(pods.project / "_history", "rev-parse", "refs/heads/main")
    assert git(pods.project / "_history", "log", "-1", "--format=%(trailers:key=Surogate-Saga,valueonly)", row.commit) == row.saga_id


async def test_a_landing_of_a_few_hundred_files_writes_its_steps_a_handful_of_times(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    files = 300
    await edited(pool, thread, f"mkdir -p 'an archive' && for i in $(seq 1 {files}); do echo $i > \"an archive/a file with a long name $i.md\"; done")
    save, touch = rows_module.save_landing, rows_module.touch_landing
    written, touched = [], []

    async def sized(session_factory, row, saga, **kwargs):
        written.append(len(json.dumps(saga.to_dict()["steps"])))
        await save(session_factory, row, saga, **kwargs)

    async def counted(session_factory, row):
        touched.append(row)
        await touch(session_factory, row)

    monkeypatch.setattr(landing_module, "save_landing", sized)
    monkeypatch.setattr(landing_module, "touch_landing", counted)
    began = time.monotonic()
    await ends(api, pool, thread, settings=SimpleNamespace(default_step_timeout=30, default_max_retries=0, retry_delay=0))
    took = time.monotonic() - began
    [row] = await rows(api, thread)
    assert row.saga_state == "completed" and len(row.files) == files
    # Three writes whatever its size, and one more every few seconds: never one a file,
    # which made a landing's cost grow with the square of its files.
    most = 3 + took / landing_module._ROW_EVERY
    assert len(written) <= most and sum(written) <= most * written[-1], (len(written), took)
    # Each try of a step still marks the row alive, for another lock holder's fence.
    assert len(touched) + len(written) >= files + 2


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


#: How a landing's row stands when its worker dies.  Its steps are written at
#: most every few seconds, so a quick landing's row is ``behind``: as its steps
#: were fixed.  A slow one's is, at best, ``exact``: written at the try it died in.
ROWS = ["behind", "exact"]


def rows_stand(monkeypatch, how: str) -> None:
    if how == "exact":
        monkeypatch.setattr(landing_module, "_ROW_EVERY", 0)
        monkeypatch.setattr(landing_module, "_ROW_SHARE", 0)


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
    async with asyncio.timeout(10):  # its pod goes in the background: before the thread's next opens
        while pool.holds_copy(str(thread.id)):
            await asyncio.sleep(0.05)
    async with api.app.state.session_factory() as db:  # its lease runs out, as a dead worker's does
        await db.execute(text("DELETE FROM session_leases WHERE session_id = :id"), {"id": thread.id})
        await db.commit()


@pytest.mark.parametrize("row_is", ROWS)
async def test_a_worker_killed_after_two_applies_is_put_back_by_the_next_landing(api, monkeypatch, pods, row_is):
    rows_stand(monkeypatch, row_is)
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "for f in a b c d; do echo $f > $f.md; done")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply b.md")
    assert pods.real_names() == ["Report.docx", "a.md", "b.md", "notes.txt"]  # half landed
    [row] = await rows(api, first)
    # Two files were written, and the row shows neither done: at best a.md done and b.md under way.
    assert row.saga_state == "running"
    # The turn its row names is the one the history has: what the next lock holder fetches to put it back.
    assert row.steps[0]["result"]["commit"] == git(pods.project / "_history", "rev-parse", f"refs/heads/threads/{first.id}")
    assert [s["state"] for s in row.steps if s["tool_name"] == "history.apply"] == {
        "behind": ["pending", "pending", "pending", "pending"],
        "exact": ["committed", "executing", "pending", "pending"],
    }[row_is]

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


async def test_a_worker_killed_right_after_its_commit_step_leaves_the_turn_on_its_branch_to_land_next(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pods = stored(api, first, tmp_path)
    pool = SandboxPool(pods)
    await open_pod(pool, first)
    for step in ("draft", "a"):  # two steps, a snapshot before each as the harness takes them
        await pool.execute(str(first.id), "_checkpoint", json.dumps({"action": "take", "reason": "before a step"}))
        await pool.execute(str(first.id), "terminal", json.dumps({"command": f"echo {step} > a.md"}))
    await a_landing_killed(api, monkeypatch, pool, first, after="commit")
    # The pod made the turn's commit and pushed it; the worker died before its row named it.
    [row] = await rows(api, first)
    assert (row.saga_state, row.steps) == ("running", [])
    durable = pods.project / "_history"
    turn = git(durable, "rev-parse", f"refs/heads/threads/{first.id}")
    assert git(durable, "show", f"{turn}:a.md") == "a" and len(git(durable, "log", "-1", "--format=%P", turn).split()) == 1
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    # The next lock holder finds nothing of it in the real files, and reads no commit from its row.
    await edited(pool, second, "echo by B > B.md")
    await ends(api, pool, second)
    assert [r.saga_state for r in await rows(api, first)] == ["compensated"]
    # The turn waits on its branch, and lands with the thread's next turn, which uses no tool.
    await ends(api, SandboxPool(pods), first)
    assert pods.real_names() == ["B.md", "Report.docx", "a.md", "notes.txt"]
    assert (pods.project / "a.md").read_text() == "a\n"


async def test_a_commit_step_whose_answer_was_lost_is_tried_again_and_its_row_names_the_commit_the_history_has(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    call, pushed = landing_module._call, []

    async def the_first_answer_is_lost(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "commit":
            pushed.append((result["commit"], git(pods.project / "_history", "rev-parse", f"refs/heads/threads/{thread.id}")))
            if len(pushed) == 1:
                await asyncio.sleep(1.1)  # a later second, so that a commit dated by its try would differ
                raise landing_module.LandingStepError("the pod's step timed out")
        return result

    monkeypatch.setattr(landing_module, "_call", the_first_answer_is_lost)
    await ends(api, pool, thread, settings=SimpleNamespace(default_step_timeout=30, default_max_retries=1, retry_delay=0))
    # Both tries made and pushed one commit: the row cannot name a turn the history lacks.
    assert len(pushed) == 2 and len({commit for answer in pushed for commit in answer}) == 1
    [row] = await rows(api, thread)
    assert (row.saga_state, row.steps[0]["result"]["commit"]) == ("completed", pushed[0][0])
    assert git(pods.project / "_history", "rev-parse", f"{row.commit}^2") == pushed[0][0]
    assert pods.real_names() == ["Report.docx", "a.md", "notes.txt"]


@pytest.mark.parametrize("row_is", ROWS)
async def test_a_recovery_from_a_row_behind_its_landing_puts_back_what_it_knows_and_writes_over_nothing(api, monkeypatch, pods, row_is):
    rows_stand(monkeypatch, row_is)
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && mkdir -p made/deep && echo b > made/deep/b.md && echo more >> notes.txt && rm Report.docx")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply notes.txt")  # its deletion never ran
    assert (pods.project / "made" / "deep" / "b.md").exists() and (pods.project / "Report.docx").exists()
    (pods.project / "a.md").write_text("saved by you since\n")  # over the file the dead landing wrote

    await edited(pool, second, "echo by B > B.md")
    await ends(api, pool, second)
    # Each file the dead landing wrote is as it was before it, and yours is never written over.
    assert (pods.project / "notes.txt").read_text() == "v1 notes\n"
    assert not (pods.project / "made" / "deep" / "b.md").exists()
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert (pods.project / "a.md").read_text() == "saved by you since\n"
    [row] = await rows(api, first)
    told = [report for report in await reports(api, master) if report.get("landing") == "escalated"]
    if row_is == "exact":
        # A row that shows the applies done knows the folders they made, and that your save is a conflict.
        assert not (pods.project / "made").exists() and row.saga_state == "escalated"
        # The master is told of it, in a report of the dead landing's thread: B's own turn landed.
        [report] = told
        assert (report["worker_id"], report["recovered"], "gone" in report) == (str(first.id), True, False)
        assert [f["ref"] for f in report["files"]] == ["a.md", "made/deep/b.md", "notes.txt", "Report.docx"]
        assert worker_note(EventType.WORKER_COMPLETE.value, report)["content"] == (
            f'[Thread "Draft A" ({first.id}): a landing its worker left unfinished was settled]\n'
            "Could not finish landing these; check them: a.md, made/deep/b.md, notes.txt, Report.docx"
        )
    else:
        # A row behind them knows neither: the folders stay, empty, and the landing reads as put back.
        assert list((pods.project / "made").rglob("*")) == [pods.project / "made" / "deep"] and row.saga_state == "compensated"
        assert told == []


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


@pytest.mark.parametrize("row_is", ROWS)
async def test_a_worker_killed_while_putting_back_is_put_back_again_by_the_next_landing(api, monkeypatch, pods, row_is):
    rows_stand(monkeypatch, row_is)
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

    async def dies_after_a(it, *, sandbox_pool, session_id):
        if it.tool_name == "history.apply":
            put_back.append((session_id, it.arguments["path"]))
        result = await compensate(it, sandbox_pool=sandbox_pool, session_id=session_id)
        if session_id == str(first.id) and it.arguments.get("path") == "a.md":
            killed.set()
            raise asyncio.CancelledError  # a.md is put back, but the row still says its put-back is under way
        return result

    async def until_killed(write, *args, **kwargs):
        if not killed.is_set():
            await write(*args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", z_fails)
    monkeypatch.setattr(landing_module, "compensate_step", dies_after_a)
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
    # Never a put-back shown done that is not: at best the one done, and the one the kill cut off under way.
    assert [(s["tool_name"], s["arguments"].get("path"), s["state"]) for s in row.steps] == [
        ("history.commit", None, "committed"),
        ("history.apply", "a.md", {"behind": "committed", "exact": "compensating"}[row_is]),
        ("history.apply", "notes.txt", {"behind": "committed", "exact": "compensated"}[row_is]),
        ("history.apply", "z.md", "failed"),
    ]
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    assert (pods.project / "notes.txt").read_text() == "v1 notes\n"

    await edited(pool, second, "echo by B > B.md")
    await ends(api, pool, second)
    # The put-back the kill cut off runs again, over a file already back.  One the row shows done
    # does not; one done that the row had not caught up with does, as safely.
    again = {"behind": [(str(second.id), "notes.txt"), (str(second.id), "a.md")], "exact": [(str(second.id), "a.md")]}[row_is]
    assert put_back == [(str(first.id), "notes.txt"), (str(first.id), "a.md"), *again]
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


async def test_a_landing_that_lost_its_lock_after_its_last_apply_records_nothing(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo b > b.md")
    call = landing_module._call

    async def the_lock_goes_after_the_last_apply(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "apply" and arguments["path"] == "b.md":
            await lose_the_lock(api, thread)  # no apply is left to find it gone
        return result

    monkeypatch.setattr(landing_module, "_call", the_lock_goes_after_the_last_apply)
    await ends(api, pool, thread)
    # The record is the push that counts: it is not made without the lock, and the files go back.
    [row] = await rows(api, thread)
    assert (row.saga_state, row.commit) == ("compensated", None)
    assert "refs/heads/main" not in (pods.project / "_history" / "packed-refs").read_text()
    assert pods.real_names() == ["Report.docx", "notes.txt"]


async def test_a_recovery_puts_back_your_upload_that_the_dead_landing_wrote_over(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo start > start.md")
    await ends(api, pool, first)  # a first landing: main is made
    (pods.project / "upload.md").write_text("uploaded by you after the last landing\n")  # main never held it
    await edited(pool, first, "echo ' edited by A' >> upload.md && echo a > a.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply upload.md")
    assert "edited by A" in (pods.project / "upload.md").read_text()  # half landed, over your file
    await edited(pool, second, "echo by B > B.md")
    await ends(api, pool, second)
    # Your version was only in the dead pod's own pickup, its turn's base: the next holder fetched it by its id.
    assert (pods.project / "upload.md").read_text() == "uploaded by you after the last landing\n"
    assert [r.saga_state for r in await rows(api, first)] == ["completed", "compensated"]
    assert pods.real_names() == ["B.md", "Report.docx", "notes.txt", "start.md", "upload.md"]


async def test_a_turn_with_no_tool_settles_a_landing_another_thread_left_running(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pods = stored(api, first, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")
    async with api.app.state.session_factory() as db:  # long quiet: its worker is dead
        await db.execute(text("UPDATE workstream_history SET updated_at = now() - interval '10 minutes' WHERE thread_id = :t"), {"t": first.id})
        await db.commit()
    assert "a.md" in pods.real_names()
    # B answers its master and stops: no pod, nothing of its own to land.  The half-landed file does not wait for a turn that uses one.
    await ends(api, SandboxPool(pods), second)
    assert [r.saga_state for r in await rows(api, first)] == ["compensated"]
    assert pods.real_names() == ["Report.docx", "notes.txt"]


async def test_a_turn_with_no_tool_opens_no_pod_for_a_landing_that_is_still_at_work(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pods = stored(api, first, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")  # its row was written a moment ago
    provision, made = pods.provision, []

    async def counted(spec):
        made.append(spec)
        return await provision(spec)

    monkeypatch.setattr(pods, "provision", counted)
    # B answers its master and stops while that row is younger than the fence: its landing may be alive.
    await ends(api, SandboxPool(pods), second)
    # No pod, no wait for the project's lock, no row of its own: the landing is its own worker's, or a later holder's.
    assert made == [] and await rows(api, second) == []
    assert [r.saga_state for r in await rows(api, first)] == ["running"]


async def test_a_landing_that_changed_no_file_leaves_no_row(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "cat notes.txt > /dev/null")  # a turn that only read
    await ends(api, pool, thread)
    assert await rows(api, thread) == []
    [report] = await reports(api, master)
    assert "landing" not in report and not (pods.project / "_history").exists()
    # One that changed a file has its row, as before.
    await edited(pool, thread, "echo a > a.md")
    await ends(api, pool, thread)
    assert [r.saga_state for r in await rows(api, thread)] == ["completed"]


async def test_a_turn_retried_after_its_landing_was_killed_is_not_told_work_its_copy_has_is_gone(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    store, pool = api.app.state.session_store, SandboxPool(pods)
    await store.emit_event(thread.id, EventType.USER_MESSAGE, {"content": "Write two notes."})
    call, killed = landing_module._call, []

    async def dies(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "apply" and not killed:
            killed.append(True)
            raise asyncio.CancelledError  # the worker is killed after the turn's file was written
        return result

    async def never(*args, **kwargs):
        await asyncio.Event().wait()

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "_call", dies)
        patch.setattr(landing_module, "_settle", never)
        patch.setattr(landing_module, "_PUT_BACK_BOUND", 0.1)
        with pytest.raises(asyncio.CancelledError):
            await a_turn(api, monkeypatch, thread, [
                calling(("write_file", {"path": "a.md", "content": "a"})), _final_response("Done."),
            ], pool=pool, saga_settings=FENCED)
    landing_module._PUTTING_BACK.pop(str(thread.id)).cancel()
    async with asyncio.timeout(10):
        while pool.holds_copy(str(thread.id)):
            await asyncio.sleep(0.05)
    async with api.app.state.session_factory() as db:
        await db.execute(text("DELETE FROM session_leases WHERE session_id = :id"), {"id": thread.id})
        await db.commit()
    # The turn is retried on another worker: its copy is made from the branch its commit step pushed.
    await a_turn(api, monkeypatch, thread, [
        calling(("write_file", {"path": "b.md", "content": "b"})), _final_response("Done."),
    ], pool=SandboxPool(pods), saga_settings=FENCED)
    first = (await last_writes(store, thread))[-1]
    assert not first.startswith("[This thread's copy"), first
    assert pods.real_names() == ["Report.docx", "a.md", "b.md", "notes.txt"]


async def test_a_turns_end_reads_the_historys_refs_only_while_they_are_a_size_a_history_has(api, monkeypatch, tmp_path):
    thread = await a_thread(api)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo half > half.md")
    await ends(api, pool, thread, failed=True)  # its branch holds work
    storage, factory = api.app.state.storage, api.app.state.session_factory
    assert await rows_module.waits_to_land(factory, storage, thread) is True
    read, reads = storage.read, []

    async def counted(bucket, key):
        reads.append(key)
        return await read(bucket, key)

    monkeypatch.setattr(storage, "read", counted)
    # A thread's commands can write that file at any size: past the bound it is never read into the worker.
    monkeypatch.setattr(rows_module, "REFS_BOUND", 64)
    assert await rows_module.waits_to_land(factory, storage, thread) is True and reads == []


async def test_a_turns_report_does_not_wait_for_the_days_pruning(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    prune, pruning, release = History.prune, pods.root / "pruning", pods.root / "release"

    def slow(self, **kwargs):
        pruning.touch()
        while not release.exists():
            time.sleep(0.05)
        return prune(self, **kwargs)

    monkeypatch.setattr(History, "prune", slow)
    turn = asyncio.create_task(ends(api, pool, thread))
    async with asyncio.timeout(30):
        while not pruning.exists():
            await asyncio.sleep(0.05)
    # The pruning is at work, in the turn's pod, and the master already has the turn's report.
    [report] = await reports(api, master)
    assert [(f["ref"], f["landing"]) for f in report["files"]] == [("a.md", "landed")]
    assert len(await turn_ends(api, thread)) == 1 and not turn.done()
    release.touch()
    await asyncio.wait_for(turn, 30)
    assert (pods.project / "_history" / "pruned").exists() and pods.pods == {}


async def test_a_failed_turn_whose_keep_fails_reads_as_not_saved(api, monkeypatch, pods):
    thread = await a_thread(api)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo half > half.md")
    call = landing_module._call

    async def the_keep_fails(sandbox_pool, owner, action, **arguments):
        if action == "keep":
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    monkeypatch.setattr(landing_module, "_call", the_keep_fails)
    await ends(api, pool, thread, failed=True)
    # Its work went with its pod: its next copy lacks it, and the thread is then told so.
    [failed] = [e.data for e in await api.app.state.session_store.get_events(thread.id, types=[EventType.SESSION_FAIL])]
    assert failed["saved"] is False
    assert not (pods.project / "_history").exists()


async def test_a_turn_with_no_tool_opens_no_pod_in_a_project_that_went_over_the_cap(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo half > half.md")
    await ends(api, pool, thread, failed=True)  # kept on its branch: work that waits to land
    assert "threads" in (pods.project / "_history" / "packed-refs").read_text()
    # The project has since grown past the cap: it has no history, and its threads work on the real files.
    over = thread.model_copy(update={"config": {**thread.config, "history_off": True}})
    provision, made = pods.provision, []

    async def counted(spec):
        made.append(spec)
        return await provision(spec)

    monkeypatch.setattr(pods, "provision", counted)
    await ends(api, SandboxPool(pods), over)
    # Nothing lands without a history: no pod is made to land it.
    assert made == [] and "half.md" not in pods.real_names()


async def test_a_failed_turns_work_is_on_its_branch_at_the_next_turn_and_lands_then(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, "printf ' half made' >> Report.docx")
    await ends(api, pool, thread, failed=True)
    # The master hears that it failed, and that its work is not lost.
    [failed] = [e.data for e in await api.app.state.session_store.get_events(master.id, types=[EventType.WORKER_FAILED])]
    assert worker_note(EventType.WORKER_FAILED.value, failed)["content"].endswith(
        "failed: provider_error. Its work is kept, and lands with the thread's next turn]"
    )
    # Not landed, kept on its branch; and its pod is gone.
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{thread.id}:Report.docx") == "PK\x03\x04 report v1 half made"
    assert not pool.holds_copy(str(thread.id))
    # Its next turn uses no tool, and lands it all the same.
    await ends(api, SandboxPool(pods), thread)
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1 half made"
    [report] = await reports(api, master)
    assert [(f["ref"], f["landing"]) for f in report["files"]] == [("Report.docx", "landed")]


async def test_a_failed_turn_whose_pod_cannot_be_let_go_still_ends_as_failed_with_its_work_kept(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, "printf ' half made' >> Report.docx")

    async def the_pool_fails(owner, **_):
        raise RuntimeError("the pool's lock was cancelled")

    monkeypatch.setattr(pool, "release_for_session", the_pool_fails)
    await ends(api, pool, thread, failed=True)
    # The turn's end is written all the same: without it the thread looks alive to its master for ever.
    [failed] = [e.data for e in await api.app.state.session_store.get_events(thread.id, types=[EventType.SESSION_FAIL])]
    assert (failed["reason"], failed["saved"]) == ("provider_error", True)
    assert len(await api.app.state.session_store.get_events(master.id, types=[EventType.WORKER_FAILED])) == 1


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
    call, down = landing_module._call, []

    async def b_fails_and_the_database_goes(sandbox_pool, owner, action, **arguments):
        if action == "apply" and arguments["path"] == "b.md":
            down.append(True)
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    async def unwritten(write, *args, **kwargs):
        if down:
            raise ConnectionError("the database is down")
        await write(*args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", b_fails_and_the_database_goes)
    # Neither its steps nor its mark that the landing is alive.
    monkeypatch.setattr(landing_module, "save_landing", partial(unwritten, landing_module.save_landing))
    monkeypatch.setattr(landing_module, "touch_landing", partial(unwritten, landing_module.touch_landing))
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
    call, compensate, putting_back = landing_module._call, landing_module.compensate_step, []

    async def the_lock_goes_after_a(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "apply" and arguments["path"] == "a.md":
            await lose_the_lock(api, thread)  # nothing failed: the landing stops before b.md
        return result

    async def watched(it, **kwargs):
        putting_back.append(True)
        return await compensate(it, **kwargs)

    async def fails_once(write, *args, **kwargs):
        if putting_back == [True]:
            putting_back.append(False)
            raise ConnectionError("the database blinked")
        await write(*args, **kwargs)

    monkeypatch.setattr(landing_module, "_call", the_lock_goes_after_a)
    monkeypatch.setattr(landing_module, "compensate_step", watched)
    # Whichever write comes next: the row's steps, or its mark that the landing is alive.
    monkeypatch.setattr(landing_module, "save_landing", partial(fails_once, landing_module.save_landing))
    monkeypatch.setattr(landing_module, "touch_landing", partial(fails_once, landing_module.touch_landing))
    await ends(api, pool, thread)
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    [row] = await rows(api, thread)
    assert row.saga_state == "compensated"
    assert {s["state"] for s in row.steps if s["tool_name"] == "history.apply"} == {"compensated", "pending"}
    assert putting_back[:2] == [True, False]  # the write after a.md's put-back failed


@pytest.mark.parametrize("row_is", ROWS)
async def test_each_put_back_marks_its_row_alive_first_and_is_never_shown_done_before_it_is(api, monkeypatch, pods, row_is):
    rows_stand(monkeypatch, row_is)
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo b > b.md && echo c > c.md")
    call, compensate, seen, marked = landing_module._call, landing_module.compensate_step, [], []

    async def c_fails(sandbox_pool, owner, action, **arguments):
        if action == "apply" and arguments["path"] == "c.md":
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    async def watched(it, **kwargs):
        if it.tool_name == "history.apply":
            [row] = await rows(api, thread)  # from another connection, as the next lock holder reads it
            seen.append(next((s["arguments"]["path"], s["state"]) for s in row.steps if s["step_id"] == it.step_id))
            marked.append(row.updated_at)
        return await compensate(it, **kwargs)

    monkeypatch.setattr(landing_module, "_call", c_fails)
    monkeypatch.setattr(landing_module, "compensate_step", watched)
    await ends(api, pool, thread)
    # A put-back the row shows done is done: a worker killed in one leaves it to run again,
    # shown under way, or, in a row behind its landing, still as its apply left it.
    state = {"behind": "committed", "exact": "compensating"}[row_is]
    assert seen == [("b.md", state), ("a.md", state)]
    # And each marked the row alive first: the next lock holder's fence waits for a put-back still running.
    assert marked[0] < marked[1]


async def test_a_put_back_the_pod_cut_off_at_its_own_timeout_is_not_recorded_as_done(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md && echo b > b.md")
    call, unapply, execute = landing_module._call, History.unapply, pods.execute

    async def b_fails(sandbox_pool, owner, action, **arguments):
        if action == "apply" and arguments["path"] == "b.md":
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    def stalls(self, path, before, after, **kwargs):
        if path == "a.md":
            time.sleep(5)  # a stalled mount: past the pod's own call timeout
        return unapply(self, path, before, after, **kwargs)

    async def a_pod_with_a_short_call_timeout(sandbox_id, name, input, *, timeout=None):
        # A sandbox timeout below the saga's step timeout: the pod kills its child first.
        short = 1 if name == "_history" and json.loads(input).get("action") == "unapply" else timeout
        return await execute(sandbox_id, name, input, timeout=short)

    monkeypatch.setattr(landing_module, "_call", b_fails)
    monkeypatch.setattr(History, "unapply", stalls)
    monkeypatch.setattr(pods, "execute", a_pod_with_a_short_call_timeout)
    await ends(api, pool, thread, settings=SimpleNamespace(default_step_timeout=30, default_max_retries=0, retry_delay=0))
    # a.md was never put back, and neither its row nor its report says it was.
    assert "a.md" in pods.real_names()
    [row] = await rows(api, thread)
    [report] = await reports(api, master)
    assert (row.saga_state, report["landing"]) == ("escalated", "escalated")
    assert [s["state"] for s in row.steps if s["arguments"].get("path") == "a.md"] == ["compensation_failed"]


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


@pytest.mark.parametrize("row_is", ROWS)
async def test_a_put_back_that_failed_before_its_worker_died_is_tried_again_by_the_next_landing(api, monkeypatch, pods, row_is):
    rows_stand(monkeypatch, row_is)
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
    assert (row.saga_state, [s["state"] for s in row.steps]) == ("running", {
        "behind": ["committed", "committed", "committed", "failed"],
        "exact": ["committed", "compensating", "compensation_failed", "failed"],
    }[row_is])
    assert pods.real_names() == ["Report.docx", "b.md", "notes.txt"]

    await edited(pool, second, "echo by B > B.md")
    await ends(api, pool, second)
    # b.md's put-back is tried again, and the landing is undone whole.
    [row] = await rows(api, first)
    assert row.saga_state == "compensated"
    assert pods.real_names() == ["B.md", "Report.docx", "notes.txt"]


@pytest.mark.parametrize("row_is", ROWS)
async def test_a_recovery_that_lost_its_lock_stops_and_the_next_holder_finishes_it(api, monkeypatch, pods, row_is):
    rows_stand(monkeypatch, row_is)
    master = await master_of(api, await create(api))
    first, second, third = [await a_thread(api, name, master) for name in ("Draft A", "Draft B", "Draft C")]
    pool = SandboxPool(pods)
    await edited(pool, first, "for f in a b c d; do echo $f > $f.md; done")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply b.md")
    await edited(pool, second, "echo by B > B.md")
    compensate, put_back = landing_module.compensate_history, []

    async def the_lock_goes_after_the_first(it, sandbox_pool, owner, **kwargs):
        result = await compensate(it, sandbox_pool, owner, **kwargs)
        put_back.append(it.arguments["path"])
        if owner == str(second.id) and len(put_back) == 1:
            await lose_the_lock(api, second)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "compensate_history", the_lock_goes_after_the_first)
        await ends(api, pool, second)
    # B's recovery stops at its lock's loss: one file went back, the other waits for the next holder.
    # A row behind its landing shows both applies pending, and the first is put back first.
    back, waits = {"behind": ("a.md", "b.md"), "exact": ("b.md", "a.md")}[row_is]
    assert put_back == [back]
    assert pods.real_names() == ["Report.docx", waits, "notes.txt"]
    [row] = await rows(api, first)
    assert row.saga_state == "running"

    await edited(pool, third, "echo by C > C.md")
    await ends(api, pool, third)
    [row] = await rows(api, first)
    assert row.saga_state == "compensated"
    assert pods.real_names() == ["C.md", "Report.docx", "notes.txt"]



async def test_a_recovery_that_loses_its_lock_among_committed_put_backs_stops(api, monkeypatch, pods):
    rows_stand(monkeypatch, "exact")  # a row that shows the applies done
    master = await master_of(api, await create(api))
    first, second, third = [await a_thread(api, name, master) for name in ("Draft A", "Draft B", "Draft C")]
    pool = SandboxPool(pods)
    await edited(pool, first, "for f in a b c d; do echo $f > $f.md; done")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply d.md")  # a, b, c committed; d under way
    await edited(pool, second, "echo by B > B.md")
    history, step, put_back = landing_module.compensate_history, landing_module.compensate_step, []

    async def counted(owner, path):
        put_back.append(path)
        if owner == str(second.id) and len(put_back) == 2:
            await lose_the_lock(api, second)

    async def unsure(it, sandbox_pool, owner, **kwargs):
        result = await history(it, sandbox_pool, owner, **kwargs)
        await counted(owner, it.arguments["path"])
        return result

    async def committed(it, *, sandbox_pool, session_id):
        result = await step(it, sandbox_pool=sandbox_pool, session_id=session_id)
        if it.tool_name == "history.apply":
            await counted(session_id, it.arguments["path"])
        return result

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "compensate_history", unsure)
        patch.setattr(landing_module, "compensate_step", committed)
        await ends(api, pool, second)
    # The unsure d.md, then the committed c.md, then the lock goes: no put-back runs without it.
    assert put_back == ["d.md", "c.md"]
    [row] = await rows(api, first)
    assert row.saga_state == "running"  # not escalated: the next holder finishes it

    await edited(pool, third, "echo by C > C.md")
    await ends(api, pool, third)
    [row] = await rows(api, first)
    assert row.saga_state == "compensated"
    assert pods.real_names() == ["C.md", "Report.docx", "notes.txt"]

async def turn_ends(api, thread) -> list[dict]:
    """What each of *thread*'s turn ends wrote: its ``session.complete`` events."""
    return [e.data for e in await api.app.state.session_store.get_events(thread.id, types=[EventType.SESSION_COMPLETE])]


async def test_a_landing_that_could_not_settle_another_threads_is_kept_and_lands_with_its_next_turn(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pods = stored(api, first, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")
    await edited(pool, second, "echo by B > B.md")
    call, looks, sleep, waits = landing_module._call, [], asyncio.sleep, []

    async def the_pod_never_answers_the_settle(sandbox_pool, owner, action, **arguments):
        if action == "fetch" and arguments.get("commits"):
            looks.append(len(waits))
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    async def counted(seconds, *args):
        waits.append(seconds)
        return await sleep(seconds, *args)

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "_call", the_pod_never_answers_the_settle)
        patch.setattr(asyncio, "sleep", counted)
        await ends(api, pool, second)
    # B's turn did not land, and is not lost with its pod: it is on B's branch, as a failed turn's is,
    # kept without waiting on A's landing a second time: the settle that failed had waited out its fence,
    # and the mark its looks left on A's row is no sign of life.  The look is tried twice, as one that
    # did not see the base is.
    assert len(looks) == 2 and "B.md" not in pods.real_names()
    assert [wait for wait in waits[looks[-1]:] if wait > 0.5] == []
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{second.id}:B.md") == "by B"
    assert [done["saved"] for done in await turn_ends(api, second)] == [True]
    [report] = await reports(api, master)
    # The master reads that it did not land, that the project's files are as they were, and that the work is kept.
    assert (report["landing"], report["saved"]) == ("compensated", True)
    said = worker_note(EventType.WORKER_COMPLETE.value, report)["content"]
    assert "\nNot landed, and the project's files are as they were: " in said
    assert said.endswith("\nThe thread's work is kept, and lands with its next turn")
    assert await rows(api, second) == [] and [r.saga_state for r in await rows(api, first)] == ["running"]
    # B's next turn, with no tool, settles A's landing and lands its own.
    await ends(api, SandboxPool(pods), second)
    assert [r.saga_state for r in await rows(api, first)] == ["compensated"]
    assert pods.real_names() == ["B.md", "Report.docx", "notes.txt"]


async def test_a_failed_turn_is_kept_though_another_threads_landing_cannot_be_settled(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")
    await edited(pool, second, "echo half > B.md")
    call = landing_module._call

    async def the_pod_never_answers_the_settle(sandbox_pool, owner, action, **arguments):
        if action == "fetch" and arguments.get("commits"):
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    monkeypatch.setattr(landing_module, "_call", the_pod_never_answers_the_settle)
    await ends(api, pool, second, failed=True)
    # A keep moves only its thread's own refs: it waits on no other thread's landing.
    [failed] = [e.data for e in await api.app.state.session_store.get_events(second.id, types=[EventType.SESSION_FAIL])]
    assert failed["saved"] is True
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{second.id}:B.md") == "half"


async def test_a_look_of_the_settle_the_pod_did_not_answer_is_tried_again_as_a_step_is(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")
    await edited(pool, second, "echo by B > B.md")
    call, touch, did = landing_module._call, landing_module.touch_landing, []
    [dead] = await rows(api, first)

    async def the_pod_times_out_once(sandbox_pool, owner, action, **arguments):
        if action == "fetch" and arguments.get("commits"):
            did.append("look")
            if did.count("look") == 1:
                raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    async def marked(session_factory, row):
        if row == dead.id and "put back" not in did:
            did.append("mark")
        await touch(session_factory, row)

    async def put_back(*args, **kwargs):
        did.append("put back")
        return await compensate(*args, **kwargs)

    compensate = landing_module.compensate_history
    monkeypatch.setattr(landing_module, "_call", the_pod_times_out_once)
    monkeypatch.setattr(landing_module, "touch_landing", marked)
    monkeypatch.setattr(landing_module, "compensate_history", put_back)
    await ends(api, pool, second, settings=SimpleNamespace(default_step_timeout=1, default_max_retries=1, retry_delay=0))
    # One timeout of another thread's settle fails no landing: the look is made again, the dead
    # landing's row marked alive before each try, as a step's is, for the next lock holder's fence.
    assert did[:4] == ["mark", "look", "mark", "look"]
    assert [r.saga_state for r in await rows(api, first)] == ["compensated"]
    assert [r.saga_state for r in await rows(api, second)] == ["completed"]
    assert pods.real_names() == ["B.md", "Report.docx", "notes.txt"]


async def test_a_landing_settled_whose_row_cannot_be_written_is_left_to_the_next_holder_and_not_settled_again(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second, third = [await a_thread(api, name, master) for name in ("Draft A", "Draft B", "Draft C")]
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")
    [dead] = await rows(api, first)
    save = landing_module.save_landing

    async def its_outcome_cannot_be_written(session_factory, row, saga, **values):
        if row == dead.id and values.get("state", "running") != "running":
            raise ConnectionError("the database blinked")
        await save(session_factory, row, saga, **values)

    await edited(pool, second, "echo by B > B.md")
    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "save_landing", its_outcome_cannot_be_written)
        await asyncio.wait_for(ends(api, pool, second), 30)  # it does not settle the row over and over
    # Its files went back and B landed; the row, still running, is the next holder's to settle.
    assert pods.real_names() == ["B.md", "Report.docx", "notes.txt"]
    assert [r.saga_state for r in await rows(api, first)] == ["running"]
    await edited(pool, third, "echo by C > C.md")
    await ends(api, pool, third)
    assert [r.saga_state for r in await rows(api, first)] == ["compensated"]


#: Why a landing left running is given up: its row's applies say so.
GONE = "The project's history no longer has the versions from before this landing: its files cannot be put back"


def looks_for_commits(monkeypatch, *, unseen: int = 0) -> list[list[str]]:
    """The commits each look of a settle asks the pod for; the first *unseen* looks do not see them, as a listing behind would not."""
    call, looks = landing_module._call, []

    async def watched(sandbox_pool, owner, action, **arguments):
        answer = await call(sandbox_pool, owner, action, **arguments)
        if action == "fetch" and arguments.get("commits"):
            looks.append(list(arguments["commits"]))
            if len(looks) <= unseen:
                return {**answer, "missing": list(arguments["commits"])}
        return answer

    monkeypatch.setattr(landing_module, "_call", watched)
    return looks


async def test_a_landing_left_running_whose_commits_the_history_lost_is_given_up_and_others_land(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    first, second, third = [await a_thread(api, name, master) for name in ("Draft A", "Draft B", "Draft C")]
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")
    [row] = await rows(api, first)
    base = row.steps[0]["result"]["base"]
    # The history is deleted, as the master's own tools can delete it: nobody has the versions from before.
    shutil.rmtree(pods.project / "_history")
    looks = looks_for_commits(monkeypatch)
    for thread, name in ((second, "B"), (third, "C")):
        await edited(pool, thread, f"echo by {name} > {name}.md")
        await ends(api, pool, thread)
    # Given up once, with why, rather than failing every later landing of the project:
    # after a second look, and for the base alone, the only versions a put-back writes.
    [row] = await rows(api, first)
    assert row.saga_state == "escalated" and looks == [[base], [base]]
    assert {(s["state"], s["error"]) for s in row.steps if s["tool_name"] == "history.apply"} == {("compensation_failed", GONE)}
    assert [(await rows(api, thread))[-1].saga_state for thread in (second, third)] == ["completed", "completed"]
    # The master is told, in a report of the dead landing's thread: its files, and that they could not be put back.
    told = [report for report in await reports(api, master) if "landing" in report]
    assert [(r["worker_id"], r["title"], r["landing"], r["recovered"], r["gone"]) for r in told] == [
        (str(first.id), "Draft A", "escalated", True, True),
    ]
    assert told[0]["files"] == [
        {"kind": "file", "label": name, "ref": name, "landing": "not_merged"} for name in ("a.md", "b.md")
    ]
    assert worker_note(EventType.WORKER_COMPLETE.value, told[0])["content"] == (
        f'[Thread "Draft A" ({first.id}): a landing its worker left unfinished was settled]\n'
        "Could not finish landing these; check them: a.md, b.md\n"
        "The project's history no longer has their versions from before the landing, so none could be put back"
    )
    # What it half landed stays, for a person to check; nothing is written over.
    assert pods.real_names() == ["B.md", "C.md", "Report.docx", "a.md", "notes.txt"]


async def test_a_landing_left_running_is_not_given_up_while_the_history_is_only_slow_to_show_its_commits(api, monkeypatch, pods):
    paused = SimpleNamespace(default_step_timeout=1, default_max_retries=0, retry_delay=0.25)
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool = SandboxPool(pods)
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")
    [row] = await rows(api, first)
    base = row.steps[0]["result"]["base"]
    await edited(pool, second, "echo by B > B.md")
    looks, sleep, waits = looks_for_commits(monkeypatch, unseen=1), asyncio.sleep, []

    async def counted(seconds, *args):
        waits.append((len(looks), seconds))
        return await sleep(seconds, *args)

    monkeypatch.setattr(asyncio, "sleep", counted)
    await ends(api, pool, second, settings=paused)
    # One look that did not see the base is no word to give a landing up on: it is looked for again,
    # a step's pause later, and the landing is put back as any dead landing is.
    assert looks == [[base], [base]] and (1, 0.25) in waits
    [row] = await rows(api, first)
    assert row.saga_state == "compensated" and pods.real_names() == ["B.md", "Report.docx", "notes.txt"]
    assert all("landing" not in report for report in await reports(api, master))


async def test_a_landing_whose_commit_step_failed_keeps_the_turn_on_its_branch(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    call = landing_module._call

    async def the_commit_fails(sandbox_pool, owner, action, **arguments):
        if action == "commit":
            raise landing_module.LandingStepError("git add failed: Input/output error")
        return await call(sandbox_pool, owner, action, **arguments)

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "_call", the_commit_fails)
        await ends(api, pool, thread)
    # Its commit step never put the turn in the history; the turn's end did, before its pod went.
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{thread.id}:a.md") == "a"
    assert [done["saved"] for done in await turn_ends(api, thread)] == [True]
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    await ends(api, SandboxPool(pods), thread)  # its next turn, with no tool
    assert pods.real_names() == ["Report.docx", "a.md", "notes.txt"]


async def test_a_project_over_the_cap_has_no_history_and_its_threads_work_on_the_real_files(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    harness = harness_of(api)
    harness._storage = api.app.state.storage
    tenant = SimpleNamespace(org_id=thread.org_id, user_id=thread.user_id)
    # Excluded files are not counted: two tracked files are within a cap of two.
    (pods.project / "node_modules").mkdir()
    (pods.project / "node_modules" / "x.js").write_text("x")
    monkeypatch.setattr(rows_module, "HISTORY_CAP", 2)
    monkeypatch.setattr(rows_module, "_COUNTED", {})
    within = await harness._with_history_cap(thread)
    assert within.config["history_off"] is False
    assert "PROJECT_DIR" in (await _build_session_sandbox_spec(within, tenant, str(thread.id))).env
    (pods.project / "c.md").write_text("one too many")
    # A wake within the minute takes the last count; one after it counts again.
    clock = time.monotonic()
    monkeypatch.setattr(rows_module.time, "monotonic", lambda: clock + rows_module.COUNT_TTL - 1)
    assert (await harness._with_history_cap(thread)).config["history_off"] is False
    monkeypatch.setattr(rows_module.time, "monotonic", lambda: clock + rows_module.COUNT_TTL + 1)
    over = await harness._with_history_cap(thread)
    assert over.config["history_off"] is True
    # The plain layout: the real files at /workspace, no copy, nothing to land.
    spec = await _build_session_sandbox_spec(over, tenant, str(thread.id))
    assert "PROJECT_DIR" not in spec.env and [r.mount_path for r in spec.resources] == ["/workspace"]
    pool = SandboxPool(pods)
    await pool.ensure(str(thread.id), spec)
    assert not pool.holds_copy(str(thread.id))


async def test_a_wake_whose_count_of_the_files_fails_takes_the_last_count_and_with_none_keeps_history_on(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    harness = harness_of(api)
    harness._storage = storage = api.app.state.storage
    monkeypatch.setattr(rows_module, "HISTORY_CAP", 1)
    monkeypatch.setattr(rows_module, "_COUNTED", {})
    listing, down = storage.list_keys, []

    async def the_bucket_does_not_answer(bucket, prefix):
        if down:
            raise ConnectionError("the bucket did not answer")
        return await listing(bucket, prefix)

    monkeypatch.setattr(storage, "list_keys", the_bucket_does_not_answer)
    down.append(True)
    # Never counted: the wake goes on, with history, rather than failing on a listing.
    assert (await harness._with_history_cap(thread)).config["history_off"] is False
    down.clear()
    clock = time.monotonic()
    monkeypatch.setattr(rows_module.time, "monotonic", lambda: clock + 2 * rows_module.COUNT_TTL)
    assert (await harness._with_history_cap(thread)).config["history_off"] is True  # two files, over a cap of one
    # Counted over the cap, and the next count fails, a minute on: the last answer stands.
    down.append(True)
    monkeypatch.setattr(rows_module.time, "monotonic", lambda: clock + 4 * rows_module.COUNT_TTL)
    assert (await harness._with_history_cap(thread)).config["history_off"] is True
    assert pods.real_names() == ["Report.docx", "notes.txt"]


async def test_a_landing_prunes_the_history_at_most_once_a_day_keeping_live_threads(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    live, resolved, gone = [await a_thread(api, title, master) for title in ("Live", "Resolved", "Gone")]
    pool = SandboxPool(pods)
    for thread in (live, resolved, gone):
        await edited(pool, thread, f"echo {thread.title} > '{thread.title}.md'")
        await ends(api, pool, thread, failed=True)  # kept, unlanded, on its branch
    async with api.app.state.session_factory() as db:
        await db.execute(text(
            "UPDATE workstream_threads SET resolved_at = now() - interval '10 days' WHERE session_id = :r"
        ), {"r": resolved.id})
        await db.execute(text(
            "UPDATE workstream_threads SET resolved_at = now() - interval '100 days' WHERE session_id = :g"
        ), {"g": gone.id})
        await db.commit()
    bounds, spared = [], []
    execute = pool.execute_released

    async def watched(sandbox_id, name, input, **kwargs):
        if name == "_history" and json.loads(input)["action"] == "prune":
            bounds.append(kwargs.get("timeout"))
            spared.append(json.loads(input)["spare"])
        return await execute(sandbox_id, name, input, **kwargs)

    monkeypatch.setattr(pool, "execute_released", watched)
    durable = pods.project / "_history"
    old = {pack.name for pack in (durable / "objects" / "pack").iterdir()}
    for name in old:  # the kept turns' packs, written long before the fence
        os.utime(durable / "objects" / "pack" / name, (time.time() - 3600, time.time() - 3600))
    lander = await a_thread(api, "Lander", master)
    await edited(pool, lander, "echo landed > landed.md")
    await ends(api, pool, lander)
    # Its own bound, from the size of the history; and the packs it is to leave, those younger than the fence.
    assert len(bounds) == 1 and bounds[0] >= landing_module._PRUNE_BOUND
    assert spared == [landing_module._fence(FENCED)]
    refs = git(durable, "for-each-ref", "--format=%(refname)").splitlines()
    assert f"refs/heads/threads/{live.id}" in refs and f"refs/heads/threads/{resolved.id}" in refs
    assert f"refs/heads/threads/{gone.id}" not in refs
    # The old packs went into one; the landing's own two, the turn's and main's, wait for the next pruning
    # while they are younger than the fence.
    left = {pack.name for pack in (durable / "objects" / "pack").iterdir()}
    assert old and not old & left and 1 <= len([name for name in left if name.endswith(".pack")]) <= 3
    pruned = (durable / "pruned").stat().st_mtime
    await edited(pool, lander, "echo again > again.md")
    await ends(api, pool, lander)
    assert (durable / "pruned").stat().st_mtime == pruned  # not again the same day


async def test_the_days_pruning_waits_for_another_landing_while_one_is_still_running(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    lander, other = await a_thread(api, "Lander", master), await a_thread(api, "Draft B", master)
    pool, factory = SandboxPool(pods), api.app.state.session_factory
    await edited(pool, lander, "echo landed > landed.md")
    prune, asked = landing_module.prune_after, []
    execute = pool.execute_released

    async def another_landing_has_begun(**kwargs):
        # Between this landing's lock and its pruning's, another thread's landing began: it may be writing a pack now.
        saga = SagaOrchestrator().create_saga(other.id, kind="landing")
        begun.append(await rows_module.start_landing(
            factory, saga, workstream_id=other.config["workstream_id"], thread_id=other.id,
            agent_id=str(other.agent_id), user_id=other.user_id, tool_saga_id=None, events=None,
        ))
        return await prune(**kwargs)

    async def watched(sandbox_id, name, input, **kwargs):
        asked.append(json.loads(input).get("action"))
        return await execute(sandbox_id, name, input, **kwargs)

    begun: list[int] = []
    monkeypatch.setattr(landing_module, "prune_after", another_landing_has_begun)
    monkeypatch.setattr(loop_artifact_completion, "prune_after", another_landing_has_begun)
    monkeypatch.setattr(pool, "execute_released", watched)
    await ends(api, pool, lander)
    # Fenced, as a landing is: the pod is not asked, and the day is not marked pruned.
    assert begun and asked == [] and not (pods.project / "_history" / "pruned").exists()
    # That landing done, the next completed landing prunes.
    async with factory() as db:
        await db.execute(text("DELETE FROM workstream_history WHERE id = :id"), {"id": begun[0]})
        await db.commit()
    monkeypatch.setattr(landing_module, "prune_after", prune)
    monkeypatch.setattr(loop_artifact_completion, "prune_after", prune)
    await edited(pool, lander, "echo again > again.md")
    await ends(api, pool, lander)
    assert asked == ["prune"] and (pods.project / "_history" / "pruned").exists()


@pytest.mark.parametrize("failed", [False, True], ids=["a turn whose landing could not start", "a failed turn"])
async def test_a_keep_whose_settle_failed_waits_out_the_fence_before_it_writes(api, monkeypatch, pods, failed):
    slow = SimpleNamespace(default_step_timeout=3, default_max_retries=0, retry_delay=0)  # a fence of four seconds
    master = await master_of(api, await create(api))
    first, second = await a_thread(api, "Draft A", master), await a_thread(api, "Draft B", master)
    pool, factory = SandboxPool(pods), api.app.state.session_factory
    await edited(pool, first, "echo a > a.md && echo b > b.md")
    await a_landing_killed(api, monkeypatch, pool, first, after="apply a.md")  # its row was marked a moment ago: it may be alive
    await edited(pool, second, "echo by B > B.md")
    running, call, unread, quiet_at_the_keep = rows_module.running_landings, landing_module._call, [], []

    async def the_rows_cannot_be_read_once(session_factory, workstream_id):
        if not unread:
            unread.append(True)
            raise ConnectionError("the database did not answer")
        return await running(session_factory, workstream_id)

    async def watched(sandbox_pool, owner, action, **arguments):
        if action == "keep":
            quiet_at_the_keep.extend(quiet for _, quiet in await running(factory, first.config["workstream_id"]))
        return await call(sandbox_pool, owner, action, **arguments)

    monkeypatch.setattr(landing_module, "running_landings", the_rows_cannot_be_read_once)
    monkeypatch.setattr(landing_module, "_call", watched)
    await ends(api, pool, second, failed=failed, settings=slow)
    # B's turn is kept, and its push went out only once A's landing had been quiet for the fence:
    # a landing still alive would have marked its row within it.
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{second.id}:B.md") == "by B"
    assert len(quiet_at_the_keep) == 1 and quiet_at_the_keep[0] >= landing_module._fence(slow)


async def a_helper(api, thread, *, channel="delegation"):
    return await create_child_session(store=api.app.state.session_store, parent=thread, channel=channel)


async def handed_off(api, pool, thread) -> None:
    """*thread*'s copy put on its hand-off, as a step that starts a helper does first."""
    await landing_module.keep_copy(
        session_factory=api.app.state.session_factory, sandbox_pool=pool, session=thread,
        saga_settings=FENCED, action="hand_off",
    )


async def test_a_helper_on_another_worker_works_on_its_own_copy_and_its_work_lands_with_the_thread(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    mine, theirs = SandboxPool(pods), SandboxPool(pods)  # the thread's worker, and the helper's
    await edited(mine, thread, "echo outline > outline.md")
    await handed_off(api, mine, thread)
    helper = await a_helper(api, thread)
    assert (helper.config["history_thread"], helper.config["sandbox_root_session_id"]) == (str(thread.id), str(helper.id))
    await a_turn(api, monkeypatch, helper, [
        calling(("terminal", {"command": "cat outline.md > sources.md && echo by the helper >> sources.md"})),
        _final_response("Wrote the sources."),
    ], pool=theirs)
    # Its own pod, gone with its turn; its work on the thread's hand-off, not in the real files.
    assert not theirs.holds_copy(str(helper.id)) and not (pods.project / "sources.md").exists()
    assert await landing_module.take_up(mine, str(thread.id)) == []
    assert (pods.copies[str(thread.id)] / "sources.md").read_text() == "outline\nby the helper\n"
    await ends(api, mine, thread)
    assert (pods.project / "sources.md").read_text() == "outline\nby the helper\n"
    [report] = await reports(api, master)
    assert {(f["ref"], f["landing"]) for f in report["files"]} == {("outline.md", "landed"), ("sources.md", "landed")}


async def test_a_second_helper_is_told_which_of_its_files_the_first_changed_first(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    first, second = await a_helper(api, thread), await a_helper(api, thread)
    for helper in (first, second):  # both at work at once, on copies made before either kept
        await open_pod(pool, helper)
    for helper, by in ((first, "first"), (second, "second")):
        await a_turn(api, monkeypatch, helper, [
            calling(("terminal", {"command": f"echo by the {by} > notes.txt && echo {by} > {by}.md"})),
            _final_response("Done."),
        ], pool=pool)
    [done] = [e.data for e in await api.app.state.session_store.get_events(second.id, types=[EventType.SESSION_COMPLETE])]
    assert done["not_kept"] == ["notes.txt"]
    await ends(api, pool, thread)  # a turn that used no tool: what its helpers kept lands all the same
    assert (pods.project / "notes.txt").read_text() == "by the first\n"
    assert pods.real_names() == ["Report.docx", "first.md", "notes.txt", "second.md"]


async def test_a_landings_report_names_a_helpers_files_the_threads_copy_did_not_take_up(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    helper = await a_helper(api, thread)
    await a_turn(api, monkeypatch, helper, [
        calling(("terminal", {"command": "echo by the helper >> notes.txt && echo h > h.md"})), _final_response("Done."),
    ], pool=SandboxPool(pods))
    (pods.project / "notes.txt").write_text("v2 notes, saved by you\n")  # after the helper started
    await ends(api, SandboxPool(pods), thread)  # the thread's next turn end takes the helper's work up, and lands it
    # Your version stays, the helper's is left out, and the master is told, not left to find the edit gone.
    assert (pods.project / "notes.txt").read_text() == "v2 notes, saved by you\n"
    [report] = await reports(api, master)
    assert [(f["ref"], f["landing"]) for f in report["files"]] == [("h.md", "landed")]
    assert report["not_taken"] == ["notes.txt"]
    assert worker_note(EventType.WORKER_COMPLETE.value, report)["content"].endswith(
        "Not taken up from a helper, because the file changed after the helper started "
        "(the helper's version is kept in the project's history): notes.txt"
    )
    # The hand-off went with the landing: a helper started now starts from what landed.
    refs = git(pods.project / "_history", "for-each-ref", "--format=%(refname)").splitlines()
    assert not [ref for ref in refs if "handoff" in ref]


async def test_a_routine_run_of_a_thread_lands_with_the_threads_next_turn(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    run = await a_helper(api, thread, channel="scheduled")
    await a_turn(api, monkeypatch, run, [
        calling(("terminal", {"command": "echo checked >> Report.docx"})),
        _final_response("Checked the report."),
    ], pool=SandboxPool(pods))
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"
    await ends(api, SandboxPool(pods), thread)
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1checked\n"


async def test_a_failed_helpers_files_never_land_and_its_thread_is_told(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    helper = await a_helper(api, thread)
    await edited(pool, helper, "printf ' half made' >> Report.docx && echo half > outline.md")
    await ends(api, pool, helper, failed=True)
    store = api.app.state.session_store
    [failed] = [e.data for e in await store.get_events(helper.id, types=[EventType.SESSION_FAIL])]
    assert failed["left"] == ["Report.docx", "outline.md"]
    # The thread waiting on it hears which files were kept apart.
    outcome = await _poll_child_completion(session_store=store, parent_session_id=thread.id, child_id=helper.id, timeout=5)
    assert "Report.docx, outline.md" in outcome["reason"]
    # Kept in the history, apart; never landed with the thread.
    kept = git(pods.project / "_history", "ls-tree", "--name-only", f"refs/helpers/{thread.id}/{helper.id}")
    assert kept.splitlines() == ["Report.docx", "notes.txt", "outline.md"]
    await ends(api, SandboxPool(pods), thread)
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    assert (pods.project / "Report.docx").read_bytes() == b"PK\x03\x04 report v1"


@pytest.mark.parametrize("apart", ["kept apart", "not kept apart either"])
async def test_a_helper_whose_hand_back_fails_says_so_and_its_thread_is_told_which_files(api, monkeypatch, tmp_path, apart):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    helper = await a_helper(api, thread)
    call = landing_module._call

    async def the_pod_times_out(sandbox_pool, owner, action, **arguments):
        if action == "hand_back" or (action == "keep_apart" and apart != "kept apart"):
            raise landing_module.LandingStepError("the pod's step timed out")
        return await call(sandbox_pool, owner, action, **arguments)

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "_call", the_pod_times_out)
        await a_turn(api, monkeypatch, helper, [
            calling(("terminal", {"command": "echo sources > sources.md"})), _final_response("Wrote sources.md."),
        ], pool=SandboxPool(pods))
    store = api.app.state.session_store
    # Its completion is marked, and its thread's report of it says the work is not in the thread's copy.
    [done] = await turn_ends(api, helper)
    [told] = [e.data for e in await store.get_events(thread.id, types=[EventType.WORKER_COMPLETE])]
    note = worker_note(EventType.WORKER_COMPLETE.value, told)["content"]
    if apart == "kept apart":
        assert (done["kept"], done["left"]) == (False, ["sources.md"])
        assert "its changes to sources.md were kept apart, not brought into the thread's copy" in note.splitlines()[0]
        assert git(pods.project / "_history", "show", f"refs/helpers/{thread.id}/{helper.id}:sources.md") == "sources"
    else:
        assert done["kept"] is False and "left" not in done
        assert "could not be handed back, and its changes are not in the thread's copy" in note.splitlines()[0]
    # A thread that waited on it in a step reads the same in the step's result.
    assert not_handed_back(done, copy="this copy").endswith("this copy")
    await ends(api, SandboxPool(pods), thread)
    assert pods.real_names() == ["Report.docx", "notes.txt"]


async def test_a_helper_whose_work_was_handed_back_says_nothing_of_it():
    assert not_handed_back({"reason": "completed"}) == "" and not_handed_back({"not_kept": ["a.md"]}) == ""
    done = {"worker_id": "w1", "result": "Done."}
    assert worker_note(EventType.WORKER_COMPLETE.value, done)["content"] == "[Worker w1 completed]\nDone."


async def test_a_pruning_keeps_a_live_threads_hand_off_and_its_helpers_copies_kept_apart(api):
    thread = await a_thread(api, "Draft A", await master_of(api, await create(api)))
    kept = await rows_module.kept_refs(api.app.state.session_factory, thread.config["workstream_id"])
    assert {f"refs/handoff/{thread.id}", f"refs/handoff-from/{thread.id}", f"refs/helpers/{thread.id}/"} <= set(kept)


async def test_a_hand_off_outside_its_projects_lock_is_refused(api, monkeypatch, pods):
    thread = await a_thread(api)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo outline > outline.md")
    settle = landing_module.settle_running

    async def the_lock_goes(*args, **kwargs):
        settled = await settle(*args, **kwargs)
        await lose_the_lock(api, thread)  # a failover while the hand-off waited its turn
        return settled

    monkeypatch.setattr(landing_module, "settle_running", the_lock_goes)
    # Its connection's end, as the driver says it: gone already, or going while it is asked.
    with pytest.raises((DBAPIError, InternalClientError)):
        await handed_off(api, pool, thread)
    # Its check of the hand-off holds only under the lock: outside it, nothing is handed off.
    assert not (pods.project / "_history" / "packed-refs").exists()


async def test_a_failed_helper_lets_its_own_pod_go_and_never_its_threads(api, monkeypatch, pods):
    thread = await a_thread(api)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo draft > draft.md")  # the thread's turn, at the step that started the helper
    helper = await a_helper(api, thread)
    await edited(pool, helper, "echo half > outline.md")
    await ends(api, pool, helper, failed=True)
    # Its own pod and copy went; its thread's, on the same worker, stay as they were.
    assert (pool.holds_copy(str(helper.id)), pool.holds_copy(str(thread.id))) == (False, True)
    assert (pods.copies[str(thread.id)] / "draft.md").read_text() == "draft\n"
    assert not (pods.copies[str(thread.id)] / "outline.md").exists()
