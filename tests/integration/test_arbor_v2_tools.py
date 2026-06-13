"""v2 tool behaviours: baseline action, related_work, multi-node dispatch."""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from surogates.arbor.store import ResearchStore
from surogates.tools.builtin.arbor import (
    _dispatch_experiments_handler,
    _idea_tree_handler,
)

from .test_arbor_tools import FakeSandboxPool, _StubSessionStore, _fake_spawn


@pytest_asyncio.fixture(loop_scope="session")
async def base_run(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/v2/trunk", branch_prefix="research/v2",
        objective="o",
    )
    await store.set_meta(run_id, {"eval_cmd": "python eval.py --split dev"})
    kwargs = {
        "session_factory": session_factory,
        "session_config": {"active_research_run_id": str(run_id)},
        "session_id": str(session_id), "sandbox_pool": FakeSandboxPool(),
        "session_store": _StubSessionStore(), "tenant": object(), "redis": object(),
    }
    return store, run_id, org_id, kwargs


@pytest.mark.asyncio(loop_scope="session")
async def test_baseline_action_spawns_a_baseline_experiment(base_run):
    store, run_id, org_id, kwargs = base_run
    spawn = AsyncMock(side_effect=_fake_spawn(
        kwargs["session_factory"], org_id, uuid.UUID(kwargs["session_id"]),
    ))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        out = json.loads(await _dispatch_experiments_handler(
            {"node_keys": [], "action": "baseline"}, **kwargs,
        ))
    assert out.get("baseline_dispatched") is True
    node = await store.get_node(run_id, "BASELINE")
    assert node.status == "running" and node.task_id is not None
    brief = spawn.call_args.kwargs["goal"]
    assert "baseline" in brief.lower() and "do not modify" in brief.lower()


@pytest.mark.asyncio(loop_scope="session")
async def test_baseline_rejected_when_already_set(base_run):
    store, run_id, org_id, kwargs = base_run
    await store.set_meta(run_id, {"baseline_score": 0.4})
    out = json.loads(await _dispatch_experiments_handler(
        {"node_keys": [], "action": "baseline"}, **kwargs,
    ))
    assert "already set" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_baseline_fold_writes_baseline_score(base_run):
    from surogates.db.models import Task

    store, run_id, org_id, kwargs = base_run
    spawn = AsyncMock(side_effect=_fake_spawn(
        kwargs["session_factory"], org_id, uuid.UUID(kwargs["session_id"]),
    ))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        await _dispatch_experiments_handler({"node_keys": [], "action": "baseline"}, **kwargs)
    node = await store.get_node(run_id, "BASELINE")
    # Executor completes with a dev score; record_from_task folds it.
    async with kwargs["session_factory"]() as db:
        t = await db.get(Task, node.task_id)
        t.status = "done"
        t.result_metadata = {"node_key": "BASELINE", "score": 0.37, "insight": "baseline"}
        await db.commit()
    out = json.loads(await _idea_tree_handler(
        {"action": "record_from_task", "task_id": str(node.task_id)}, **kwargs,
    ))
    assert out["folded"] == "BASELINE"
    run = await store.get_run(run_id)
    assert run.meta["baseline_score"] == 0.37


@pytest.mark.asyncio(loop_scope="session")
async def test_related_work_is_writable_via_update(base_run):
    store, run_id, org_id, kwargs = base_run
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h")
    out = json.loads(await _idea_tree_handler(
        {"action": "update", "node_key": "1",
         "fields": {"related_work": "see Smith et al. 2024"}}, **kwargs,
    ))
    assert out["ok"]
    assert (await store.get_node(run_id, "1")).related_work == "see Smith et al. 2024"


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_rejects_duplicate_node_keys(base_run):
    store, run_id, org_id, kwargs = base_run
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h")
    out = json.loads(await _dispatch_experiments_handler(
        {"node_keys": ["1", "1"]}, **kwargs,
    ))
    assert "duplicate" in out["error"]


@pytest.mark.asyncio(loop_scope="session")
async def test_dispatch_two_nodes_spawns_two_worktrees(base_run):
    store, run_id, org_id, kwargs = base_run
    await store.set_meta(run_id, {"max_parallel": 2})
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="a")
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="b")
    spawn = AsyncMock(side_effect=_fake_spawn(
        kwargs["session_factory"], org_id, uuid.UUID(kwargs["session_id"]),
    ))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        out = json.loads(await _dispatch_experiments_handler(
            {"node_keys": ["1", "2"]}, **kwargs,
        ))
    assert out["dispatched"] == ["1", "2"]
    pool = kwargs["sandbox_pool"]
    adds = [i for (_, _, i) in pool.calls if "git worktree add" in i]
    assert len(adds) == 2
    assert spawn.await_count == 2
