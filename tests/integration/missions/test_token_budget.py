"""What has this mission cost, and has it spent its allowance?

`missions` tracked iterations and nothing else. Token spend existed only
per-session, compared to nothing, so "how much has this objective consumed"
had no answer and no ceiling.

Spend is DERIVED — a SUM over the mission's sessions — not a counter
incremented alongside the work, so it cannot drift from what was actually
billed. Tokens are the enforcement quantity deliberately: PROD runs tier
sentinels priced at 0.0, which is how a 1.48M-token session recorded
`estimated_cost_usd = 0`.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import update

from surogates.db.models import Session as ORMSession, Task
from surogates.missions.store import MissionStore

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _spend(session_factory, session_id, *, inp, out):
    async with session_factory() as db:
        await db.execute(
            update(ORMSession).where(ORMSession.id == session_id)
            .values(input_tokens=inp, output_tokens=out)
        )
        await db.commit()


async def test_a_fresh_mission_has_spent_nothing(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
    )
    assert await store.tokens_spent(mid) == 0


async def test_the_coordinator_session_counts(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
    )
    await _spend(session_factory, chat_session.id, inp=100, out=50)
    assert await store.tokens_spent(mid) == 150


async def test_worker_sessions_under_the_mission_count(
    session_factory, org_id, user_id, chat_session, session_store,
):
    """Spend joins through tasks: a worker session bills to its mission."""
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
    )
    async with session_factory() as db:
        task = Task(
            org_id=org_id, parent_session_id=chat_session.id,
            goal="do it", status="ready", mission_id=mid,
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    worker = await session_store.create_session(
        org_id=org_id, user_id=user_id, agent_id="a",
        channel="web", task_id=task_id,
    )
    await _spend(session_factory, chat_session.id, inp=10, out=10)
    await _spend(session_factory, worker.id, inp=200, out=100)

    assert await store.tokens_spent(mid) == 320


async def test_another_missions_spend_is_not_counted(
    session_factory, org_id, user_id, chat_session, sa_chat_session,
):
    store = MissionStore(session_factory)
    a = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
    )
    await _spend(session_factory, sa_chat_session.id, inp=999, out=999)
    assert await store.tokens_spent(a) == 0


async def test_no_budget_is_never_exhausted(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
    )
    await _spend(session_factory, chat_session.id, inp=10**7, out=10**7)
    assert await store.budget_exhausted(mid) is False


async def test_under_budget_is_not_exhausted(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
        budget_tokens=1000,
    )
    await _spend(session_factory, chat_session.id, inp=100, out=100)
    assert await store.budget_exhausted(mid) is False


async def test_over_budget_is_exhausted(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works", budget_tokens=150,
    )
    await _spend(session_factory, chat_session.id, inp=100, out=100)
    assert await store.budget_exhausted(mid) is True


async def test_exhaustion_pauses_rather_than_inventing_a_status(
    session_factory, org_id, user_id, chat_session,
):
    """`paused` + `paused_reason` already exist and already render.

    A new terminal status would have to land in the SDK's types.ts and its
    rebuilt dist — a known release-breaker.
    """
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works", budget_tokens=10,
    )
    await _spend(session_factory, chat_session.id, inp=100, out=100)

    assert await store.pause_if_budget_exhausted(mid) is True
    mission = await store.get(mid)
    assert mission.status == "paused"
    assert mission.paused_reason == "budget_exhausted"


async def test_pausing_is_idempotent(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works", budget_tokens=10,
    )
    await _spend(session_factory, chat_session.id, inp=100, out=100)
    assert await store.pause_if_budget_exhausted(mid) is True
    assert await store.pause_if_budget_exhausted(mid) is False


async def test_an_unknown_mission_is_not_exhausted(session_factory):
    assert await MissionStore(session_factory).budget_exhausted(uuid.uuid4()) is False


async def test_an_exhausted_mission_stops_being_evaluated(
    session_factory, org_id, user_id, chat_session,
):
    """Budget is checked at the evaluator boundary, mirroring how Arbor
    enforces its cycle ceiling — and only after the cheap negative paths."""
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works", budget_tokens=10,
    )
    await _spend(session_factory, chat_session.id, inp=100, out=100)

    decision = await should_evaluate(
        mission_id=mid,
        coordinator_last_response="done\n[[mission-complete]]",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=0,
    )
    assert decision.should is False
    assert decision.trigger == "budget_exhausted"

    assert (await store.get(mid)).status == "paused"


async def test_a_mission_within_budget_still_evaluates(
    session_factory, org_id, user_id, chat_session,
):
    from surogates.missions.evaluator import should_evaluate

    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
        budget_tokens=10**6,
    )
    await _spend(session_factory, chat_session.id, inp=10, out=10)

    decision = await should_evaluate(
        mission_id=mid,
        coordinator_last_response="done\n[[mission-complete]]",
        session_factory=session_factory,
        mission_store=store,
        rate_limit_seconds=0,
    )
    assert decision.should is True


async def test_the_budget_verb_sets_it_on_a_running_mission(
    session_factory, org_id, user_id, chat_session,
):
    """The case that motivated this: a mission is already running and turns
    out to be burning more than expected."""
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
    )
    assert (await store.get(mid)).budget_tokens is None

    await store.set_budget(mid, budget_tokens=5_000_000)
    assert (await store.get(mid)).budget_tokens == 5_000_000


async def test_the_ceiling_can_be_taken_off_again(
    session_factory, org_id, user_id, chat_session,
):
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description="ship", rubric="works",
        budget_tokens=1000,
    )
    await store.set_budget(mid, budget_tokens=None)

    assert (await store.get(mid)).budget_tokens is None
    assert await store.budget_exhausted(mid) is False


async def test_a_budget_set_at_creation_via_the_command_path(
    session_factory, org_id, user_id, chat_session,
):
    """End to end: the parsed value reaches the row."""
    from surogates.missions.commands import parse_mission_command

    cmd = parse_mission_command("ship it\nBudget: 3M\nRubric: works")
    store = MissionStore(session_factory)
    mid = await store.create(
        org_id=org_id, user_id=user_id, session_id=chat_session.id,
        agent_id="a", description=cmd.description, rubric=cmd.rubric,
        budget_tokens=cmd.budget_tokens,
    )
    assert (await store.get(mid)).budget_tokens == 3_000_000
