"""The harvest digest carries a convergence intervention once the run plateaus."""
from __future__ import annotations

import pytest
import pytest_asyncio

from surogates.arbor.store import ResearchStore
from surogates.harness.loop_arbor import ArborHarvestMixin


class _Harness(ArborHarvestMixin):
    """Minimal host exposing the attributes the mixin reads."""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._sandbox_pool = None
        self._llm = None
        self.events: list[str] = []

        outer = self

        class _Store:
            async def emit_event(self_inner, sid, etype, payload):
                outer.events.append(getattr(etype, "value", str(etype)))

        self._store = _Store()


@pytest_asyncio.fixture(loop_scope="session")
async def plateaued_run(session_factory, seeded_org_and_session):
    org_id, mission_id, session_id = seeded_org_and_session
    store = ResearchStore(session_factory)
    run_id = await store.create_run(
        org_id=org_id, mission_id=mission_id, session_id=session_id,
        agent_id="agent-x", repo_path="/workspace/repo",
        trunk_branch="research/cv/trunk", branch_prefix="research/cv",
        objective="maximize F1",
        meta_overrides={"convergence_min_experiments": 4, "convergence_warn_after": 3},
    )
    await store.set_meta(run_id, {"trunk_score": 0.50}, allow_machine_keys=True)
    for i, s in enumerate([0.50, 0.49, 0.48, 0.47], start=1):
        await store.add_node(run_id, org_id=org_id, parent_key="ROOT", hypothesis=f"h{i}")
        await store.update_node(run_id, str(i), status="done", score=s, insight="no gain")
    return store, run_id, session_id


@pytest.mark.asyncio(loop_scope="session")
async def test_harvest_digest_includes_convergence(session_factory, plateaued_run):
    from surogates.db.models import Task

    store, run_id, session_id = plateaued_run
    org = (await store.get_run(run_id)).org_id
    # A 5th experiment just finished; node 5 running and linked to a terminal task.
    await store.add_node(run_id, org_id=org, parent_key="ROOT", hypothesis="h5")
    async with session_factory() as db:
        t = Task(org_id=org, parent_session_id=session_id, agent_def_name="arbor-executor",
                 goal="g", status="done", max_attempts=1,
                 result_metadata={"score": 0.46, "insight": "still no gain"})
        db.add(t)
        await db.commit()
        await db.refresh(t)
        tid = t.id
    await store.update_node(run_id, "5", status="running", task_id=tid)

    harness = _Harness(session_factory)
    session = type("S", (), {"id": session_id, "model": None,
                             "config": {"active_research_run_id": str(run_id)}})()
    messages: list[dict] = []
    await harness.maybe_harvest_research(session, messages)

    digest = next((m["content"] for m in messages if "[research harvest]" in m["content"]), "")
    assert digest
    assert "CONVERGENCE" in digest
    assert "research.converged" in harness.events
