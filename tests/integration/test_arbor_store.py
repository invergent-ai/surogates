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
