"""Worker process entry point.

Bootstraps all dependencies -- database pool, Redis client, session store,
tool registry, and the agent harness -- then runs the
orchestrator loop until SIGTERM/SIGINT triggers graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from openai import AsyncOpenAI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.harness.prompt_library import default_library as default_prompt_library
from surogates.health import infrastructure_readiness, start_health_server
from surogates.memory.manager import MemoryManager
from surogates.memory.store import MemoryStore
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.session.store import SessionStore
from surogates.tenant.context import TenantContext
from surogates.sandbox.pool import SandboxPool
from surogates.sandbox.process import ProcessSandbox
from surogates.tools.loader import ResourceLoader
from surogates.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from surogates.config import Settings

logger = logging.getLogger(__name__)


async def _load_attached_kbs(
    *,
    agent_id: str,
    ops_db_url: str,
) -> list[dict]:
    """Look up the KBs attached to *agent_id* in the ops DB.

    Empty list is the safe default and is returned in three cases:

      - ``ops_db_url`` is empty (worker not wired to the KB platform).
      - The ops engine fails to initialize or query (network glitch,
        schema drift, etc.) -- we log and degrade gracefully rather
        than refuse to start the session.
      - The agent simply has no KBs attached.

    The dicts returned mirror what PromptBuilder._kb_section consumes:
    ``id``, ``name``, ``display_name``, ``description``. Keeping the
    surface plain dict (not a SQLAlchemy row) lets us cache it and
    pass it across async boundaries without dragging the session.
    """
    if not ops_db_url:
        return []
    try:
        from surogates.db.ops_engine import get_ops_session_factory
        from surogates.db.ops_models import (
            OpsKnowledgeBase, agent_knowledge_bases,
        )
        import sqlalchemy as sa

        factory = get_ops_session_factory()
        if factory is None:
            return []

        async with factory() as session:
            result = await session.execute(
                sa.select(
                    OpsKnowledgeBase.id,
                    OpsKnowledgeBase.name,
                    OpsKnowledgeBase.display_name,
                    OpsKnowledgeBase.description,
                )
                .join(
                    agent_knowledge_bases,
                    agent_knowledge_bases.c.kb_id == OpsKnowledgeBase.id,
                )
                .where(agent_knowledge_bases.c.agent_id == agent_id)
                .order_by(OpsKnowledgeBase.name.asc())
            )
            return [
                {
                    "id": row[0],
                    "name": row[1],
                    "display_name": row[2],
                    "description": row[3],
                }
                for row in result.all()
            ]
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to load attached KBs for agent %s; KB tools will be "
            "disabled for this session", agent_id, exc_info=True,
        )
        return []


async def _load_prompt_catalogs(
    *,
    settings: Settings,
    tenant: TenantContext,
    session_factory: Any,
) -> tuple[list[Any], list[Any]]:
    """Load prompt-visible sub-agent and skill catalogs for a tenant."""
    resource_loader = ResourceLoader(
        platform_skills_dir=getattr(settings, "platform_skills_dir", None),
        platform_agents_dir=getattr(settings, "platform_agents_dir", None),
    )

    try:
        async with session_factory() as _db:
            available_agents = await resource_loader.load_agents(
                tenant, db_session=_db,
            )
    except Exception:
        logger.debug(
            "Failed to load sub-agent catalog for tenant %s",
            tenant.org_id, exc_info=True,
        )
        available_agents = []

    try:
        async with session_factory() as _db:
            available_skills = await resource_loader.load_skills(
                tenant, db_session=_db,
            )
    except Exception:
        logger.debug(
            "Failed to load skill catalog for tenant %s",
            tenant.org_id, exc_info=True,
        )
        available_skills = []

    return available_agents, available_skills


async def run_worker(settings: Settings) -> None:
    """Bootstrap all dependencies and run the orchestrator loop.

    1. Create async SQLAlchemy engine + session factory.
    2. Create Redis client.
    3. Create SessionStore.
    4. Create ToolRegistry.
    5. Define harness_factory that builds AgentHarness per session.
    6. Create Orchestrator.
    7. Handle SIGTERM/SIGINT for graceful shutdown.
    8. Run orchestrator.run().
    """

    # 0. Prompt fragments -- validate up front so a missing or malformed
    # fragment fails the readiness probe instead of crashing live sessions
    # mid-turn.  Validated bodies stay cached so the agent loop never
    # hits disk for prompt prose.
    default_prompt_library().validate()

    # 1. Database
    engine = create_async_engine(
        settings.db.url,
        pool_size=settings.db.pool_size,
        max_overflow=settings.db.pool_overflow,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # 2. Redis
    redis_client = Redis.from_url(
        settings.redis.url,
        decode_responses=False,
    )

    # 3. Session store
    session_store = SessionStore(session_factory, redis=redis_client)

    # 4a. Sandbox pool -- one sandbox per session, lazily provisioned.
    if settings.sandbox.backend == "kubernetes":
        from surogates.sandbox.kubernetes import K8sSandbox
        sandbox_backend = K8sSandbox(
            namespace=settings.sandbox.k8s_namespace,
            service_account=settings.sandbox.k8s_service_account,
            pod_ready_timeout=settings.sandbox.k8s_pod_ready_timeout,
            executor_path=settings.sandbox.k8s_executor_path,
            storage_settings=settings.storage,
            s3fs_image=settings.sandbox.k8s_s3fs_image,
            s3_endpoint=settings.sandbox.k8s_s3_endpoint,
            mcp_proxy_url=settings.mcp_proxy_url,
        )
    else:
        sandbox_backend = ProcessSandbox()
    sandbox_pool = SandboxPool(sandbox_backend)

    # 4a. Optionally initialize the ops DB connection used by the
    # KB navigation tools (kb_list_pages, kb_read_page). Skipped when
    # SUROGATES_OPS_DB_URL is empty -- kb_tools handlers detect the
    # missing factory and return a polite "unavailable" message,
    # which is preferable to a crash for workers running without a
    # KB platform.
    if settings.ops_db.url:
        from surogates.db.ops_engine import init_ops_engine
        init_ops_engine(
            settings.ops_db.url,
            pool_size=settings.ops_db.pool_size,
            pool_overflow=settings.ops_db.pool_overflow,
        )
        logger.info("Ops DB initialized for KB tools")
    else:
        logger.info("Ops DB not configured; KB tools will return unavailable")

    # 4. Tool registry -- register all builtin tools + MCP tools.
    tool_registry = ToolRegistry()
    from surogates.tools.runtime import ToolRuntime
    tool_runtime = ToolRuntime(tool_registry)
    tool_runtime.register_builtins()
    logger.info(
        "Registered %d builtin tools: %s",
        len(tool_registry.tool_names),
        ", ".join(sorted(tool_registry.tool_names)),
    )

    # 5b. MCP tools — two modes:
    #   - Direct: worker connects to MCP servers in-process (dev mode)
    #   - Proxied: worker delegates to the MCP proxy service (production)
    from surogates.tools.mcp.proxy import MCPToolProxy
    mcp_proxy = MCPToolProxy(tool_registry)
    mcp_proxy_client: Any = None  # HTTP client for proxied mode

    if settings.mcp_proxy_url:
        # Production: MCP tools are called via the proxy service.
        # Tool discovery happens at session start (per-tenant), not here.
        from surogates.orchestrator.mcp_client import McpProxyClient
        mcp_proxy_client = McpProxyClient(
            base_url=settings.mcp_proxy_url,
            registry=tool_registry,
        )
        logger.info("MCP tools via proxy: %s", settings.mcp_proxy_url)
    else:
        # Dev mode: connect directly to MCP servers.
        mcp_servers: dict[str, dict] = {}
        try:
            platform_loader = ResourceLoader(platform_mcp_dir=settings.platform_mcp_dir)
            for server_def in platform_loader._load_mcp_from_dir(settings.platform_mcp_dir):
                mcp_servers[server_def.name] = {
                    "transport": server_def.transport,
                    "command": server_def.command,
                    "args": server_def.args,
                    "url": server_def.url,
                    "env": server_def.env,
                    "timeout": server_def.timeout,
                }
        except Exception:
            logger.debug("No platform MCP configs found", exc_info=True)

        if mcp_servers:
            registered_mcp = mcp_proxy.add_servers(mcp_servers)
            if registered_mcp:
                logger.info(
                    "Registered %d MCP tools from %d servers: %s",
                    len(registered_mcp),
                    len(mcp_servers),
                    ", ".join(sorted(registered_mcp)),
                )
        else:
            logger.debug("No MCP servers configured")

    # 6. LLM client -- configured from settings.llm (config.yaml + env vars).
    llm_kwargs: dict[str, Any] = {}
    if settings.llm.api_key:
        llm_kwargs["api_key"] = settings.llm.api_key
    if settings.llm.base_url:
        llm_kwargs["base_url"] = settings.llm.base_url
    llm_client = AsyncOpenAI(**llm_kwargs)

    logger.info(
        "LLM client: model=%s, base_url=%s, api_key=%s",
        settings.llm.model,
        settings.llm.base_url or "(default)",
        f"{settings.llm.api_key[:12]}..." if settings.llm.api_key else "(not set)",
    )

    # Worker identity -- from K8s downward API or a generated fallback.
    worker_id = settings.worker_id or f"worker-{id(asyncio.get_event_loop()):x}"

    # Resolve the org_id from config (required).
    if not settings.org_id:
        raise RuntimeError(
            "SUROGATES_ORG_ID is not set. Each agent instance must belong to an org. "
            "Set org_id in config.yaml or SUROGATES_ORG_ID env var."
        )
    configured_org_id = UUID(settings.org_id)

    # Resolve the agent_id from config (required).  Sessions belong to an
    # agent; a worker refuses to process sessions that belong to a
    # different agent.
    if not settings.agent_id:
        raise RuntimeError(
            "SUROGATES_AGENT_ID is not set. Each worker instance serves "
            "exactly one agent. Set agent_id in config.yaml or "
            "SUROGATES_AGENT_ID env var."
        )
    configured_agent_id = settings.agent_id

    # 7. Harness factory -- creates a fully-wired AgentHarness for a given session.
    async def harness_factory(session_id: UUID) -> AgentHarness:
        """Build an AgentHarness with all dependencies injected.

        Resolves the tenant from the session's user_id + the configured org_id.
        """
        # Load session to get user_id.
        session = await session_store.get_session(session_id)

        # Refuse to process sessions that belong to a different agent —
        # defence-in-depth in case a foreign session id leaks into this
        # worker's queue.
        if session.agent_id != configured_agent_id:
            raise RuntimeError(
                f"session {session_id} belongs to agent {session.agent_id!r}, "
                f"this worker serves agent {configured_agent_id!r}"
            )

        from sqlalchemy import select as sa_select
        from surogates.db.models import Org, User

        async with session_factory() as db:
            org_row = (await db.execute(
                sa_select(Org).where(Org.id == configured_org_id)
            )).scalar_one_or_none()
            if session.user_id is not None:
                user_row = (await db.execute(
                    sa_select(User).where(User.id == session.user_id)
                )).scalar_one_or_none()
            else:
                user_row = None

        tenant = TenantContext(
            org_id=configured_org_id,
            user_id=session.user_id,
            org_config=org_row.config if org_row else {},
            user_preferences=user_row.preferences if user_row else {},
            permissions=frozenset(),
            asset_root=f"{settings.tenant_assets_root}/{configured_org_id}",
            service_account_id=session.service_account_id,
        )

        if not settings.llm.model:
            # Worker raises rather than returns a 503 because there's no
            # HTTP response surface here.  ``dispatcher._process``
            # catches and emits ``HARNESS_CRASH`` events; after 3 retries
            # with exponential backoff the dispatcher promotes the
            # session to ``SESSION_FAIL``.  A deployment that
            # legitimately leaves ``llm.model`` blank therefore burns
            # ~7s + log noise per session — operators should fix the
            # config rather than rely on the retry loop.
            raise RuntimeError(
                f"LLM model is not configured (settings.llm.model is empty); "
                f"cannot wake session {session.id}."
            )
        model_id = settings.llm.model
        budget = IterationBudget(max_total=90)
        compressor = ContextCompressor(
            model_id,
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            model_overrides=settings.llm.models,
        )

        # User-scoped memory dir for interactive sessions, org-shared
        # memory dir for service-account sessions (no per-user context
        # to carry forward).
        from pathlib import Path
        if session.user_id is not None:
            memory_dir = (
                Path(tenant.asset_root)
                / "users"
                / str(session.user_id)
                / "memory"
            )
        else:
            memory_dir = Path(tenant.asset_root) / "shared" / "memory"
        memory_store = MemoryStore(memory_dir=memory_dir)
        memory_manager = MemoryManager(memory_store)

        # Load prompt-visible catalogs.  Skills include expert
        # definitions, which the prompt lists so the default LLM knows
        # valid ``consult_expert`` names for voluntary delegation.
        # Coordinator sessions also render sub-agents as an "Available
        # Sub-Agents" block and use their presence to gate delegation
        # tool schemas.
        available_agents, available_skills = await _load_prompt_catalogs(
            settings=settings,
            tenant=tenant,
            session_factory=session_factory,
        )

        # Knowledge bases attached to this agent. Empty list when
        # KB tools are unavailable (no ops DB) or no KBs are wired
        # to this agent yet.
        attached_kbs = await _load_attached_kbs(
            agent_id=configured_agent_id,
            ops_db_url=settings.ops_db.url,
        )
        # Filter the tool set to drop kb_list_pages / kb_read_page
        # when this agent has nothing to navigate. Keeps the LLM from
        # ever seeing tool schemas it cannot meaningfully use, and
        # also keeps the prompt KB section empty so the LLM is not
        # primed to hallucinate kb_id values.
        effective_tools = set(tool_registry.tool_names)
        if not attached_kbs:
            effective_tools.discard("kb_list_pages")
            effective_tools.discard("kb_read_page")

        prompt_builder = PromptBuilder(
            tenant,
            skills=available_skills,
            memory_manager=memory_manager,
            session=session,
            available_agents=available_agents,
            available_kbs=attached_kbs,
            # The builder gates tool-aware guidance fragments (artifact,
            # memory, skills, expert, session_search, tool_use_enforcement)
            # on membership in ``available_tools``.  Passing the filtered
            # tool set keeps those fragments in sync with what the LLM
            # actually has access to this turn.
            available_tools=effective_tools,
        )

        # Interactive sessions get a regular user access token;
        # service-account sessions get a short-lived session-scoped SA
        # token so the harness can reach /v1/skills and /v1/memory on
        # their behalf.
        harness_api_client = None
        if settings.worker.use_api_for_harness_tools:
            from surogates.harness.api_client import HarnessAPIClient
            from surogates.tenant.auth.jwt import (
                create_access_token,
                create_service_account_session_token,
            )

            if tenant.user_id is not None:
                token = create_access_token(
                    org_id=tenant.org_id,
                    user_id=tenant.user_id,
                    permissions=set(tenant.permissions) or {
                        "sessions:read", "sessions:write", "tools:read",
                    },
                )
            elif session.service_account_id is not None:
                token = create_service_account_session_token(
                    org_id=tenant.org_id,
                    service_account_id=session.service_account_id,
                    session_id=session.id,
                )
            else:
                # Anonymous (website-channel) session — no principal to
                # mint a token from. Visitors hit ``/v1/website/sessions``
                # which deliberately creates sessions with ``user_id=None``
                # and no ``service_account_id`` (see
                # docs/channels/website.md §"Interaction with other
                # subsystems"). Falling through with
                # ``harness_api_client = None`` routes the harness into
                # the same code path the ``use_api_for_harness_tools=False``
                # config already supports: skill loading goes to local
                # disk via ``_load_all_skills`` (skills.py:225-229),
                # per-user memory is skipped (visitors have none by
                # design), and auto-artifact creation is silently
                # bypassed (loop.py:2185 guards on
                # ``self._api_client is None``). Tool allow-list
                # enforcement is unaffected; it reads from
                # ``session.config`` independently of the API client.
                logger.info(
                    "session %s has no principal; harness will run "
                    "without API client (anonymous website visitor)",
                    session.id,
                )
                token = None

            if token is not None:
                harness_api_client = HarnessAPIClient(
                    base_url=settings.worker.api_base_url,
                    token=token,
                    session_id=str(session.id),
                )

        return AgentHarness(
            session_store=session_store,
            tool_registry=tool_registry,
            llm_client=llm_client,
            tenant=tenant,
            worker_id=worker_id,
            budget=budget,
            context_compressor=compressor,
            prompt_builder=prompt_builder,
            redis_client=redis_client,
            memory_manager=memory_manager,
            sandbox_pool=sandbox_pool,
            api_client=harness_api_client,
            default_model=model_id,
            session_factory=session_factory,
            saga_enabled=settings.saga.enabled,
            saga_settings=settings.saga if settings.saga.enabled else None,
            log_policy_allowed=settings.governance.log_allowed,
        )

    # 8. Orchestrator — consumes from this agent's dedicated work queue.
    from surogates.config import agent_queue_key
    queue_key = agent_queue_key(configured_agent_id)
    orchestrator = Orchestrator(
        redis_client=redis_client,
        session_store=session_store,
        harness_factory=harness_factory,
        max_concurrent=settings.worker.concurrency,
        agent_id=configured_agent_id,
        queue_key=queue_key,
        poll_timeout=settings.worker.poll_timeout,
    )

    health_server = await start_health_server(
        settings.health_port,
        lambda: infrastructure_readiness(redis_client, session_factory),
    )

    # 9. Signal handling for graceful shutdown.
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logger.info("Received shutdown signal")
        loop.create_task(orchestrator.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    logger.info(
        "Worker %s starting (concurrency=%d, queue=%s)",
        worker_id,
        settings.worker.concurrency,
        queue_key,
    )

    try:
        await orchestrator.run()
    finally:
        # Cleanup
        logger.info("Worker %s shutting down", worker_id)
        await health_server.stop()
        await sandbox_pool.destroy_all()
        mcp_proxy.shutdown_all()
        if mcp_proxy_client is not None:
            await mcp_proxy_client.close()
        await redis_client.aclose()
        await engine.dispose()
        await llm_client.close()
        logger.info("Worker %s stopped", worker_id)
