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
from surogates.tenant.credentials import CredentialVault
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
    bundle: Any | None = None,
) -> tuple[list[Any], list[Any]]:
    """Load prompt-visible sub-agent and skill catalogs for a tenant.

    Plan 3 / Task 15.  When ``bundle`` is provided, layer 1
    (platform skills + sub-agents) reads from the bundle instead of
    the on-disk ``/etc/surogates/{skills,agents}/`` paths.  Layers
    2-4 (user files + DB) are unchanged.
    """
    resource_loader = ResourceLoader(
        platform_skills_dir=getattr(settings, "platform_skills_dir", None),
        platform_agents_dir=getattr(settings, "platform_agents_dir", None),
    )

    try:
        async with session_factory() as _db:
            available_agents = await resource_loader.load_agents(
                tenant,
                db_session=_db,
                bundle=bundle,
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
                bundle=bundle,
            )
    except Exception:
        logger.debug(
            "Failed to load skill catalog for tenant %s",
            tenant.org_id,
            exc_info=True,
        )
        available_skills = []

    return available_agents, available_skills


def _install_worker_runtime_plumbing(state: dict, settings) -> None:
    """Wire PlatformClient + RuntimeConfigCache + FileBundleCache
    onto a worker state dict.

    Plan 2 / Task 1 + Plan 3 / Task 9.  Mirrors
    :func:`surogates.api.app._install_shared_runtime_plumbing` but
    keyed on a plain dict (workers don't have a FastAPI app.state).
    ``state['platform_client']``, ``state['runtime_config_cache']``,
    and ``state['file_bundle_cache']`` are set on success; all stay
    ``None`` when ``runtime_mode != 'shared'`` OR
    ``platform_api_url`` is empty (the file_bundle_cache also stays
    None when ``settings.hub.endpoint`` is empty or the Hub SDK is
    not installed).
    """
    from surogates.api.app import (
        _maybe_build_file_bundle_cache,
        _maybe_build_memory_cache,
    )
    from surogates.runtime import PlatformClient, RuntimeConfigCache

    if getattr(settings, "runtime_mode", "helm") != "shared":
        state["platform_client"] = None
        state["runtime_config_cache"] = None
        state["file_bundle_cache"] = None
        state["memory_cache"] = None
        return

    if not settings.platform_api_url:
        logger.error(
            "runtime_mode='shared' but SUROGATES_PLATFORM_API_URL is empty; "
            "worker harness_factory will fail on every session",
        )
        state["platform_client"] = None
        state["runtime_config_cache"] = None
        state["file_bundle_cache"] = None
        state["memory_cache"] = None
        return

    client = PlatformClient(
        base_url=settings.platform_api_url,
        token=settings.platform_api_token,
    )
    runtime_config_cache = RuntimeConfigCache(
        loader=client.get_runtime_config, ttl_seconds=1.0,
    )
    state["platform_client"] = client
    state["runtime_config_cache"] = runtime_config_cache
    state["file_bundle_cache"] = _maybe_build_file_bundle_cache(
        settings=settings, runtime_config_cache=runtime_config_cache,
    )
    # Plan 4 / Task 6 — per-user memory cache.  Reads
    # state['storage_backend'] when set (caller wires this before
    # _install_worker_runtime_plumbing).  Stays None when storage
    # is unconfigured; the harness falls back to disk MemoryStore.
    state["memory_cache"] = _maybe_build_memory_cache(
        settings=settings,
        storage_backend=state.get("storage_backend"),
    )


async def _build_helm_session_llm_clients(settings, tenant) -> "SessionLLMClients":
    """Helm-mode adapter: build a SessionLLMClients from process-wide
    settings.llm + tenant overrides.

    Plan 2 / Task 7 transitional helper.  Wraps the legacy
    auxiliary-client builders so harness_factory uses a unified
    SessionLLMClients shape in both modes.  Plan 9 retires helm mode
    entirely and deletes this helper along with the auxiliary builders.

    The main slot uses process-wide settings.llm.{api_key,base_url,model}
    directly — there's no per-session vault resolution in helm mode
    because the legacy contract was a single key per pod.

    Async so it can ``await main_client.close()`` on a partial-build
    failure — without that, a flaky auxiliary builder would leak the
    main client's connection pool per failed session start.
    """
    from surogates.harness.auxiliary_client import (
        build_advisor_auxiliary_llm,
        build_summary_auxiliary_llm,
        build_vision_auxiliary_llm,
    )
    from surogates.harness.session_llm import ResolvedLLM, SessionLLMClients

    llm_kwargs: dict[str, Any] = {}
    if settings.llm.api_key:
        llm_kwargs["api_key"] = settings.llm.api_key
    if settings.llm.base_url:
        llm_kwargs["base_url"] = settings.llm.base_url

    main_client = AsyncOpenAI(**llm_kwargs)
    try:
        summary = build_summary_auxiliary_llm(settings, tenant)
        vision = build_vision_auxiliary_llm(settings, tenant)
        advisor = build_advisor_auxiliary_llm(settings, tenant)
    except BaseException:
        try:
            await main_client.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning(
                "Failed to aclose helm main AsyncOpenAI during partial-"
                "build cleanup; original error being re-raised",
                exc_info=True,
            )
        raise

    return SessionLLMClients(
        main=ResolvedLLM(client=main_client, model=settings.llm.model),
        summary=(
            ResolvedLLM(client=summary.client, model=summary.model)
            if summary is not None
            else None
        ),
        vision=(
            ResolvedLLM(client=vision.client, model=vision.model)
            if vision is not None
            else None
        ),
        advisor=(
            ResolvedLLM(client=advisor.client, model=advisor.model)
            if advisor is not None
            else None
        ),
    )


async def _shutdown_worker_runtime_plumbing(state: dict) -> None:
    """Close the worker-side platform client and drop cache references.

    Idempotent — calling twice is a no-op.
    """
    client = state.get("platform_client")
    if client is not None:
        await client.aclose()
    state["platform_client"] = None
    state["runtime_config_cache"] = None
    state["file_bundle_cache"] = None
    state["memory_cache"] = None


def _start_worker_invalidator(state: dict) -> None:
    """Start the Redis pub/sub listener that invalidates the worker's
    RuntimeConfigCache when surogate-ops publishes a change.

    Plan 2 / Task 2.  No-op when the cache wasn't wired (helm mode or
    empty platform url).  The worker only routes runtime-config +
    bundle changes; firebase / slug invalidations are api-side only.
    """
    import asyncio

    cache = state.get("runtime_config_cache")
    redis_client = state.get("redis")
    if cache is None or redis_client is None:
        state["runtime_invalidator_task"] = None
        return

    from surogates.runtime import run_invalidator

    state["runtime_invalidator_task"] = asyncio.create_task(
        run_invalidator(
            redis_client,
            runtime_config_cache=cache,
            file_bundle_cache=state.get("file_bundle_cache"),
            memory_cache=state.get("memory_cache"),
        ),
        name="surogates-worker-runtime-invalidator",
    )


async def _stop_worker_invalidator(state: dict) -> None:
    task = state.get("runtime_invalidator_task")
    if task is None:
        return
    task.cancel()
    try:
        await task
    except BaseException:  # noqa: BLE001 — cancellation expected
        pass
    state["runtime_invalidator_task"] = None


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

    # 6. LLM clients -- now per-session (Plan 2 / Task 7).  Helm-mode
    # sessions get a bundle from _build_helm_session_llm_clients in
    # harness_factory (wraps settings.llm + tenant overrides); shared-
    # mode sessions get a bundle from build_session_llm_clients fed by
    # the per-agent runtime config + the credential vault.  No
    # process-wide AsyncOpenAI instance here — every session owns its
    # own connection pool.
    logger.info(
        "LLM defaults: model=%s, base_url=%s, api_key=%s",
        settings.llm.model,
        settings.llm.base_url or "(default)",
        f"{settings.llm.api_key[:12]}..." if settings.llm.api_key else "(not set)",
    )
    _warn_if_base_model_missing_from_metadata(settings.llm.model)

    # Worker-side shared-runtime plumbing (Plan 2 / Tasks 1+2).  In
    # shared mode this gives harness_factory a RuntimeConfigCache to
    # pull AgentRuntimeContext from per session; in helm mode it's a
    # no-op and ``runtime_config_cache`` stays ``None``.
    worker_state: dict = {"redis": redis_client}
    _install_worker_runtime_plumbing(worker_state, settings)
    _start_worker_invalidator(worker_state)
    runtime_config_cache = worker_state["runtime_config_cache"]

    # Credential vault — required in shared mode for per-session
    # vault://<name> resolution; tolerated as None in helm mode where
    # settings.llm.api_key is the single key for the pod.
    if settings.encryption_key:
        credential_vault: CredentialVault | None = CredentialVault(
            session_factory,
            encryption_key=settings.encryption_key.encode("utf-8"),
        )
    else:
        credential_vault = None
        logger.warning(
            "SUROGATES_ENCRYPTION_KEY not set; shared-mode sessions "
            "will fail to resolve api_key_ref through the vault",
        )

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

        # Plan 2 / Task 9 — resolve AgentRuntimeContext early so its
        # storage_key_prefix can feed TenantContext.asset_root.  Helm
        # mode goes through _legacy_helm_context (Task 9 enhances it
        # to populate the prefix from settings.tenant_assets_root +
        # org_id); shared mode hits the worker-side cache.
        from surogates.runtime import resolve_runtime_context_for_session

        ctx = await resolve_runtime_context_for_session(
            session,
            cache=runtime_config_cache,
            settings=settings,
        )

        tenant = TenantContext(
            org_id=session_org_id,
            user_id=session.user_id,
            org_config=org_row.config if org_row else {},
            user_preferences=user_row.preferences if user_row else {},
            permissions=frozenset(),
            asset_root=ctx.storage_key_prefix,
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

        # Plan 2 / Task 7 — per-session LLM bundle.  Helm mode wraps
        # the legacy settings + auxiliary-builder path; shared mode
        # resolves through the AgentRuntimeContext (already resolved
        # above for asset_root / Task 9) + the credential vault.
        if runtime_mode == "helm":
            llm_bundle = await _build_helm_session_llm_clients(
                settings, tenant,
            )
        else:
            from surogates.harness.session_llm import (
                build_session_llm_clients,
            )

            llm_bundle = await build_session_llm_clients(
                ctx, vault=credential_vault, user_id=tenant.user_id,
            )

        if not llm_bundle.main.model:
            # Worker raises rather than returns a 503 because there's no
            # HTTP response surface here.  ``dispatcher._process``
            # catches and emits ``HARNESS_CRASH`` events; after 3 retries
            # with exponential backoff the dispatcher promotes the
            # session to ``SESSION_FAIL``.  A deployment that
            # legitimately leaves the model blank therefore burns
            # ~7s + log noise per session — operators should fix the
            # config rather than rely on the retry loop.
            await llm_bundle.aclose()
            raise RuntimeError(
                f"LLM model is not configured (main slot has empty model); "
                f"cannot wake session {session.id}."
            )
        model_id = llm_bundle.main.model
        llm_client = llm_bundle.main.client
        budget = IterationBudget(max_total=90)
        summary_slot = llm_bundle.summary
        vision_slot = llm_bundle.vision
        advisor_slot = llm_bundle.advisor
        compressor = ContextCompressor(
            model_id,
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            model_overrides=settings.llm.models,
            summary_model_override=(
                summary_slot.model if summary_slot is not None else None
            ),
            summary_client=(
                summary_slot.client if summary_slot is not None else None
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

        # Plan 4 / Task 11 — shared-mode memory is R2-backed.
        # Helm mode keeps the legacy disk-based MemoryStore until
        # Plan 9 cutover.  Branch on (runtime_mode == 'shared') AND
        # memory_cache wired so a misconfigured shared pod (no
        # storage backend) falls back to disk silently.
        memory_cache = worker_state.get("memory_cache")
        if runtime_mode == "shared" and memory_cache is not None:
            from surogates.memory.r2_store import R2MemoryStore
            from surogates.runtime.memory_protocol import memory_object_key

            mem_bucket = (
                settings.storage.memory_bucket or settings.storage.bucket
            )
            mem_key = memory_object_key(
                storage_key_prefix=ctx.storage_key_prefix,
                user_id=(
                    str(session.user_id) if session.user_id else None
                ),
            )
            memory_store = R2MemoryStore(
                backend=storage_backend, bucket=mem_bucket, key=mem_key,
            )
            await memory_store.load_from_r2()
        else:
            memory_store = MemoryStore(memory_dir=memory_dir)
        memory_manager = MemoryManager(memory_store)

        # Plan 3 / Task 12+15 — resolve the per-session file bundle
        # once and share it across the catalog load (Task 15) and the
        # PromptBuilder content pre-load (Task 12).  None when the
        # FileBundleCache isn't wired or the agent has no bundle
        # configured; both downstream consumers fall back to the
        # legacy filesystem paths silently in that case.
        bundle = None
        if file_bundle_cache is not None and ctx.bundle_hub_ref:
            try:
                bundle = await file_bundle_cache.get(session.agent_id)
            except LookupError:
                bundle = None

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
            bundle=bundle,
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

        # Plan 3 / Task 12 — pre-load SOUL.md / AGENT.md from the
        # bundle resolved above (shared with the Task 15 catalog
        # load).  The PromptBuilder stays sync; the loaders return
        # None silently when bundle is None and the builder falls
        # back to load_soul_md_from_disk (legacy helm path).
        from surogates.harness.context_files import (
            load_agent_md, load_soul_md,
        )
        soul_md_content = await load_soul_md(bundle)
        agent_md_content = await load_agent_md(bundle)

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
            soul_md_content=soul_md_content,
            agent_md_content=agent_md_content,
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
            and summary_slot is not None
        ):
            turn_summarizer: TurnSummarizer | None = TurnSummarizer(
                summary_client=summary_slot.client,
                summary_model=summary_slot.model,
            )
        else:
            turn_summarizer = None

        harness = AgentHarness(
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
                vision_slot.client if vision_slot is not None else None
            ),
            vision_model=(
                vision_slot.model if vision_slot is not None else ""
            ),
            advisor_client=(
                advisor_slot.client if advisor_slot is not None else None
            ),
            advisor_model=(
                advisor_slot.model if advisor_slot is not None else ""
            ),
            advisor_max_calls_per_turn=settings.llm.advisor_max_calls_per_turn,
            advisor_max_tokens=settings.llm.advisor_max_tokens,
            turn_summarizer=turn_summarizer,
        )
        # Plan 2 / Task 7 — stash the bundle so the dispatcher can
        # aclose its four connection pools at session retirement.
        # A long-running worker would otherwise accumulate one pool
        # per processed session.
        harness._session_llm_bundle = llm_bundle  # type: ignore[attr-defined]
        return harness

    # 8. Orchestrator — consumes from the shared work queue
    # (Plan 2 / Task 14).  Per-tenant isolation is enforced by the
    # TurnConcurrencyGate the dispatcher consults on every dequeue.
    from surogates.config import SHARED_WORK_QUEUE_KEY
    from surogates.runtime import TurnConcurrencyGate

    turn_gate = TurnConcurrencyGate(
        redis_client,
        default_max=getattr(
            settings.worker, "max_concurrent_turns_default", 10,
        ),
    )

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
        queue_key=SHARED_WORK_QUEUE_KEY,
        poll_timeout=settings.worker.poll_timeout,
        browser_pool=browser_pool,
        session_factory=session_factory,
        tenant_for_task=_tenant_for_task,
        turn_gate=turn_gate,
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
        # Plan 2 / Tasks 1+2 — drain the worker-side runtime cache +
        # invalidator before tearing down the redis client.
        await _stop_worker_invalidator(worker_state)
        await _shutdown_worker_runtime_plumbing(worker_state)
        await redis_client.aclose()
        await engine.dispose()
        logger.info("Worker %s stopped", worker_id)
