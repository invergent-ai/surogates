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
from surogates.memory.manager import MemoryManager
from surogates.memory.store import MemoryStore
from surogates.orchestrator.dispatcher import Orchestrator
from surogates.session.store import SessionStore
from surogates.tenant.context import TenantContext
from surogates.sandbox.pool import SandboxPool
from surogates.sandbox.process import ProcessSandbox
from surogates.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from surogates.config import Settings

logger = logging.getLogger(__name__)


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
    sandbox_backend = ProcessSandbox()
    sandbox_pool = SandboxPool(sandbox_backend)

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

    # 5b. MCP tools — discover from platform + tenant MCP configs.
    from surogates.tools.mcp.proxy import MCPToolProxy
    mcp_proxy = MCPToolProxy(tool_registry)

    # Load MCP server configs from platform volume + tenant asset root.
    mcp_servers: dict[str, dict] = {}
    from surogates.tools.loader import ResourceLoader
    try:
        # Platform-level MCP configs (mounted from ConfigMap).
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

    # 7. Harness factory -- creates a fully-wired AgentHarness for a given session.
    async def harness_factory(session_id: UUID) -> AgentHarness:
        """Build an AgentHarness with all dependencies injected.

        Resolves the tenant from the session's user_id + the configured org_id.
        """
        # Load session to get user_id.
        session = await session_store.get_session(session_id)

        # Load org + user from DB.
        from sqlalchemy import select as sa_select
        from surogates.db.models import Org, User

        async with session_factory() as db:
            org_row = (await db.execute(
                sa_select(Org).where(Org.id == configured_org_id)
            )).scalar_one_or_none()
            user_row = (await db.execute(
                sa_select(User).where(User.id == session.user_id)
            )).scalar_one_or_none()

        tenant = TenantContext(
            org_id=configured_org_id,
            user_id=session.user_id,
            org_config=org_row.config if org_row else {},
            user_preferences=user_row.preferences if user_row else {},
            permissions=frozenset(),
            asset_root=f"{settings.tenant_assets_root}/{configured_org_id}",
        )

        model_id = settings.llm.model or "gpt-4o"
        budget = IterationBudget(max_total=90)
        compressor = ContextCompressor(model_id)

        # Create MemoryStore + MemoryManager.
        from pathlib import Path
        memory_dir = Path(tenant.asset_root) / "memory"
        memory_store = MemoryStore(memory_dir=memory_dir)
        memory_manager = MemoryManager(memory_store)

        prompt_builder = PromptBuilder(tenant, memory_manager=memory_manager)

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
        )

    # 8. Orchestrator
    orchestrator = Orchestrator(
        redis_client=redis_client,
        session_store=session_store,
        harness_factory=harness_factory,
        max_concurrent=settings.worker.concurrency,
        queue_key=settings.worker.queue_name,
        poll_timeout=settings.worker.poll_timeout,
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
        settings.worker.queue_name,
    )

    try:
        await orchestrator.run()
    finally:
        # Cleanup
        logger.info("Worker %s shutting down", worker_id)
        await sandbox_pool.destroy_all()
        mcp_proxy.shutdown_all()
        await redis_client.aclose()
        await engine.dispose()
        await llm_client.close()
        logger.info("Worker %s stopped", worker_id)
