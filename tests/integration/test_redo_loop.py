"""Your edits picked up at each landing, and the redo of a thread's change that a newer file kept out."""

from __future__ import annotations

import pytest

from surogates.harness import landing as landing_module
from surogates.harness import loop_artifact_completion
from surogates.sandbox.pool import SandboxPool

from .test_devices import api  # noqa: F401  (api is a fixture)
from .test_durable_landings import (  # noqa: F401  (a_short_fence is a fixture)
    a_short_fence,
    edited,
    ends,
    rows,
    stored,
    turn_ends,
)
from .test_thread_copies import a_thread, git, pods  # noqa: F401  (pods is a fixture)
from .test_workstreams import create, master_of

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_your_upload_during_a_turn_is_its_landings_pickup_by_you(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    (pods.project / "brief.pdf").write_bytes(b"%PDF uploaded while the thread worked")
    await ends(api, pool, thread)
    durable = pods.project / "_history"
    [row] = await rows(api, thread)
    pickup = git(durable, "rev-parse", f"{row.commit}^1")
    assert git(durable, "log", "-1", "--format=%ae", pickup) == f"user:{thread.user_id}@surogate"
    assert git(durable, "log", "-1", "--format=%(trailers:key=Surogate-Kind,valueonly)", pickup) == "pickup"
    assert git(durable, "log", "-1", "--format=%(trailers:key=Surogate-Saga,valueonly)", pickup) == row.saga_id
    assert [(f["path"], f["before"]) for f in row.picked_up] == [("brief.pdf", None)]
    assert row.steps[0]["tool_name"] == "history.pickup"


async def test_a_turn_that_changed_nothing_records_none_of_your_edits(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    await ends(api, pool, thread)
    await edited(pool, thread, "cat notes.txt")
    (pods.project / "notes.txt").write_text("v2 notes, saved by you\n")
    await ends(api, pool, thread)
    # It lands nothing: it leaves no row, main does not move, and your edit is in no history yet.
    [landed] = await rows(api, thread)
    durable = pods.project / "_history"
    assert git(durable, "rev-parse", "refs/heads/main") == landed.commit
    assert git(durable, "show", "refs/heads/main:notes.txt") == "v1 notes"


async def test_a_landing_whose_push_answer_was_lost_lists_your_edits_all_the_same(api, monkeypatch, pods):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    (pods.project / "brief.pdf").write_bytes(b"%PDF uploaded while the thread worked")
    call, land_turn = landing_module._call, landing_module.land_turn
    outcomes: list[dict] = []

    async def the_push_answer_is_lost(sandbox_pool, owner, action, **arguments):
        result = await call(sandbox_pool, owner, action, **arguments)
        if action == "record":
            raise landing_module.LandingStepError("the pod's step timed out")
        return result

    async def landed(**arguments):
        outcomes.append(await land_turn(**arguments))
        return outcomes[-1]

    monkeypatch.setattr(landing_module, "_call", the_push_answer_is_lost)
    monkeypatch.setattr(loop_artifact_completion, "land_turn", landed)
    await ends(api, pool, thread)
    # The push happened: the landing counts, and the row it is settled into names your upload as one that ended well does.
    [row] = await rows(api, thread)
    assert (row.saga_state, row.commit) == ("completed", git(pods.project / "_history", "rev-parse", "refs/heads/main"))
    assert [(f["path"], f["before"]) for f in row.picked_up] == [("brief.pdf", None)]
    [outcome] = outcomes
    assert (outcome["commit"], outcome["picked_up"]) == (row.commit, row.picked_up)


async def test_a_landing_whose_pickup_failed_keeps_the_turn_on_its_branch(api, monkeypatch, tmp_path):
    master = await master_of(api, await create(api))
    thread = await a_thread(api, "Draft A", master)
    pods = stored(api, thread, tmp_path)
    pool = SandboxPool(pods)
    await edited(pool, thread, "echo a > a.md")
    call = landing_module._call

    async def the_pickup_fails(sandbox_pool, owner, action, **arguments):
        if action == "pickup":
            raise landing_module.LandingStepError("git add failed: Input/output error")
        return await call(sandbox_pool, owner, action, **arguments)

    with monkeypatch.context() as patch:
        patch.setattr(landing_module, "_call", the_pickup_fails)
        await ends(api, pool, thread)
    # It never reached its commit step: the turn's end put the turn in the history, before its pod went.
    assert git(pods.project / "_history", "show", f"refs/heads/threads/{thread.id}:a.md") == "a"
    assert [done["saved"] for done in await turn_ends(api, thread)] == [True]
    assert pods.real_names() == ["Report.docx", "notes.txt"]
    [row] = await rows(api, thread)
    assert (row.saga_state, [(s["tool_name"], s["state"]) for s in row.steps]) == ("compensated", [("history.pickup", "failed")])
    await ends(api, SandboxPool(pods), thread)  # its next turn, with no tool
    assert pods.real_names() == ["Report.docx", "a.md", "notes.txt"]
