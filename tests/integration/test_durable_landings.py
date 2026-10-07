"""A landing's durable record, and a landing a killed worker left running."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from surogates.db.models import WorkstreamHistory
from surogates.harness import landing as landing_module
from surogates.sandbox.pool import SandboxPool
from surogates.session.events import EventType
from surogates.workstreams import history as rows_module
from tests.test_steer_loop import _final_response

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_thread_copies import a_thread, git, open_pod, pods, reports  # noqa: F401  (pods is a fixture)
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
