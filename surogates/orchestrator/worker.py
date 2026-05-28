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
from surogates.harness.auxiliary_client import (
    build_advisor_auxiliary_llm,
    build_summary_auxiliary_llm,
    build_vision_auxiliary_llm,
)
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.model_metadata import get_model_info
from surogates.harness.prompt import PromptBuilder
from surogates.harness.prompt_library import default_library as default_prompt_library
from surogates.health import infrastructure_readiness, start_health_server
from surogates.browser.control import BrowserControlStore
from surogates.browser.pool import BrowserPool
from surogates.browser.process import ProcessBrowserBackend
from surogates.browser.registry import BrowserRegistry
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
    from surogates.config import BrowserSettings, Settings

logger = logging.getLogger(__name__)


#: Channels whose sessions have no human user and no service-account
#: principal.  The worker mints a ``channel_session`` JWT for these so
#: the harness can reach the API server via ``HarnessAPIClient`` — the
#: same path authenticated sessions take.  Without this, ``api_client``
#: is ``None`` and every api-client-gated feature silently degrades
#: (skills from the API pod's filesystem, ``create_artifact``, etc.).
#:
#: New channels added here MUST satisfy:
#:
#: * The channel creates sessions with both ``user_id`` and
#:   ``service_account_id`` ``NULL`` (the auth shape the JWT is
#:   designed for).
#: * The deployment authority — not a per-user principal — is the
#:   right grant for everything the harness will do.  Today this is
#:   the public-website widget; future per-product embeds fit the
#:   same model.
ANONYMOUS_CHANNELS: frozenset[str] = frozenset({"website"})


def _select_harness_token(
    *,
    tenant: TenantContext,
    session: Any,
    agent_id: str,
) -> str | None:
    """Mint the worker→API JWT appropriate to *session*'s principal.

    Three principal shapes produce three token types; an unrecognised
    shape produces ``None`` (the caller leaves
    ``harness_api_client = None`` for that session).

    * User principal → ``access`` token carrying the tenant's
      permissions (falling back to a sensible default when the
      caller did not pass any).
    * Service-account principal → ``service_account_session`` token
      scoped to the session id.
    * Channel principal (``session.channel`` in ``ANONYMOUS_CHANNELS``)
      → ``channel_session`` token carrying ``agent_id``, ``session_id``,
      and ``channel``.
    """
    from surogates.tenant.auth.jwt import (
        create_access_token,
        create_channel_session_token,
        create_service_account_session_token,
    )

    if tenant.user_id is not None:
        return create_access_token(
            org_id=tenant.org_id,
            user_id=tenant.user_id,
            permissions=set(tenant.permissions)
            or {
                "sessions:read",
                "sessions:write",
                "tools:read",
            },
        )
    if session.service_account_id is not None:
        return create_service_account_session_token(
            org_id=tenant.org_id,
            service_account_id=session.service_account_id,
            session_id=session.id,
        )
    if session.channel in ANONYMOUS_CHANNELS:
        return create_channel_session_token(
            org_id=tenant.org_id,
            agent_id=agent_id,
            session_id=session.id,
            channel=session.channel,
        )
    return None


def _filter_effective_tools(
    *,
    tools: set[str],
    tenant: TenantContext,
    session: Any,
    use_api_for_harness_tools: bool,
) -> set[str]:
    """Return the LLM-visible tool set after principal-aware filtering.

    Two rules layered on top of the caller's starting set:

    1. ``create_artifact`` requires the harness API client.  It stays
       only when the session WILL have one — that is, when
       ``use_api_for_harness_tools`` is enabled AND the session has a
       principal that :func:`_select_harness_token` will mint a token
       for.  Otherwise the LLM would call the tool and get the
       unhelpful "Artifacts require an API client" error.
    2. Anonymous-channel sessions never see ``memory`` or
       ``skill_manage``: the route gates refuse them anyway (see
       ``api/routes/memory.py`` and the mutating ``/v1/skills``
       handlers), and exposing the schemas would invite the LLM to
       try.  Belt + braces: route gate is the hard boundary, tool-set
       exclusion keeps the LLM from advertising capability it doesn't
       have.

    All other tools pass through unchanged.
    """
    result = set(tools)

    session_will_have_api_client = use_api_for_harness_tools and (
        tenant.user_id is not None
        or session.service_account_id is not None
        or session.channel in ANONYMOUS_CHANNELS
    )
    if not session_will_have_api_client:
        result.discard("create_artifact")

    session_is_anonymous_channel = (
        tenant.user_id is None
        and session.service_account_id is None
        and session.channel in ANONYMOUS_CHANNELS
    )
    if session_is_anonymous_channel:
        result.discard("memory")
        result.discard("skill_manage")

    # worker_block / worker_complete / worker_context are only meaningful
    # when this session is executing a subagent task (the dispatcher set
    # ``Session.task_id``). Plain chat and spawn_worker children never
    # have a task to operate on, so we strip them from the schema so
    # the LLM is not tempted to call them. The ``worker_*`` prefix is
    # deliberate: these are *self*-tools that act on the calling
    # worker's task row, NOT user-task-completion signals — earlier
    # ``task_*`` names confused LLMs into calling them at the end of
    # plain chat turns.
    if getattr(session, "task_id", None) is None:
        result.discard("worker_block")
        result.discard("worker_complete")
        result.discard("worker_context")

    return result


def _warn_if_base_model_missing_from_metadata(model_id: str) -> None:
    """Warn when the configured base model has no static metadata entry."""
    normalized = str(model_id or "").strip()
    if not normalized or get_model_info(normalized) is not None:
        return
    logger.warning(
        "Base LLM model %r is not present in surogates.harness.model_metadata "
        "MODEL_CATALOG or aliases; add model metadata so context sizing, "
        "capability checks, and cost estimates remain accurate.",
        normalized,
    )


def _build_browser_backend(
    settings: "BrowserSettings",
    *,
    storage_settings: Any = None,
) -> Any:
    """Build the configured browser backend without touching external services."""
    if settings.backend == "kubernetes":
        return _build_kubernetes_backend(settings, storage_settings=storage_settings)
    if settings.backend == "fleet":
        return _build_fleet_backend(settings, storage_settings=storage_settings)
    return ProcessBrowserBackend(
        image=settings.image,
        rest_port_base=settings.rest_port_base,
        cdp_port_base=settings.cdp_port_base,
        live_view_port_base=settings.live_view_port_base,
    )


def _build_kubernetes_backend(
    settings: "BrowserSettings",
    *,
    storage_settings: Any = None,
) -> Any:
    from surogates.browser.kubernetes import K8sBrowserBackend

    return K8sBrowserBackend(
        namespace=settings.k8s_namespace,
        service_account=settings.k8s_service_account,
        cluster_domain=settings.k8s_cluster_domain,
        pod_ready_timeout=settings.pod_ready_timeout,
        endpoint_probe_timeout=settings.endpoint_probe_timeout,
        image=settings.image,
        storage_settings=storage_settings,
        s3fs_image=settings.k8s_s3fs_image,
        s3_endpoint=settings.k8s_s3_endpoint,
    )


def _build_fleet_backend(
    settings: "BrowserSettings",
    *,
    storage_settings: Any = None,
) -> Any:
    """Construct a FleetBackend, optionally wrapped in a fallback composite."""
    import httpx

    from surogates.browser.composite import CompositeFallbackBackend
    from surogates.browser.fleet import FleetBackend

    if not settings.fleet_worker_token:
        raise RuntimeError(
            "browser.backend=fleet requires browser.fleet_worker_token "
            "(SUROGATES_BROWSER_FLEET_WORKER_TOKEN) — point it at the "
            "K8s Secret-mounted env var holding the worker bearer token",
        )

    http = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout=float(settings.fleet_timeout)),
    )
    primary = FleetBackend(
        endpoint=settings.fleet_endpoint,
        worker_token=settings.fleet_worker_token,
        http=http,
        timeout_seconds=float(settings.fleet_timeout),
        storage_settings=storage_settings,
    )
    if settings.fleet_fallback_backend == "none":
        return primary
    if settings.fleet_fallback_backend == "kubernetes":
        fallback = _build_kubernetes_backend(
            settings, storage_settings=storage_settings,
        )
    elif settings.fleet_fallback_backend == "process":
        fallback = ProcessBrowserBackend(
            image=settings.image,
            rest_port_base=settings.rest_port_base,
            cdp_port_base=settings.cdp_port_base,
            live_view_port_base=settings.live_view_port_base,
        )
    else:  # pragma: no cover — Literal restricts the surface
        raise ValueError(
            f"unknown browser.fleet_fallback_backend: {settings.fleet_fallback_backend}"
        )
    return CompositeFallbackBackend(primary=primary, fallback=fallback)


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
            OpsKnowledgeBase,
            agent_knowledge_bases,
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
            "disabled for this session",
            agent_id,
            exc_info=True,
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
                tenant,
                db_session=_db,
            )
    except Exception:
        logger.debug(
            "Failed to load sub-agent catalog for tenant %s",
            tenant.org_id,
            exc_info=True,
        )
        available_agents = []

    try:
        async with session_factory() as _db:
            available_skills = await resource_loader.load_skills(
                tenant,
                db_session=_db,
            )
    except Exception:
        logger.debug(
            "Failed to load skill catalog for tenant %s",
            tenant.org_id,
            exc_info=True,
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
    # asyncpg's prepared-statement cache must be off behind PgBouncer
    # transaction-mode pooling — cached plans are bound to backends that
    # get swapped between transactions. Harmless on a direct PG connection.
    db_connect_args: dict = {}
    if "asyncpg" in settings.db.url:
        db_connect_args = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        }
    engine = create_async_engine(
        settings.db.url,
        pool_size=settings.db.pool_size,
        max_overflow=settings.db.pool_overflow,
        connect_args=db_connect_args,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # 2. Redis
    redis_client = Redis.from_url(
        settings.redis.url,
        decode_responses=False,
    )

    # 3. Session store
    session_store = SessionStore(session_factory, redis=redis_client)

    # 3b. Workspace storage shared by the API, sandbox mounts, and
    # harness-local tools that need to materialize session files.
    from surogates.storage.backend import create_backend

    storage_backend = create_backend(settings)

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

    # 4b. Browser pool -- one browser per session, lazily provisioned.
    browser_backend = _build_browser_backend(
        settings.browser,
        storage_settings=settings.storage,
    )
    browser_registry = BrowserRegistry(redis_client)
    browser_control = BrowserControlStore(redis_client)

    async def _emit_browser_event(
        session_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        from surogates.session.events import EventType

        # Shield against cancellation. Browser provision/destroy callers
        # often emit at the tail of an awaitable that the harness can
        # cancel (session.pause). Without shield, a pause arriving while
        # we're mid-emit drops the event row even though the underlying
        # k8s/Redis state has already changed, leaving the SDK with a
        # stale view of the browser. emit_event itself is idempotent on
        # session counters via raw SQL, so shielding is safe.
        try:
            await asyncio.shield(
                session_store.emit_event(
                    UUID(session_id), EventType(event_type), data
                )
            )
        except asyncio.CancelledError:
            # The shielded task continues to completion; CancelledError
            # here means our caller was cancelled. Re-raise so the
            # cancellation propagates correctly.
            raise
        except Exception:
            logger.exception("Failed to emit browser event %s", event_type)

    browser_pool = BrowserPool(
        backend=browser_backend,
        registry=browser_registry,
        event_emitter=_emit_browser_event,
    )
    logger.info("Agent browser ready (backend=%s)", settings.browser.backend)

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
            for server_def in platform_loader._load_mcp_from_dir(
                settings.platform_mcp_dir
            ):
                mcp_servers[server_def.name] = {
                    "transport": server_def.transport,
                    "command": server_def.command,
                    "args": server_def.args,
                    "url": server_def.url,
                    "env": server_def.env,
                    "timeout": server_def.timeout,
                    # Forward HTTP headers (Authorization bearer etc.) so
                    # dev-mode connections to authenticated HTTP MCP
                    # endpoints don't 401. In proxy-mode these come from
                    # the credential vault; in dev-mode they are inlined
                    # in ``servers.json``.
                    "headers": server_def.headers,
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
    _warn_if_base_model_missing_from_metadata(settings.llm.model)

    # Worker identity -- from K8s downward API or a generated fallback.
    worker_id = settings.worker_id or f"worker-{id(asyncio.get_event_loop()):x}"
    settings.worker_id = worker_id

    # Worker bootstrap branches on runtime_mode.
    #
    # ``helm`` (legacy, default): each worker process serves exactly
    # one agent.  ``SUROGATES_ORG_ID`` and ``SUROGATES_AGENT_ID`` are
    # required and we resolve them up front; the harness factory below
    # closes over them.
    #
    # ``shared`` (Plan 1+): the worker pool serves any tenant.  Both
    # values are resolved per-session inside ``harness_factory`` from
    # the dequeued session's row (``session.org_id``,
    # ``session.agent_id``).  The startup guard is relaxed; the
    # per-session mismatch refusal lower down is also relaxed for
    # shared mode (every session is in scope).
    runtime_mode = getattr(settings, "runtime_mode", "helm")

    if runtime_mode == "helm":
        if not settings.org_id:
            raise RuntimeError(
                "SUROGATES_ORG_ID is not set. Each agent instance must belong to an org. "
                "Set org_id in config.yaml or SUROGATES_ORG_ID env var."
            )
        configured_org_id: UUID | None = UUID(settings.org_id)
        if not settings.agent_id:
            raise RuntimeError(
                "SUROGATES_AGENT_ID is not set. Each worker instance serves "
                "exactly one agent. Set agent_id in config.yaml or "
                "SUROGATES_AGENT_ID env var."
            )
        configured_agent_id: str | None = settings.agent_id
    else:
        # In shared mode we deliberately ignore any stale
        # SUROGATES_AGENT_ID / SUROGATES_ORG_ID set in the pod env so
        # a misconfigured rollout cannot silently route traffic to the
        # wrong tenant.  The harness factory below builds the tenant
        # context per session from the dequeued row instead.
        configured_org_id = None
        configured_agent_id = None

    # 7. Harness factory -- creates a fully-wired AgentHarness for a given session.
    async def harness_factory(session_id: UUID) -> AgentHarness:
        """Build an AgentHarness with all dependencies injected.

        Resolves the tenant from the session's user_id + the configured org_id.
        """
        # Load session to get user_id.
        session = await session_store.get_session(session_id)

        # In helm mode, refuse to process sessions that belong to a
        # different agent — defence-in-depth in case a foreign session
        # id leaks into this worker's queue.  In shared mode every
        # tenant is in scope by design; the per-agent queue keys still
        # guarantee an agent's sessions are not stolen by another.
        if (
            runtime_mode == "helm"
            and configured_agent_id is not None
            and session.agent_id != configured_agent_id
        ):
            raise RuntimeError(
                f"session {session_id} belongs to agent {session.agent_id!r}, "
                f"this worker serves agent {configured_agent_id!r}"
            )

        # Per-session tenant identity.  In helm mode we use the bootstrap-
        # validated configured_org_id; in shared mode we take org_id off
        # the session row.  Either way the harness sees a tenant context
        # bound to *this* session, never to process-wide state.
        session_org_id = (
            configured_org_id
            if runtime_mode == "helm"
            else UUID(str(session.org_id))
        )

        from sqlalchemy import select as sa_select
        from surogates.db.models import Org, User

        async with session_factory() as db:
            org_row = (
                await db.execute(sa_select(Org).where(Org.id == session_org_id))
            ).scalar_one_or_none()
            if session.user_id is not None:
                user_row = (
                    await db.execute(sa_select(User).where(User.id == session.user_id))
                ).scalar_one_or_none()
            else:
                user_row = None

        tenant = TenantContext(
            org_id=session_org_id,
            user_id=session.user_id,
            org_config=org_row.config if org_row else {},
            user_preferences=user_row.preferences if user_row else {},
            permissions=frozenset(),
            asset_root=f"{settings.tenant_assets_root}/{session_org_id}",
            service_account_id=session.service_account_id,
        )

        # Proxy-mode MCP tool discovery. The worker shares one tool
        # registry across sessions; in proxy mode MCP tools have to be
        # discovered through the MCP proxy with a session-scoped
        # sandbox JWT (the proxy validates that token to scope server
        # access + credential resolution). The McpProxyClient caches
        # already-registered tools, so repeat calls within a worker's
        # lifetime are cheap. Falls back silently if the proxy is
        # unreachable — built-in tools still work; only the
        # platform/copilot MCP tools go missing in that case.
        if mcp_proxy_client is not None:
            try:
                principal_user_id = session.user_id or session.service_account_id
                if principal_user_id is not None:
                    await mcp_proxy_client.discover_and_register(
                        org_id=configured_org_id,
                        user_id=principal_user_id,
                        session_id=session.id,
                        is_service_account=session.user_id is None,
                    )
            except Exception:
                logger.warning(
                    "MCP proxy tool discovery failed for session %s; "
                    "built-in tools still available",
                    session.id, exc_info=True,
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
        summary_auxiliary = build_summary_auxiliary_llm(settings, tenant)
        vision_auxiliary = build_vision_auxiliary_llm(settings, tenant)
        advisor_auxiliary = build_advisor_auxiliary_llm(settings, tenant)
        compressor = ContextCompressor(
            model_id,
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            model_overrides=settings.llm.models,
            summary_model_override=(
                summary_auxiliary.model if summary_auxiliary is not None else None
            ),
            summary_client=(
                summary_auxiliary.client if summary_auxiliary is not None else None
            ),
        )

        # User-scoped memory dir for interactive sessions, org-shared
        # memory dir for service-account sessions (no per-user context
        # to carry forward).
        from pathlib import Path

        if session.user_id is not None:
            memory_dir = (
                Path(tenant.asset_root) / "users" / str(session.user_id) / "memory"
            )
        else:
            memory_dir = Path(tenant.asset_root) / "shared" / "memory"
        memory_store = MemoryStore(memory_dir=memory_dir)
        memory_manager = MemoryManager(memory_store)

        # Load prompt-visible catalogs.  Expert skill definitions may still
        # exist in storage, but PromptBuilder hides them from executor prompts
        # now that strategic advice is handled by the harness advisor.
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
        # Principal-aware tool-set filtering: ``create_artifact``
        # requires the harness API client (so the session must have a
        # principal AND ``use_api_for_harness_tools`` must be on), and
        # anonymous-channel sessions never see ``memory`` /
        # ``skill_manage`` (the routes refuse them, and exposing the
        # schemas would invite the LLM to try).  ``_filter_effective_tools``
        # documents the rules.
        effective_tools = _filter_effective_tools(
            tools=effective_tools,
            tenant=tenant,
            session=session,
            use_api_for_harness_tools=settings.worker.use_api_for_harness_tools,
        )

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

        # User / SA / channel-session principals each map to a JWT
        # type via :func:`_select_harness_token`.  A session whose
        # channel is not in ``ANONYMOUS_CHANNELS`` and which lacks a
        # user/SA principal yields ``None`` — the harness then runs
        # with ``api_client=None``, the legacy degraded path.
        harness_api_client = None
        if settings.worker.use_api_for_harness_tools:
            from surogates.harness.api_client import HarnessAPIClient

            token = _select_harness_token(
                tenant=tenant,
                session=session,
                agent_id=settings.agent_id,
            )
            if token is None:
                logger.info(
                    "session %s has no recognised principal "
                    "(channel=%r); harness will run without API client",
                    session.id, session.channel,
                )
            else:
                harness_api_client = HarnessAPIClient(
                    base_url=settings.worker.api_base_url,
                    token=token,
                    session_id=str(session.id),
                )

        # Build a TurnSummarizer when both the summary auxiliary LLM is
        # configured AND the kill-switch is on. Reuses the existing
        # cheap-model client constructed for context compression so we
        # avoid spinning up an extra AsyncOpenAI instance per turn.
        from surogates.harness.turn_summarizer import TurnSummarizer

        if (
            settings.worker.emit_turn_summaries
            and summary_auxiliary is not None
        ):
            turn_summarizer: TurnSummarizer | None = TurnSummarizer(
                summary_client=summary_auxiliary.client,
                summary_model=summary_auxiliary.model,
            )
        else:
            turn_summarizer = None

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
            browser_pool=browser_pool,
            browser_control=browser_control,
            storage=storage_backend,
            api_client=harness_api_client,
            default_model=model_id,
            session_factory=session_factory,
            saga_enabled=settings.saga.enabled,
            saga_settings=settings.saga if settings.saga.enabled else None,
            log_policy_allowed=settings.governance.log_allowed,
            vision_client=(
                vision_auxiliary.client if vision_auxiliary is not None else None
            ),
            vision_model=(
                vision_auxiliary.model if vision_auxiliary is not None else ""
            ),
            advisor_client=(
                advisor_auxiliary.client if advisor_auxiliary is not None else None
            ),
            advisor_model=(
                advisor_auxiliary.model if advisor_auxiliary is not None else ""
            ),
            advisor_max_calls_per_turn=settings.llm.advisor_max_calls_per_turn,
            advisor_max_tokens=settings.llm.advisor_max_tokens,
            turn_summarizer=turn_summarizer,
        )

    # 8. Orchestrator — consumes from this agent's dedicated work queue.
    from surogates.config import agent_queue_key

    queue_key = agent_queue_key(configured_agent_id)

    # Build the tenant-for-task callable used by ``tasks_tick`` to spawn
    # child sessions on behalf of subagent tasks. The tick runs as a
    # system actor (no specific user), so user_id stays None and the
    # other tenant fields use minimal defaults — the spawn path reads
    # only ``org_id`` (for AgentDef catalog scoping).
    from surogates.tenant.context import TenantContext

    def _tenant_for_task(task: Any) -> TenantContext:
        return TenantContext(
            org_id=task.org_id,
            user_id=None,
            org_config={},
            user_preferences={},
            permissions=frozenset(),
            asset_root="",
        )

    orchestrator = Orchestrator(
        redis_client=redis_client,
        session_store=session_store,
        harness_factory=harness_factory,
        max_concurrent=settings.worker.concurrency,
        agent_id=configured_agent_id,
        queue_key=queue_key,
        poll_timeout=settings.worker.poll_timeout,
        browser_pool=browser_pool,
        session_factory=session_factory,
        tenant_for_task=_tenant_for_task,
    )

    scheduled_runner = None
    scheduled_task = None
    if settings.scheduled_sessions.enabled:
        from surogates.scheduled.runner import ScheduledSessionRunner
        from surogates.scheduled.store import ScheduledSessionStore

        scheduled_runner = ScheduledSessionRunner(
            settings=settings,
            session_factory=session_factory,
            session_store=session_store,
            scheduled_store=ScheduledSessionStore(session_factory),
            redis=redis_client,
            storage=storage_backend,
        )
        scheduled_task = asyncio.create_task(
            scheduled_runner.run_forever(),
            name="scheduled-session-runner",
        )

    from surogates.jobs.inbox_expire import run_expire_loop
    inbox_expire_task = asyncio.create_task(
        run_expire_loop(session_store),
        name="inbox-expire-sweeper",
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
        if scheduled_runner is not None:
            await scheduled_runner.shutdown()
        if scheduled_task is not None:
            scheduled_task.cancel()
            try:
                await scheduled_task
            except asyncio.CancelledError:
                pass
        inbox_expire_task.cancel()
        try:
            await inbox_expire_task
        except asyncio.CancelledError:
            pass
        await health_server.stop()
        await browser_pool.destroy_all()
        await sandbox_pool.destroy_all()
        mcp_proxy.shutdown_all()
        if mcp_proxy_client is not None:
            await mcp_proxy_client.close()
        await redis_client.aclose()
        await engine.dispose()
        await llm_client.close()
        logger.info("Worker %s stopped", worker_id)
