"""Platform-level idle-session reset.

Same shape as :mod:`surogates.jobs.platform_cleanup` for the
idle-reset operation.

The K8s CronJob that invokes this script lives in
``surogates/k8s/platform/idle-reset-cronjob.yaml.template`` and
runs every 5 minutes by default.

Agent enumeration + per-agent idle-reset are injected via
parameters so tests can substitute fakes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)


AgentIter = Callable[[], AsyncIterator[dict]]
IdleResetFn = Callable[[dict], Awaitable[dict]]


async def run_platform_idle_reset(
    *,
    agent_iter: AgentIter,
    idle_reset_for_agent: IdleResetFn,
) -> dict[str, dict]:
    """Iterate agents; run ``idle_reset_for_agent`` for each;
    return ``{agent_id: per-agent-outcome}``.

    Per-agent failures captured in the outcome dict; iteration
    continues.  Mirrors :func:`run_platform_cleanup`.
    """
    outcomes: dict[str, dict] = {}
    async for agent in agent_iter():
        agent_id = agent.get("id")
        if agent_id is None:
            logger.warning(
                "platform_idle_reset skipping agent with no id: %r",
                agent,
            )
            continue
        try:
            outcomes[agent_id] = await idle_reset_for_agent(agent)
        except Exception as exc:  # noqa: BLE001 — per-agent isolated
            logger.error(
                "platform_idle_reset failed for agent %s: %s",
                agent_id, exc, exc_info=True,
            )
            outcomes[agent_id] = {
                "error": f"{type(exc).__name__}: {exc}",
            }
    return outcomes


async def _default_idle_reset_for_agent(  # pragma: no cover
    agent: dict,
) -> dict:
    raise NotImplementedError(
        "Production idle-reset helper wiring is pending.",
    )


async def _default_agent_iter() -> AsyncIterator[dict]:  # pragma: no cover
    raise NotImplementedError(
        "Production agent enumeration wiring is pending.",
    )
    yield {}  # noqa: B901 -- unreachable; typing only


async def main(
    *,
    agent_iter: AgentIter | None = None,
    idle_reset_for_agent: IdleResetFn | None = None,
) -> dict[str, dict]:
    """CLI entry for the platform idle-reset CronJob."""
    if agent_iter is None:
        agent_iter = _default_agent_iter
    if idle_reset_for_agent is None:
        idle_reset_for_agent = _default_idle_reset_for_agent
    return await run_platform_idle_reset(
        agent_iter=agent_iter,
        idle_reset_for_agent=idle_reset_for_agent,
    )


if __name__ == "__main__":  # pragma: no cover - CLI path
    asyncio.run(main())
