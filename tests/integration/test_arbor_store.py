"""Integration tests for the Arbor research tables and ResearchStore."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from surogates.db.models import IdeaNode, ResearchRun


@pytest.mark.asyncio(loop_scope="session")
async def test_research_tables_roundtrip(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    async with session_factory() as db:
        run = ResearchRun(
            org_id=org_id, mission_id=mission_id, session_id=session_id,
            agent_id="agent-x", repo_path="/workspace/repo",
            trunk_branch="research/trunk", branch_prefix="research/run1",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id
        assert run.status == "init"
        assert run.meta == {}

        root = IdeaNode(
            org_id=org_id, run_id=run_id, node_key="ROOT",
            parent_key=None, depth=0, hypothesis="(objective)",
        )
        db.add(root)
        await db.commit()

    async with session_factory() as db:
        got = (await db.execute(
            select(IdeaNode).where(IdeaNode.run_id == run_id)
        )).scalars().all()
        assert [n.node_key for n in got] == ["ROOT"]
        assert got[0].status == "pending"


@pytest.mark.asyncio(loop_scope="session")
async def test_idea_node_key_unique_per_run(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    async with session_factory() as db:
        run = ResearchRun(
            org_id=org_id, mission_id=mission_id, session_id=session_id,
            agent_id="agent-x", repo_path="/workspace/repo",
            trunk_branch="t", branch_prefix="p",
        )
        db.add(run)
        await db.commit()
        await db.refresh(run)
        run_id = run.id
        db.add(IdeaNode(org_id=org_id, run_id=run_id, node_key="1",
                        parent_key="ROOT", depth=1, hypothesis="h"))
        await db.commit()
    async with session_factory() as db:
        db.add(IdeaNode(org_id=org_id, run_id=run_id, node_key="1",
                        parent_key="ROOT", depth=1, hypothesis="dup"))
        with pytest.raises(Exception):  # IntegrityError via uq constraint
            await db.commit()


# ---------------------------------------------------------------------------
# ResearchStore
# ---------------------------------------------------------------------------

from surogates.arbor.store import (  # noqa: E402
    MetaKeyError,
    NodeStateError,
    ResearchStore,
    is_improvement,
)


@pytest_asyncio.fixture(loop_scope="session")
async def research_run(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/trunk", branch_prefix="research/run1",
        objective="maximize F1",
    )
    return store, run_id, org_id


@pytest.mark.asyncio(loop_scope="session")
async def test_create_run_seeds_root_and_defaults(research_run):
    store, run_id, _ = research_run
    run = await store.get_run(run_id)
    assert run.status == "init"
    assert run.meta["metric_direction"] == "maximize"
    assert run.meta["max_cycles"] == 20
    root = await store.get_node(run_id, "ROOT")
    assert root.depth == 0 and root.status == "pending"


@pytest.mark.asyncio(loop_scope="session")
async def test_set_meta_enforces_closed_keys_and_machine_keys(research_run):
    store, run_id, _ = research_run
    await store.set_meta(run_id, {"eval_cmd": "python eval.py --split dev"})
    assert (await store.get_run(run_id)).meta["eval_cmd"] == "python eval.py --split dev"
    with pytest.raises(MetaKeyError):
        await store.set_meta(run_id, {"not_a_real_key": 1})
    with pytest.raises(MetaKeyError):  # machine-score keys rejected from the LLM path
        await store.set_meta(run_id, {"test_trunk_score": 99.0})
    await store.set_meta(
        run_id, {"test_trunk_score": 99.0}, allow_machine_keys=True,
    )
    assert (await store.get_run(run_id)).meta["test_trunk_score"] == 99.0


@pytest.mark.asyncio(loop_scope="session")
async def test_add_update_and_cycle_accounting(research_run):
    store, run_id, org_id = research_run
    await store.add_node(
        run_id, org_id=org_id, parent_key="ROOT",
        hypothesis="Mechanism: X\nHypothesis: Y\nObservable: Z\nConflicts: none",
    )
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h2")
    nodes = await store.list_nodes(run_id)
    keys = sorted(n.node_key for n in nodes if n.node_key != "ROOT")
    assert keys == ["1", "2"]
    assert await store.cycles_spent(run_id) == 0
    await store.update_node(run_id, "1", status="running")
    await store.update_node(run_id, "1", status="done", score=0.41,
                            insight="works", result="ok")
    await store.update_node(run_id, "2", status="failed", insight="Timed out")
    assert await store.cycles_spent(run_id) == 2  # done + failed both spend
    assert await store.in_flight_count(run_id) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_prune_is_recursive_and_terminal(research_run):
    store, run_id, org_id = research_run
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="h1")
    await store.add_node(run_id, org_id=org_id, parent_key="1", hypothesis="child")
    await store.prune(run_id, "1", reason="dead end")
    n1 = await store.get_node(run_id, "1")
    child = await store.get_node(run_id, "1.1")
    assert n1.status == "pruned" and child.status == "pruned"
    assert "[Pruned: dead end]" in (n1.insight or "")
    with pytest.raises(NodeStateError):
        await store.update_node(run_id, "1", status="running")


def test_is_improvement_direction_aware():
    assert is_improvement(0.5, 0.4, "maximize")
    assert not is_improvement(0.3, 0.4, "maximize")
    assert is_improvement(0.3, 0.4, "minimize")
    assert is_improvement(0.5, None, "maximize")  # no baseline yet -> improvement
    assert not is_improvement(None, 0.4, "maximize")


@pytest.mark.asyncio(loop_scope="session")
async def test_constraints_block_shows_hitl_mode(research_run):
    store, run_id, _ = research_run
    await store.set_meta(run_id, {"hitl_mode": "review"})
    block = await store.constraints_block(run_id)
    assert "HITL mode: review" in block


@pytest.mark.asyncio(loop_scope="session")
async def test_constraints_block_renders_sections(research_run):
    store, run_id, org_id = research_run
    await store.set_meta(run_id, {"eval_cmd": "python eval.py", "max_cycles": 8})
    await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis="first idea")
    await store.update_node(run_id, "1", status="done", score=0.42, insight="i")
    block = await store.constraints_block(run_id)
    assert "### TREE SHAPE" in block
    assert "### ROOT INSIGHT" in block
    assert "### VALIDATED FINDINGS" in block
    assert "- 1 [done] score=0.42 first idea" in block
    assert "cycles: 1/8" in block
    assert "B_test ONLY through merge_experiment" in block
