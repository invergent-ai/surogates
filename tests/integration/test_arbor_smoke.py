"""Smoke-mode end-to-end protocol regression for arbor research missions.

No real git / training / LLM: a FakeSandboxPool scripts shell output and
create_task_and_spawn is patched. Drives one full arbor cycle through the
real store, tools, harvest fold, merge gate, and evaluator policy:

    create -> set_meta -> add -> dispatch -> (executor done) -> harvest
    -> merge gate (scripted held-out eval) -> report task -> verdict gate

and asserts the deterministic-satisfied gate flips a prose "satisfied" to
needs_revision until a machine-written test improvement AND a report task
both exist.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from surogates.arbor.evaluator_policy import adjust_research_verdict
from surogates.arbor.store import ResearchStore
from surogates.db.models import Session as ORMSession, Task
from surogates.missions.commands import (
    AutoResearchCommand,
    handle_research_mission_create,
)
from surogates.missions.store import MissionStore
from surogates.tools.builtin.arbor import (
    _dispatch_experiments_handler,
    _idea_tree_handler,
    _merge_experiment_handler,
)

from .conftest import create_org, create_user
from .test_arbor_tools import FakeSandboxPool, _StubSessionStore, _fake_spawn


@pytest_asyncio.fixture(loop_scope="session")
async def mission_free_session(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(ORMSession(
            id=session_id, org_id=org_id, user_id=user_id,
            agent_id="agent-x", config={},
        ))
        await db.commit()
    return org_id, user_id, session_id


@pytest.mark.asyncio(loop_scope="session")
async def test_one_full_arbor_cycle_end_to_end(session_factory, mission_free_session):
    org_id, user_id, session_id = mission_free_session
    store = ResearchStore(session_factory)
    session_store = _StubSessionStore()

    # 1. CREATE via /auto-research handler (server-side baselines included).
    cmd = AutoResearchCommand(
        action="create", description="Improve F1 on ./repo",
        rubric="- test_trunk_score improves on test_baseline_score",
        repo="/workspace/repo", max_iterations=20,
        baseline=0.40, baseline_test=0.50,
    )
    created = await handle_research_mission_create(
        cmd=cmd, session_id=session_id, org_id=org_id, agent_id="agent-x",
        session_store=session_store, session_factory=session_factory,
        mission_store=MissionStore(session_factory), user_id=user_id,
    )
    assert created.ok
    mission_id = created.mission_id
    run = await store.get_run_for_mission(mission_id)
    run_id = run.id

    # Harness handler kwargs for the coordinator session.
    pool = FakeSandboxPool()
    kwargs = {
        "session_factory": session_factory,
        "session_config": {"active_research_run_id": str(run_id)},
        "session_id": str(session_id),
        "sandbox_pool": pool,
        "session_store": session_store,
        "tenant": object(), "redis": object(),
    }

    # 2. INIT: coordinator stamps the eval contract, then ideates.
    out = json.loads(await _idea_tree_handler(
        {"action": "set_meta", "values": {
            "eval_cmd": "python eval.py --split dev",
            "eval_cmd_test": "python eval.py --split test",
            "max_cycles": 4,
        }}, **kwargs,
    ))
    assert out["ok"]
    out = json.loads(await _idea_tree_handler(
        {"action": "add", "parent_key": "ROOT",
         "hypothesis": "Mechanism: M\nHypothesis: H\nObservable: O\nConflicts: none"},
        **kwargs,
    ))
    assert out["node_key"] == "1"

    # 3. DISPATCH (mocked spawn inserts a real Task row -> FK holds).
    spawn = AsyncMock(side_effect=_fake_spawn(session_factory, org_id, session_id))
    with patch("surogates.tasks.service.create_task_and_spawn", new=spawn):
        out = json.loads(await _dispatch_experiments_handler(
            {"node_keys": ["1"]}, **kwargs,
        ))
    assert out["dispatched"] == ["1"]
    node = await store.get_node(run_id, "1")
    assert node.status == "running" and node.task_id is not None

    # 4. EXECUTOR completes: mark its Task done with a structured report.
    async with session_factory() as db:
        task = await db.get(Task, node.task_id)
        task.status = "done"
        task.result = "improved the retrieval step"
        task.result_metadata = {
            "node_key": "1", "score": 0.55,
            "insight": "retrieval was the bottleneck", "result": "F1 up",
        }
        await db.commit()

    # 5. HARVEST (via the coordinator's record_from_task correction channel,
    # which runs the same deterministic fold the wake hook uses).
    out = json.loads(await _idea_tree_handler(
        {"action": "record_from_task", "task_id": str(node.task_id)}, **kwargs,
    ))
    assert out["folded"] == "1"
    node = await store.get_node(run_id, "1")
    assert node.status == "done" and node.score == 0.55

    # 5a. Before any merge, a prose "satisfied" is DEMOTED (no machine score,
    # no report task).
    run = await store.get_run(run_id)
    demoted = adjust_research_verdict(
        {"result": "satisfied", "explanation": "looks great", "feedback": ""},
        meta=run.meta or {}, report_task_done=False,
    )
    assert demoted["result"] == "needs_revision"

    # 6. MERGE gate: start launches the detached held-out eval; status reads
    # the scripted result.json. 0.61 > test_baseline 0.50 -> merge.
    out = json.loads(await _merge_experiment_handler(
        {"action": "start", "node_key": "1"}, **kwargs,
    ))
    assert out["started"] == "1"
    pool.responses["cat /workspace/.arbor/merge-eval/1/result.json"] = '{"score": 0.61}'
    out = json.loads(await _merge_experiment_handler(
        {"action": "status", "node_key": "1"}, **kwargs,
    ))
    assert out["merged"] is True and out["test_score"] == 0.61
    run = await store.get_run(run_id)
    assert run.meta["test_trunk_score"] == 0.61
    assert (await store.get_node(run_id, "1")).status == "merged"

    # 7. FINALIZE: a report task completes. Now the satisfied gate passes.
    async with session_factory() as db:
        report_task = Task(
            org_id=org_id, parent_session_id=session_id,
            agent_def_name="arbor-executor", goal="write report",
            status="done", max_attempts=1, mission_id=mission_id,
            result_metadata={"report": True},
        )
        db.add(report_task)
        await db.commit()

    run = await store.get_run(run_id)
    satisfied = adjust_research_verdict(
        {"result": "satisfied", "explanation": "done", "feedback": ""},
        meta=run.meta or {}, report_task_done=True,
    )
    assert satisfied["result"] == "satisfied"  # machine-verified improvement + report
