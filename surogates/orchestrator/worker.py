"""Worker process entry point.

Bootstraps all dependencies -- database pool, Redis client, session store,
sandbox pool, tool registry, and the agent harness -- then runs the
orchestrator loop until SIGTERM/SIGINT triggers graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING
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
from surogates.sandbox.pool import SandboxPool
from surogates.sandbox.process import ProcessSandbox
from surogates.session.store import SessionStore
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from surogates.config import Settings

logger = logging.getLogger(__name__)


async def run_worker(settings: Settings) -> None:
    """Bootstrap all dependencies and run the orchestrator loop.

    1. Create async SQLAlchemy engine + session factory.
    2. Create Redis client.
    3. Create SessionStore.
    4. Create SandboxPool with ProcessSandbox backend.
    5. Create ToolRegistry.
    6. Define harness_factory that builds AgentHarness per session.
    7. Create Orchestrator.
    8. Handle SIGTERM/SIGINT for graceful shutdown.
    9. Run orchestrator.run().
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
    session_store = SessionStore(session_factory)

    # 4. Sandbox pool
    sandbox_backend = ProcessSandbox()
    sandbox_pool = SandboxPool(sandbox_backend)

    # 5. Tool registry (populated by loader modules during startup;
    #    the empty registry is usable out of the box).
    tool_registry = ToolRegistry()

    # 6. LLM client -- uses the OPENAI_API_KEY env var by default.
    #    Tenants may override the base_url via org_config in the future.
    llm_client = AsyncOpenAI()

    # Worker identity -- from K8s downward API or a generated fallback.
    worker_id = settings.worker_id or f"worker-{id(asyncio.get_event_loop()):x}"

    # 7. Harness factory -- creates a fully-wired AgentHarness for a given session.
    def harness_factory(session_id: UUID) -> AgentHarness:
        """Build an AgentHarness with all dependencies injected.

        Each invocation creates fresh per-session objects (budget, compressor,
        prompt builder) while sharing the long-lived singletons (store, LLM
        client, sandbox pool, tool registry).
        """
        # For Phase 1 we use a default tenant context.  In production the
        # orchestrator would resolve the tenant from the session's org_id
        # before constructing the harness.
        tenant = TenantContext(
            org_id=UUID("00000000-0000-0000-0000-000000000000"),
            user_id=UUID("00000000-0000-0000-0000-000000000000"),
            org_config={},
            user_preferences={},
            permissions=frozenset(),
            asset_root=settings.tenant_assets_root,
        )

        model_id = "gpt-4o"
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
            sandbox_pool=sandbox_pool,
            tenant=tenant,
            worker_id=worker_id,
            budget=budget,
            context_compressor=compressor,
            prompt_builder=prompt_builder,
            redis_client=redis_client,
            memory_manager=memory_manager,
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
        await redis_client.aclose()
        await engine.dispose()
        await llm_client.close()
        logger.info("Worker %s stopped", worker_id)
