"""Platform-level session-workspace cleanup.

A single platform-level script that iterates ALL agents and
runs workspace-prefix-cleanup logic per agent.

The K8s CronJob that invokes this script lives in
``surogates/k8s/platform/cleanup-cronjob.yaml.template`` and
runs every 6 hours by default.

Agent enumeration is injected via the ``agent_iter`` parameter
so tests can substitute a fixed list.  Production callers pass
``None`` and the default implementation fetches agents from the
surogate-ops api endpoint ``GET /api/agents``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

logger = logging.getLogger(__name__)


AgentIter = Callable[[], AsyncIterator[dict]]
CleanupFn = Callable[[dict], Awaitable[dict]]


async def run_platform_cleanup(
    *,
    agent_iter: AgentIter,
    cleanup_for_agent: CleanupFn,
) -> dict[str, dict]:
    """Iterate agents; run ``cleanup_for_agent`` for
    each; return ``{agent_id: per-agent-outcome}``.

    Per-agent failures are captured in the outcome dict
    (``{"error": "<exception class>: <message>"}``); they do NOT
    stop iteration.  A platform-wide failure (e.g. the agent
    iterator raising before yielding) bubbles out.
    """
    outcomes: dict[str, dict] = {}
    async for agent in agent_iter():
        agent_id = agent.get("id")
        if agent_id is None:
            logger.warning(
                "platform_cleanup skipping agent with no id: %r", agent,
            )
            continue
        try:
            outcomes[agent_id] = await cleanup_for_agent(agent)
        except Exception as exc:  # noqa: BLE001 — per-agent isolated
            logger.error(
                "platform_cleanup failed for agent %s: %s",
                agent_id, exc, exc_info=True,
            )
            outcomes[agent_id] = {
                "error": f"{type(exc).__name__}: {exc}",
            }
    return outcomes


async def _default_cleanup_for_agent(agent: dict) -> dict:  # pragma: no cover
    """Production cleanup -- thin orchestration around the
    storage-backend list/delete for a single agent.
    """
    raise NotImplementedError(
        "Production cleanup helper wiring is pending.  "
        "For now, callers must inject cleanup_for_agent.",
    )


async def _default_agent_iter() -> AsyncIterator[dict]:  # pragma: no cover
    """Production agent enumeration -- fetches agents from
    the surogate-ops api.
    """
    raise NotImplementedError(
        "Production agent enumeration wiring is pending.  "
        "For now, callers must inject agent_iter.",
    )
    # Unreachable -- here to make the iterator typing valid.
    yield {}  # noqa: B901


async def main(
    *,
    agent_iter: AgentIter | None = None,
    cleanup_for_agent: CleanupFn | None = None,
) -> dict[str, dict]:
    """CLI entry for the platform cleanup CronJob."""
    if agent_iter is None:
        agent_iter = _default_agent_iter
    if cleanup_for_agent is None:
        cleanup_for_agent = _default_cleanup_for_agent
    return await run_platform_cleanup(
        agent_iter=agent_iter,
        cleanup_for_agent=cleanup_for_agent,
    )


if __name__ == "__main__":  # pragma: no cover - CLI path
    asyncio.run(main())
