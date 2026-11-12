"""The idle-mission nudge: re-drives an active mission whose coordinator has gone quiet.

The orchestrator runs :func:`nudge_idle_missions` on a short tick
(``Orchestrator._missions_tick_forever``).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger(__name__)

#: The most missions one tick nudges.
_MAX_ENQUEUES_PER_TICK: int = 10


# How long a mission's coordinator may be idle before the tick re-drives it.
# Generous on purpose: poking a coordinator mid-thought is worse than waiting,
# and the sweep only exists to rescue objectives nothing else will touch.
_MISSION_IDLE_SECONDS: int = 900

# Nudges without an intervening evaluation before the mission is handed to a
# human. Matches the orphan-recovery ceiling and the parse-failure pause, so an
# operator meets one number rather than three.
_MAX_MISSION_NUDGES: int = 3


async def nudge_idle_missions(
    *,
    session_factory: async_sessionmaker,
    session_store: Any,
    redis: Any,
    idle_seconds: int = _MISSION_IDLE_SECONDS,
) -> int:
    """Re-drive active missions whose coordinator has gone quiet.

    ``should_evaluate`` only runs inside a coordinator turn, and a text-only
    LLM response ends that turn without re-enqueueing anything. A mission
    whose coordinator stops talking is therefore unreachable: no evaluation,
    no budget check, no stagnation count — every other mission gate is
    downstream of a loop nothing was driving.

    Skips a mission whose coordinator currently holds a lease: a worker is on
    it right now and interrupting mid-thought is worse than waiting.

    Bounded. Nudges since the mission last progressed — its last evaluation,
    or its creation if it has never been evaluated — are counted, and past
    :data:`_MAX_MISSION_NUDGES` the mission is blocked for a human instead of
    being poked forever. An evaluation resets the count, because that means
    the loop moved.
    """
    from surogates.config import enqueue_session
    from surogates.db.models import Mission as MissionRow
    from surogates.missions.store import MissionStore
    from surogates.session.events import EventType

    # Compared by the database clock, which is the one that stamped
    # ``updated_at`` — these columns are naive, so a Python-side aware
    # datetime would not even encode.
    async with session_factory() as db:
        rows = (await db.execute(
            select(MissionRow).where(
                MissionRow.status == "active",
                MissionRow.updated_at
                < func.now() - text(f"make_interval(secs => {int(idle_seconds)})"),
            ).limit(_MAX_ENQUEUES_PER_TICK)
        )).scalars().all()
        missions = [
            (m.id, m.session_id, m.org_id, m.agent_id, m.description,
             m.last_evaluation_at, m.created_at)
            for m in rows
        ]

    store = MissionStore(session_factory)
    woken = 0
    for mid, sid, org_id, agent_id, description, last_eval, created in missions:
        try:
            if await session_store.has_live_lease(sid):
                continue
            # A coordinator waiting on its agents is not idle: the judge holds
            # off while they run, so no evaluation would reset the nudge count.
            if await store.has_agents_in_flight(mid):
                continue

            since = last_eval or created
            nudges = await session_store.count_synthetic_since(
                sid, synthetic="mission_nudge", since=since,
            )
            if nudges >= _MAX_MISSION_NUDGES:
                logger.warning(
                    "mission %s nudged %d times with no evaluation; blocking",
                    mid, nudges,
                )
                await store.set_status(
                    mid, "blocked",
                    paused_reason=(
                        f"coordinator went quiet after {nudges} nudges "
                        "without an evaluation"
                    ),
                )
                continue

            await session_store.emit_event(
                sid, EventType.USER_MESSAGE,
                {
                    "content": (
                        "[Mission still open]\n\n"
                        f"Objective: {description}\n\n"
                        "Your last turn ended without completing this mission "
                        "and nothing has advanced it since. Continue the work: "
                        "start the next agent, or if the objective is genuinely "
                        "finished say so and mark it complete."
                    ),
                    "synthetic": "mission_nudge",
                    "mission_id": str(mid),
                },
            )
            await enqueue_session(
                redis, org_id=str(org_id), agent_id=agent_id, session_id=sid,
            )
            woken += 1
        except Exception:
            logger.exception("mission liveness nudge failed for %s", mid)

    return woken
