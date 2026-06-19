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
from surogates.harness.model_metadata import get_model_info
from surogates.harness.prompt import PromptBuilder
from surogates.harness.prompt_library import default_library as default_prompt_library
from surogates.health import infrastructure_readiness, start_health_server
from surogates.browser.control import BrowserControlStore
from surogates.browser.pool import BrowserPool
from surogates.browser.profiles import BrowserProfileStore
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
    else:
        # Task workers always get their self-tools, even under a
        # restrictive AgentDef allowlist (e.g. a specialist whose ``tools``
        # list is just its domain tools).  ``worker_complete`` /
        # ``worker_context`` / ``worker_block`` are execution-context
        # self-tools, not work tools subject to the allowlist — without
        # them a task worker can't hand off a structured result or read
        # its parents' output.
        result.update({"worker_block", "worker_complete", "worker_context"})

    # share_note / read_board / expand_note are coordination self-tools,
    # meaningful only inside a coordination group (the spawn paths stamp
    # ``context_group_id`` on every fan-out member; see
    # docs/superpowers/specs/2026-06-11-coordination-board-design.md).
    # Same idiom as the worker_* self-tools above: stripped for solo
    # sessions, force-added for members even under a restrictive AgentDef
    # allowlist.
    if not (getattr(session, "config", None) or {}).get("context_group_id"):
        result.discard("share_note")
        result.discard("read_board")
        result.discard("expand_note")
    else:
        result.update({"share_note", "read_board", "expand_note"})

    # idea_tree / dispatch_experiments / merge_experiment are the research
    # coordinator's deterministic spine. They are present only while a
    # research run is active AND only on the coordinator session (never a
    # task worker): executors stay tree-blind so they cannot invent a
    # second shared-state protocol (mle_kaggle.yaml "no second shared-state
    # protocol between executors"). Unlike the self-tools above they are
    # never force-added — they ride the registry's full set on the
    # coordinator and are stripped everywhere else.
    research_config = getattr(session, "config", None) or {}
    is_research_coordinator = (
        bool(research_config.get("active_research_run_id"))
        and getattr(session, "task_id", None) is None
    )
    if not is_research_coordinator:
        result.discard("idea_tree")
        result.discard("dispatch_experiments")
        result.discard("merge_experiment")

    return result


def _warn_if_base_model_missing_from_metadata(model_id: str) -> None:
    """Warn when the configured base model has no static metadata entry."""
    normalized = str(model_id or "").strip()
    if not normalized or get_model_info(normalized) is not None:
        return
    logger.warning(
        "Base LLM model %r is not present in surogates.harness.model_metadata "
        "MODEL_CATALOG or aliases; add model metadata so context sizing and capability checks remain accurate.",
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
    ``id``, ``name``, ``display_name``, ``description``, ``mode``
    (grounding|reference), ``pages_tree`` (pre-rendered markdown ToC)
    and ``pages_total``. Keeping the surface plain dict (not a
    SQLAlchemy row) lets us cache it and pass it across async
    boundaries without dragging the session.
    """
    # ToC cap: protects the prompt from a pathological KB. The cut is
    # announced in the tree so the LLM knows the listing is partial.
    max_tree_pages = 200

    if not ops_db_url:
        return []
    try:
        from surogates.db.ops_engine import get_ops_session_factory
        from surogates.db.ops_models import (
            OpsKBWikiPage,
            OpsKnowledgeBase,
            agent_knowledge_bases,
        )
        from surogates.tools.builtin.kb_tools import _format_pages_tree
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
                    agent_knowledge_bases.c.mode,
                )
                .join(
                    agent_knowledge_bases,
                    agent_knowledge_bases.c.kb_id == OpsKnowledgeBase.id,
                )
                .where(agent_knowledge_bases.c.agent_id == agent_id)
                .order_by(OpsKnowledgeBase.name.asc())
            )
            kbs = [
                {
                    "id": row[0],
                    "name": row[1],
                    "display_name": row[2],
                    "description": row[3],
                    "mode": row[4] or "grounding",
                }
                for row in result.all()
            ]
            if not kbs:
                return []

            # One round-trip for every attached KB's page list. The
            # page tree makes the KB's contents visible in the system
            # prompt so the agent can judge relevance instead of being
            # blind to what the KB covers.
            kb_ids = [kb["id"] for kb in kbs]
            pages_result = await session.execute(
                sa.select(OpsKBWikiPage)
                .where(OpsKBWikiPage.kb_id.in_(kb_ids))
                .order_by(OpsKBWikiPage.path.asc())
            )
            pages_by_kb: dict[str, list] = {kb_id: [] for kb_id in kb_ids}
            for page in pages_result.scalars().all():
                pages_by_kb[page.kb_id].append(page)

        for kb in kbs:
            pages = pages_by_kb.get(kb["id"], [])
            kb["pages_total"] = len(pages)
            tree = _format_pages_tree(pages[:max_tree_pages])
            if len(pages) > max_tree_pages:
                tree += (
                    f"\n(showing {max_tree_pages} of {len(pages)} pages"
                    f" -- use kb_list_pages for the full listing)"
                )
            kb["pages_tree"] = tree
        return kbs
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
    tenant: TenantContext,
    session_factory: Any,
    bundle: Any | None = None,
    system_bundle: Any | None = None,
) -> tuple[list[Any], list[Any]]:
    """Load prompt-visible sub-agent and skill catalogs for a tenant.

    Skill catalog Layer 1 is the merge of the shared
    ``platform/system-skills`` bundle and the per-agent bundle; layers
    2-4 are user files + org/user DB rows.  Sub-agents do not have a
    system-bundle layer today (no operational pressure to share them),
    so only ``bundle`` is threaded through ``load_agents``.
    """
    resource_loader = ResourceLoader()

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
                system_bundle=system_bundle,
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

    Mirrors :func:`surogates.api.app._install_shared_runtime_plumbing`
    but keyed on a plain dict (workers don't have a FastAPI
    app.state).  ``settings.platform_api_url`` is required; missing
    it makes ``harness_factory`` fail on the first session.
    """
    from surogates.api.app import (
        build_file_bundle_cache,
        build_memory_cache,
        build_system_bundle_cache,
    )
    from surogates.runtime import PlatformClient, RuntimeConfigCache

    if not settings.platform_api_url:
        raise RuntimeError(
            "SUROGATES_PLATFORM_API_URL is required; the worker cannot "
            "resolve per-tenant config without it",
        )

    client = PlatformClient(
        base_url=settings.platform_api_url,
        token=settings.platform_api_token,
    )
    runtime_config_cache = RuntimeConfigCache(
        loader=client.get_runtime_config, ttl_seconds=1.0,
    )
    state["platform_client"] = client
    state["runtime_config_cache"] = runtime_config_cache
    state["file_bundle_cache"] = build_file_bundle_cache(
        settings=settings, runtime_config_cache=runtime_config_cache,
    )
    state["memory_cache"] = build_memory_cache(
        settings=settings,
        storage_backend=state.get("storage_backend"),
    )
    # Shared system-skills bundle.  One snapshot per worker process,
    # invalidated by the Redis ``system_skills_changed:`` channel
    # routed in ``_start_worker_invalidator``.
    state["system_bundle_cache"] = build_system_bundle_cache(
        settings=settings,
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
    state["system_bundle_cache"] = None


def _start_worker_invalidator(state: dict) -> None:
    """Start the Redis pub/sub listener that invalidates the worker's
    RuntimeConfigCache when surogate-ops publishes a change.

    The worker only routes runtime-config + bundle changes; firebase
    / slug invalidations are api-side only.
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
            system_bundle_cache=state.get("system_bundle_cache"),
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

    # Audit store for memory write / conflict
    # events (and any other worker-side audit emit added later).
    from surogates.audit.store import AuditStore
    audit_store = AuditStore(session_factory)

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
            executor_port=settings.sandbox.k8s_executor_port,
            storage_settings=settings.storage,
            s3fs_image=settings.sandbox.k8s_s3fs_image,
            s3_endpoint=settings.sandbox.k8s_s3_endpoint,
            mcp_proxy_url=settings.mcp_proxy_url,
        )
    elif settings.sandbox.backend == "docker":
        from surogates.sandbox.docker import DockerSandbox

        sandbox_backend = DockerSandbox(
            image=settings.sandbox.docker_image,
            executor_port_base=settings.sandbox.docker_executor_port_base,
            ready_timeout=settings.sandbox.docker_ready_timeout,
            network=settings.sandbox.docker_network,
            mcp_proxy_url=settings.mcp_proxy_url,
            storage_settings=settings.storage,
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

    # Use-time browser-minutes gate. Reads the ops ``credit_balances``
    # row before provisioning a new browser pod and refuses when the
    # project is out of minutes. It self-disables when the ops DB is
    # not configured (see ``assert_browser_minutes_available``), so it
    # is safe to wire unconditionally — the ops engine is initialized
    # just below for the KB tools and resolved lazily by the guard.
    from surogates.db.ops_credits import assert_browser_minutes_available

    browser_pool = BrowserPool(
        backend=browser_backend,
        registry=browser_registry,
        event_emitter=_emit_browser_event,
        credit_guard=assert_browser_minutes_available,
        browser_profile_store=(
            BrowserProfileStore(
                session_factory,
                encryption_key=settings.encryption_key.encode("utf-8"),
            )
            if settings.encryption_key
            else None
        ),
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

    # 5b. MCP tools — the worker delegates to the MCP proxy service,
    # which reads the ``mcp_servers`` table per request and connects on
    # behalf of the tenant.  Tool discovery happens at session start
    # (per-tenant), not here.  ``mcp_proxy_url`` is required; missing it
    # is a deployment misconfiguration and we surface that loudly
    # rather than silently registering zero MCP tools.
    if not settings.mcp_proxy_url:
        raise RuntimeError(
            "mcp_proxy_url is required; without it the worker registers "
            "no MCP tools and every session sees only built-in harness "
            "tools.  Set ``mcp_proxy_url`` in the worker config (see "
            "k8s/surogates-runtime/production/30-runtime-configmap.yaml).",
        )
    from surogates.orchestrator.mcp_client import McpProxyClient

    mcp_proxy_client = McpProxyClient(
        base_url=settings.mcp_proxy_url,
        registry=tool_registry,
    )
    logger.info("MCP tools via proxy: %s", settings.mcp_proxy_url)

    # 6. LLM clients are per-session.  Every session gets a fresh
    # SessionLLMClients bundle built by ``build_session_llm_clients``
    # from the per-agent runtime config + the credential vault.  No
    # process-wide AsyncOpenAI instance here — every session owns
    # its own connection pool.
    _warn_if_base_model_missing_from_metadata(settings.llm.model)

    # Worker-side shared-runtime plumbing.  Wires the
    # RuntimeConfigCache + FileBundleCache + MemoryCache that
    # harness_factory reads per session.
    #
    # ``storage_backend`` MUST be on ``worker_state`` before
    # ``_install_worker_runtime_plumbing`` runs, because
    # ``build_memory_cache`` reads ``state.get("storage_backend")``
    # at construction time and silently returns ``None`` when the
    # backend is missing — which makes every harness_factory call
    # fall through to the disk-backed ``MemoryStore`` and fail with
    # ``Permission denied`` on containers without a writable working
    # directory (Plan 4 R2-memory path requires this wiring).
    worker_state: dict = {
        "redis": redis_client,
        "storage_backend": storage_backend,
    }
    _install_worker_runtime_plumbing(worker_state, settings)
    _start_worker_invalidator(worker_state)
    runtime_config_cache = worker_state["runtime_config_cache"]

    # Credential vault — required for per-session ``vault://<name>``
    # resolution.  Pod cannot serve sessions without it.
    if not settings.encryption_key:
        raise RuntimeError(
            "SUROGATES_ENCRYPTION_KEY is required to resolve per-"
            "session api_key_ref through the credential vault",
        )
    credential_vault = CredentialVault(
        session_factory,
        encryption_key=settings.encryption_key.encode("utf-8"),
    )

    # Worker identity -- from K8s downward API or a generated fallback.
    worker_id = settings.worker_id or f"worker-{id(asyncio.get_event_loop()):x}"
    settings.worker_id = worker_id

    # Construct the TurnConcurrencyGate before the harness_factory
    # closure so harnesses can hold a reference and release/re-acquire
    # their slot during idle waits (e.g. delegate_task polling a child).
    # Without this, a parent that's just sleeping waiting for its
    # child still counts against the per-tenant cap, and a deep
    # delegation chain can saturate the gate on a single user prompt.
    from surogates.config import SHARED_WORK_QUEUE_KEY  # noqa: F401  -- consumed at orchestrator construction below
    from surogates.runtime import TurnConcurrencyGate

    turn_gate = TurnConcurrencyGate(
        redis_client,
        default_max=getattr(
            settings.worker, "max_concurrent_turns_default", 10,
        ),
    )

    # 7. Harness factory -- creates a fully-wired AgentHarness for a given session.
    async def harness_factory(session_id: UUID) -> AgentHarness:
        """Build an AgentHarness with all dependencies injected.

        Resolves the tenant per-session from the dequeued row — the
        worker pool serves every shared-runtime agent on demand and
        carries no process-wide tenant identity.
        """
        session = await session_store.get_session(session_id)
        session_org_id = UUID(str(session.org_id))

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

        # Resolve AgentRuntimeContext early so its
        # storage_key_prefix feeds TenantContext.asset_root.
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
        discovered_mcp_tools: set[str] = set()
        if mcp_proxy_client is not None:
            try:
                principal_user_id = session.user_id or session.service_account_id
                if principal_user_id is not None:
                    discovered_mcp_tools = set(
                        await mcp_proxy_client.discover_and_register(
                            org_id=session_org_id,
                            user_id=principal_user_id,
                            session_id=session.id,
                            agent_id=ctx.agent_id,
                            is_service_account=session.user_id is None,
                        )
                    )
            except Exception:
                logger.warning(
                    "MCP proxy tool discovery failed for session %s; "
                    "built-in tools still available",
                    session.id, exc_info=True,
                )

        # Per-session LLM bundle resolved from the AgentRuntimeContext
        # (already pulled above for ``asset_root``) + the credential
        # vault.  Every agent has ``llm_main`` populated at create
        # time by ops's ``create_shared_agent_extras``; an empty
        # main here means the agent's runtime config is broken and
        # the session deserves to fail fast.
        from surogates.harness.session_llm import (
            build_session_llm_clients,
            resolve_video_endpoint,
        )
        from surogates.tools.builtin.media_gen import MediaGenConfig

        llm_bundle = await build_session_llm_clients(
            ctx, vault=credential_vault, user_id=tenant.user_id,
            settings=settings,
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
        image_slot = llm_bundle.image
        video_endpoint = await resolve_video_endpoint(
            ctx, vault=credential_vault, user_id=tenant.user_id,
            settings=settings,
        )
        media_gen_config = MediaGenConfig(
            image_client=image_slot.client if image_slot is not None else None,
            image_model=image_slot.model if image_slot is not None else "",
            video_model=video_endpoint.model if video_endpoint is not None else "",
            video_base_url=(
                video_endpoint.base_url if video_endpoint is not None else ""
            ),
            video_api_key=(
                video_endpoint.api_key if video_endpoint is not None else ""
            ),
            video_timeout=settings.llm.video_timeout,
            video_poll_interval=settings.llm.video_poll_interval,
        )
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

        # R2-backed per-user memory store.  Requires both the memory
        # cache (wired at worker startup) and a storage backend; the
        # in-memory fallback below covers test contexts only.
        memory_cache = worker_state.get("memory_cache")
        if memory_cache is not None:
            from surogates.memory.r2_store import (
                MEMORY_TARGETS, R2MemoryStore,
            )
            from surogates.runtime.memory_protocol import memory_object_key

            mem_bucket = (
                settings.storage.memory_bucket or settings.storage.bucket
            )
            _user_id_str = (
                str(session.user_id) if session.user_id else None
            )
            mem_keys = {
                target: memory_object_key(
                    storage_key_prefix=ctx.storage_key_prefix,
                    user_id=_user_id_str,
                    target=target,
                )
                for target in MEMORY_TARGETS
            }
            # on every successful write, publish
            # user.memory_changed:<org_id>:<user_id> on Redis so other
            # workers serving the same user invalidate their L1
            # MemoryCache entry; also emit a write/conflict audit so
            # dashboards surface conflict rates per tenant.
            from surogates.audit.types import AuditType

            _user_token = (
                str(session.user_id) if session.user_id else "shared"
            )
            _memory_channel = (
                f"user.memory_changed:{ctx.org_id}:{_user_token}".encode()
            )

            async def _on_memory_write(
                action, *, target, new_version, conflict_detected,
            ):
                try:
                    await redis_client.publish(
                        f"user.memory_changed:{ctx.org_id}:{_user_token}",
                        _memory_channel,
                    )
                except Exception:
                    logger.warning(
                        "Failed to publish memory invalidation",
                        exc_info=True,
                    )
                try:
                    await audit_store.emit(
                        org_id=session_org_id,
                        agent_id=ctx.agent_id,
                        user_id=session.user_id,
                        type=(
                            AuditType.MEMORY_CONFLICT
                            if conflict_detected
                            else AuditType.MEMORY_WRITE
                        ),
                        data={
                            "action": action,
                            "target": target,
                            "version": new_version,
                        },
                    )
                except Exception:
                    logger.warning(
                        "Failed to emit memory audit", exc_info=True,
                    )

            memory_store = R2MemoryStore(
                backend=storage_backend, bucket=mem_bucket, keys=mem_keys,
                on_write=_on_memory_write,
            )
            await memory_store.load_from_r2()
        else:
            memory_store = MemoryStore(memory_dir=memory_dir)
        memory_manager = MemoryManager(memory_store)

        # Resolve the per-session file bundle once and share it
        # across the catalog load and the PromptBuilder content
        # pre-load.  ``None`` is acceptable for agents that haven't
        # had their first publish yet; the loaders return ``None``
        # silently and the prompt builder skips the SOUL.md /
        # AGENT.md sections rather than crashing.
        bundle = None
        file_bundle_cache = worker_state.get("file_bundle_cache")
        if file_bundle_cache is not None and ctx.bundle_hub_ref:
            try:
                bundle = await file_bundle_cache.get(session.agent_id)
            except LookupError:
                bundle = None

        # Resolve the shared system-skills bundle once per session.
        # ``None`` when the system-skills repo has no v* tag yet — the
        # loader treats that as 'no Layer 1a' and the rest of the
        # layers still resolve.
        system_bundle = None
        system_bundle_cache = worker_state.get("system_bundle_cache")
        if system_bundle_cache is not None:
            try:
                system_bundle = await system_bundle_cache.get()
            except LookupError:
                system_bundle = None

        # Load prompt-visible catalogs.  Expert skill definitions may still
        # exist in storage, but PromptBuilder hides them from executor prompts
        # now that strategic advice is handled by the harness advisor.
        # Coordinator sessions also render sub-agents as an "Available
        # Sub-Agents" block and use their presence to gate delegation
        # tool schemas.
        available_agents, available_skills = await _load_prompt_catalogs(
            tenant=tenant,
            session_factory=session_factory,
            bundle=bundle,
            system_bundle=system_bundle,
        )

        # Knowledge bases attached to this agent. Empty list when
        # KB tools are unavailable (no ops DB) or no KBs are wired
        # to this agent yet.
        attached_kbs = await _load_attached_kbs(
            agent_id=session.agent_id,
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
        # "Live browser support" capability: drop the browser_* tools from
        # the model-visible set when the agent has it turned off.  The
        # shared browser pool stays wired; this agent's LLM just never
        # sees the schemas, so the browser pane stays empty.  Derived from
        # the registry's ``browser`` toolset so it never drifts.
        if not ctx.browser_enabled:
            effective_tools.difference_update(
                e.name
                for e in tool_registry.get_all()
                if e.toolset == "browser"
            )
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

        # Pre-load SOUL.md / AGENT.md from the bundle resolved
        # above.  The PromptBuilder stays sync; the loaders return
        # ``None`` silently when the bundle is missing or doesn't
        # carry those files and the builder skips those sections.
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
            slash_commands=ctx.slash_commands,
            brainstorming_gate=ctx.brainstorming_gate,
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
                agent_id=session.agent_id,
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
                    agent_id=session.agent_id,
                )

        # Build a TurnSummarizer when the kill-switch is on. The
        # per-turn recap + artifact curation runs on the agent's base
        # model (curating deliverables needs the strong model);
        # iteration one-liners reuse the cheap summary client already
        # constructed for context compression, and are skipped when no
        # summary model is configured.
        from surogates.harness.turn_summarizer import TurnSummarizer

        if settings.worker.emit_turn_summaries:
            turn_summarizer: TurnSummarizer | None = TurnSummarizer(
                base_client=llm_client,
                base_model=model_id,
                summary_client=(
                    summary_slot.client if summary_slot is not None else None
                ),
                summary_model=(
                    summary_slot.model if summary_slot is not None else ""
                ),
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
            credential_vault=credential_vault,
            default_model=model_id,
            session_factory=session_factory,
            saga_enabled=settings.saga.enabled,
            saga_settings=settings.saga if settings.saga.enabled else None,
            log_policy_allowed=settings.governance.log_allowed,
            summary_client=(
                summary_slot.client if summary_slot is not None else None
            ),
            summary_model=(
                summary_slot.model if summary_slot is not None else ""
            ),
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
            media_gen=media_gen_config,
            turn_summarizer=turn_summarizer,
            # Share the per-session bundle with
            # the harness so sub-agent resolution at wake time can
            # see Hub-backed ``agents/`` entries alongside the disk
            # built-ins.  Already resolved at line ~1140 for the
            # PromptBuilder catalog load; threading it here avoids a
            # second file_bundle_cache lookup per session.
            bundle=bundle,
            # Share the turn gate so the harness can release the
            # slot while idle-waiting for a delegated child.  This
            # closes the per-tenant cap leak that used to choke
            # deep-research and other fan-out workflows.
            turn_gate=turn_gate,
            # Per-agent MCP tool set discovered above; the harness uses
            # it to filter the shared registry's prompt schemas down to
            # this agent's own MCP tools.
            mcp_tool_names=frozenset(discovered_mcp_tools),
            # Per-agent slash-command gating resolved from the runtime
            # config; the dispatch gate refuses disabled commands.
            slash_commands=ctx.slash_commands,
        )
        # Stash the bundle so the dispatcher can
        # aclose its four connection pools at session retirement.
        # A long-running worker would otherwise accumulate one pool
        # per processed session.
        harness._session_llm_bundle = llm_bundle  # type: ignore[attr-defined]
        return harness

    # 8. Orchestrator — consumes from the shared work queue.
    # Per-tenant isolation is enforced by the ``turn_gate`` built
    # above and shared with both the dispatcher (acquire on dequeue)
    # and every harness (release/re-acquire around idle waits).

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
        agent_id=None,
        queue_key=SHARED_WORK_QUEUE_KEY,
        poll_timeout=settings.worker.poll_timeout,
        browser_pool=browser_pool,
        session_factory=session_factory,
        tenant_for_task=_tenant_for_task,
        turn_gate=turn_gate,
        file_bundle_cache=worker_state.get("file_bundle_cache"),
    )

    # Scheduled-work polling is owned by the platform ticker
    # (``surogates.scheduled.platform_ticker``) which runs as its
    # own Deployment with Redis leader election.  Workers no longer
    # carry a per-tenant scheduled-session runner.

    from surogates.jobs.inbox_expire import run_expire_loop
    inbox_expire_task = asyncio.create_task(
        run_expire_loop(session_store),
        name="inbox-expire-sweeper",
    )

    from surogates.jobs.board_maintenance import run_board_maintenance_loop
    board_maintenance_task = asyncio.create_task(
        run_board_maintenance_loop(session_factory),
        name="board-maintenance-sweeper",
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
        SHARED_WORK_QUEUE_KEY,
    )

    try:
        await orchestrator.run()
    finally:
        # Cleanup
        logger.info("Worker %s shutting down", worker_id)
        inbox_expire_task.cancel()
        try:
            await inbox_expire_task
        except asyncio.CancelledError:
            pass
        board_maintenance_task.cancel()
        try:
            await board_maintenance_task
        except asyncio.CancelledError:
            pass
        await health_server.stop()
        await browser_pool.destroy_all()
        await sandbox_pool.destroy_all()
        # K8sSandbox holds an aiohttp session for the executor daemons;
        # ProcessSandbox has no aclose, hence the getattr guard.
        backend_close = getattr(sandbox_backend, "aclose", None)
        if backend_close is not None:
            await backend_close()
        await mcp_proxy_client.close()
        # Drain the worker-side runtime cache +
        # invalidator before tearing down the redis client.
        await _stop_worker_invalidator(worker_state)
        await _shutdown_worker_runtime_plumbing(worker_state)
        await redis_client.aclose()
        await engine.dispose()
        logger.info("Worker %s stopped", worker_id)
