"""Wake-time resolution of sub-agent types for child sessions.

When a coordinator spawns a worker via ``spawn_worker`` / ``delegate_task``
with ``agent_type=<name>``, the child session's ``config["agent_type"]``
is set.  On each :meth:`AgentHarness.wake`, the harness calls
:func:`resolve_agent_def` to load the corresponding :class:`AgentDef`
from the tenant's merged agent catalog, then :func:`apply_agent_def_to_session`
hydrates the session's in-memory config with the agent's presets
(tool filter, model, iteration cap, policy profile name).

The application is **non-destructive**: explicit values already on
``session.config`` always win over agent-def presets.  This lets the
spawn/delegate tools set overrides that take precedence, and also gives
the harness a safe fallback if an agent type is referenced without the
spawn tool having hydrated the rest of the config.

Mutation of ``session`` is scoped to the current wake cycle only -- the
session row in the database is not modified.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from surogates.tools.loader import AgentDef, ResourceLoader

if TYPE_CHECKING:
    from surogates.session.models import Session
    from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

# Absolute ceiling on iteration budgets applied at wake-time.  Matches
# the worker-spawn cap in ``tools/builtin/coordinator.WORKER_MAX_ITERATIONS``
# so an agent def loaded via ``session.config["agent_type"]`` (without
# going through ``spawn_worker``) cannot grant itself a bigger budget
# than a coordinator-spawned child would get.
_MAX_ITERATIONS_CEILING: int = 30


async def resolve_agent_by_name(
    name: str,
    tenant: TenantContext,
    *,
    session_factory: Any | None = None,
    loader: ResourceLoader | None = None,
) -> AgentDef | None:
    """Find an enabled :class:`AgentDef` by *name* in the tenant catalog.

    Returns ``None`` when the name is empty, no agent with that name
    exists, or the matched agent has ``enabled=False``.  Loader errors
    are logged and swallowed -- callers decide how to react to a
    missing agent.

    Shared between the harness wake path
    (:func:`resolve_agent_def`) and the coordinator/delegate spawn
    tools so the resolution rules cannot drift between them.

    Parameters
    ----------
    name:
        The agent name (from ``session.config['agent_type']`` or the
        ``agent_type`` argument on ``spawn_worker``/``delegate_task``).
    tenant:
        The tenant context used to scope the agent catalog.
    session_factory:
        Optional ``async_sessionmaker``.  When provided, the DB overlay
        layers (org_db, user_db) are consulted; otherwise only the
        filesystem layers are merged.
    loader:
        Optional injected :class:`ResourceLoader` for tests.  A default
        instance is created when not provided.
    """
    if not name:
        return None

    if loader is None:
        loader = ResourceLoader()

    try:
        if session_factory is not None:
            async with session_factory() as db:
                agents = await loader.load_agents(tenant, db_session=db)
        else:
            agents = await loader.load_agents(tenant)
    except Exception:
        logger.warning(
            "Failed to load agent catalog for agent_type=%s",
            name, exc_info=True,
        )
        return None

    for a in agents:
        if a.name == name and a.enabled:
            return a
    return None


async def resolve_agent_def(
    session: Session,
    tenant: TenantContext,
    *,
    session_factory: Any | None = None,
    loader: ResourceLoader | None = None,
) -> AgentDef | None:
    """Resolve ``session.config['agent_type']`` to an :class:`AgentDef`.

    Returns ``None`` when the session has no ``agent_type`` key, the
    name doesn't resolve to an enabled agent, or loading fails.  Emits
    a warning when ``agent_type`` is set but unresolvable so the
    condition is visible in worker logs.
    """
    agent_type = (session.config or {}).get("agent_type")
    if not agent_type:
        return None

    resolved = await resolve_agent_by_name(
        agent_type, tenant,
        session_factory=session_factory, loader=loader,
    )
    if resolved is None:
        logger.warning(
            "agent_type=%r set in session config but no matching enabled "
            "agent was found in the tenant catalog",
            agent_type,
        )
    return resolved


def apply_agent_def_to_session(
    session: Session,
    agent_def: AgentDef,
) -> None:
    """Hydrate ``session.config`` and ``session.model`` with agent presets.

    Modifies the in-memory ``session`` object only.  The update is
    non-destructive: existing keys in ``session.config`` and a non-null
    ``session.model`` win over the agent def's values.

    Populated keys:

    - ``allowed_tools`` -- from ``agent_def.tools`` (allowlist)
    - ``excluded_tools`` -- from ``agent_def.disallowed_tools`` (denylist)
    - ``max_iterations`` -- from ``agent_def.max_iterations``
    - ``policy_profile`` -- from ``agent_def.policy_profile`` (consulted in Step 6)

    Also fills in ``session.model`` if the session has no explicit
    model and the agent def provides one.
    """
    cfg = session.config

    if agent_def.tools is not None and not cfg.get("allowed_tools"):
        cfg["allowed_tools"] = list(agent_def.tools)
    if agent_def.disallowed_tools is not None and not cfg.get("excluded_tools"):
        cfg["excluded_tools"] = list(agent_def.disallowed_tools)
    if agent_def.max_iterations is not None and not cfg.get("max_iterations"):
        # Cap at the worker ceiling: spawn_worker already clamps to this
        # value, but a session created directly with ``config.agent_type``
        # (e.g. via a webhook) skips that path, so enforce it here too.
        cfg["max_iterations"] = min(
            agent_def.max_iterations, _MAX_ITERATIONS_CEILING,
        )
    if agent_def.policy_profile and not cfg.get("policy_profile"):
        cfg["policy_profile"] = agent_def.policy_profile

    if not session.model and agent_def.model:
        session.model = agent_def.model
