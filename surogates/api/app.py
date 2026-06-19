"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from surogates.audit import AuditStore
from surogates.config import load_settings
from surogates.db.engine import async_engine_from_settings, async_session_factory
from surogates.harness.prompt_library import default_library as default_prompt_library
from surogates.session.store import SessionStore
from surogates.storage.backend import create_backend
from surogates.browser.profiles import BrowserProfileStore
from surogates.tenant.credentials import CredentialVault

logger = logging.getLogger(__name__)


def _build_vault(encryption_key: str, session_factory) -> CredentialVault | None:
    """Return a ``CredentialVault``, or ``None`` if the key is missing
    or malformed — a bad key must not crash startup."""
    if not encryption_key:
        logger.warning("SUROGATES_ENCRYPTION_KEY not set; credential vault disabled.")
        return None
    try:
        return CredentialVault(
            session_factory,
            encryption_key=encryption_key.encode("utf-8"),
        )
    except ValueError:
        logger.error(
            "SUROGATES_ENCRYPTION_KEY is not a valid Fernet key; "
            "credential vault disabled.",
        )
        return None


def _install_browser_api_dependencies(app: Any, settings: Any) -> None:
    """Install API-side browser resolver/control dependencies on app.state."""
    from surogates.browser.control import BrowserControlStore
    from surogates.browser.registry import BrowserRegistry
    from surogates.browser.resolver import BrowserResolver
    from surogates.config import enqueue_session
    from surogates.session.events import EventType

    backend = None
    if settings.browser.backend == "kubernetes":
        from surogates.browser.kubernetes import K8sBrowserBackend

        backend = K8sBrowserBackend(
            namespace=settings.browser.k8s_namespace,
            service_account=settings.browser.k8s_service_account,
            cluster_domain=settings.browser.k8s_cluster_domain,
            pod_ready_timeout=settings.browser.pod_ready_timeout,
            endpoint_probe_timeout=settings.browser.endpoint_probe_timeout,
            image=settings.browser.image,
            storage_settings=getattr(settings, "storage", None),
            s3fs_image=settings.browser.k8s_s3fs_image,
            s3_endpoint=settings.browser.k8s_s3_endpoint,
        )

    browser_registry = BrowserRegistry(app.state.redis)
    browser_control = BrowserControlStore(app.state.redis)
    browser_resolver = BrowserResolver(registry=browser_registry, backend=backend)

    async def emit_session_event(
        session_id: str,
        event_type: EventType | str,
        data: dict,
    ) -> None:
        resolved_type = (
            event_type if isinstance(event_type, EventType) else EventType(event_type)
        )
        await app.state.session_store.emit_event(
            UUID(session_id),
            resolved_type,
            data,
        )

    async def wake_session(session_id: str) -> None:
        # the shared queue needs the tenant tuple
        # so the dispatcher's gate check can find the
        # session's org without a DB round-trip.  Look up the row
        # once; the caller's hot path runs at most a few times per
        # request so the extra read is negligible.
        session = await app.state.session_store.get_session(
            UUID(session_id),
        )
        await enqueue_session(
            app.state.redis,
            org_id=str(session.org_id),
            agent_id=session.agent_id,
            session_id=session_id,
        )

    app.state.browser_registry = browser_registry
    app.state.browser_control = browser_control
    app.state.browser_resolver = browser_resolver
    app.state.browser_backend = backend
    app.state.session_event_emitter = emit_session_event
    app.state.session_wake = wake_session


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown resources."""
    # -- startup ----------------------------------------------------------
    settings = load_settings()

    # Validate bundled prompt fragments up front so a missing or malformed
    # file fails the readiness probe instead of crashing a live session.
    # Bodies stay cached after validation, so production requests never
    # hit disk for prompt prose.
    default_prompt_library().validate()

    engine = async_engine_from_settings(settings.db)

    app.state.settings = settings
    app.state.session_factory = async_session_factory(engine)
    app.state.redis = Redis.from_url(settings.redis.url)
    app.state.session_store = SessionStore(
        app.state.session_factory, redis=app.state.redis
    )
    app.state.audit_store = AuditStore(app.state.session_factory)
    app.state.storage = create_backend(settings)

    app.state.credential_vault = _build_vault(
        settings.encryption_key,
        app.state.session_factory,
    )
    app.state.browser_profile_store = (
        BrowserProfileStore(
            app.state.session_factory,
            encryption_key=settings.encryption_key.encode("utf-8"),
        )
        if settings.encryption_key
        else None
    )

    from surogates.channels.pairing import PairingStore

    app.state.pairing_store = PairingStore(redis=app.state.redis)
    _install_browser_api_dependencies(app, settings)

    # Per-request resolution goes through ``agent_runtime_context_dep``
    # which reads the caches wired below.
    _install_shared_runtime_plumbing(app, settings)

    logger.info("Surogates API started (workers=%d)", settings.api.workers)

    yield

    # -- shutdown ---------------------------------------------------------
    logger.info("Surogates API shutting down")
    await _shutdown_shared_runtime_plumbing(app)
    await app.state.redis.aclose()
    await engine.dispose()


def _install_shared_runtime_plumbing(app: FastAPI, settings: Any) -> None:
    """Wire the PlatformClient + RuntimeConfigCache onto app.state.

    Constructed once per process; ``aclose()``'d on shutdown.
    Requires ``settings.platform_api_url`` — surfaced at boot via
    the RuntimeError below so a misconfigured pod doesn't silently
    serve broken requests.

    Also starts a background ``run_invalidator`` task that listens on
    the Redis pub/sub channels surogate-ops publishes to when admins
    mutate per-agent runtime config.  The task is cancelled cleanly on
    shutdown.
    """
    import asyncio

    from surogates.runtime import (
        ChannelRoutingCache,
        FileBundleCache,
        FirebaseConfig,
        FirebaseConfigCache,
        PerTenantRateLimiter,
        PlatformClient,
        RuntimeConfigCache,
        SlugResolverCache,
        SystemBundleCache,
        run_invalidator,
    )

    if not settings.platform_api_url:
        raise RuntimeError(
            "SUROGATES_PLATFORM_API_URL is required; the api pod "
            "cannot resolve per-tenant context without it",
        )

    client = PlatformClient(
        base_url=settings.platform_api_url,
        token=settings.platform_api_token,
    )
    cache = RuntimeConfigCache(
        loader=client.get_runtime_config,
        ttl_seconds=1.0,
    )
    # Project dict → FirebaseConfig dataclass at the loader edge so
    # every callsite gets a typed object back from the cache.  The
    # PlatformClient returns the raw JSON; FirebaseConfig mirrors the
    # field shape one-to-one.
    async def _load_firebase(project_id: str) -> FirebaseConfig:
        payload = await client.get_firebase_config(project_id)
        return FirebaseConfig(
            project_id=payload["project_id"],
            firebase_project_id=payload["firebase_project_id"],
            api_key=payload["api_key"],
            auth_domain=payload["auth_domain"],
            enabled_providers=tuple(payload.get("enabled_providers") or ()),
            app_id=payload.get("app_id"),
            messaging_sender_id=payload.get("messaging_sender_id"),
            measurement_id=payload.get("measurement_id"),
        )

    firebase_cache = FirebaseConfigCache(
        loader=_load_firebase,
        ttl_seconds=60.0,
    )
    slug_cache = SlugResolverCache(
        loader=client.get_agent_id_for_slug,
        ttl_seconds=30.0,
    )
    rate_limiter = PerTenantRateLimiter(
        app.state.redis,
        default_rpm=getattr(settings.api, "rate_limit_rpm", 300),
    )

    # File-bundle cache — Hub is required.  Builder raises if
    # ``settings.hub.endpoint`` is empty.
    file_bundle_cache: FileBundleCache = build_file_bundle_cache(
        settings=settings, runtime_config_cache=cache,
    )

    # Shared system-skills bundle cache — one snapshot per cluster
    # (not per-agent).  Lazily resolves the latest v* tag on first
    # ``get()``; the seed-builtin-skills CLI fires
    # ``system_skills_changed:<tag>`` on Redis to drop the cache
    # whenever the catalog changes.
    system_bundle_cache: SystemBundleCache = build_system_bundle_cache(
        settings=settings,
    )

    # Per-user memory cache — storage backend is required.  Builder
    # raises if ``settings.storage.bucket`` is empty.  Storage was
    # wired earlier as ``app.state.storage`` (line ~132); the historic
    # ``app.state.storage_backend`` name is a stale alias from an
    # earlier rev that ``build_memory_cache`` still expects.
    memory_cache = build_memory_cache(
        settings=settings,
        storage_backend=app.state.storage,
    )

    # Channel routing cache.  Powers the shared adapter pod's
    # per-event tenant resolution.  In the api process the cache
    # is wired primarily so the invalidator subscribes to the
    # channel_routing_changed:<kind>:<id> Redis channel; the api
    # itself doesn't currently dispatch inbound channel events,
    # but keeping the cache + invalidator alive here means every
    # pod picks up the same code path.
    channel_routing_cache = build_channel_routing_cache(
        settings=settings, platform_client=client,
    )

    app.state.platform_client = client
    app.state.runtime_config_cache = cache
    app.state.firebase_config_cache = firebase_cache
    app.state.slug_resolver_cache = slug_cache
    app.state.rate_limiter = rate_limiter
    app.state.file_bundle_cache = file_bundle_cache
    app.state.memory_cache = memory_cache
    app.state.channel_routing_cache = channel_routing_cache
    app.state.system_bundle_cache = system_bundle_cache
    app.state.runtime_invalidator_task = asyncio.create_task(
        run_invalidator(
            app.state.redis,
            runtime_config_cache=cache,
            firebase_cache=firebase_cache,
            slug_cache=slug_cache,
            file_bundle_cache=file_bundle_cache,
            memory_cache=memory_cache,
            channel_routing_cache=channel_routing_cache,
            system_bundle_cache=system_bundle_cache,
        ),
        name="surogates-runtime-invalidator",
    )
    logger.info(
        "Shared-runtime plumbing wired (platform=%s)", settings.platform_api_url,
    )


def build_memory_cache(*, settings, storage_backend):
    """Construct an R2-backed MemoryCache.

    Requires both a storage backend and a configured bucket — the
    harness never falls back to disk memory, so this raises rather
    than returning ``None`` on a misconfig.
    """
    from surogates.runtime import MemoryCache
    from surogates.runtime.memory_protocol import (
        EnvelopeDecodeError, _MemoryEnvelope,
        decode_envelope, encode_envelope,
    )

    storage = getattr(settings, "storage", None)
    if storage_backend is None or storage is None or not storage.bucket:
        raise RuntimeError(
            "Memory cache requires a configured storage backend + "
            "non-empty ``storage.bucket``",
        )

    bucket = storage.memory_bucket or storage.bucket
    fresh_envelope_bytes = encode_envelope(
        _MemoryEnvelope(version=0, content=""),
    )

    async def _loader(key: str) -> bytes:
        # The cache key is the R2 object key (computed by the
        # harness via memory_object_key); the loader fetches the
        # bytes verbatim.  Missing object OR corrupted envelope
        # both resolve to "start fresh" (version=0, content="")
        # so a botched manual migration self-heals on next write
        # instead of crashing session bootstrap.
        try:
            raw = await storage_backend.read(bucket, key)
        except (KeyError, FileNotFoundError):
            return fresh_envelope_bytes
        try:
            decode_envelope(raw)
        except EnvelopeDecodeError:
            return fresh_envelope_bytes
        return raw

    return MemoryCache(loader=_loader, ttl_seconds=5.0)


def build_channel_routing_cache(*, settings, platform_client):
    """Construct a :class:`ChannelRoutingCache`.

    Loader splits the cache key ``"<kind>:<identifier>"`` back into
    the two :meth:`PlatformClient.get_channel_routing` args.
    """
    from surogates.runtime import ChannelRoutingCache

    async def _loader(key: str) -> dict | None:
        kind, _, identifier = key.partition(":")
        return await platform_client.get_channel_routing(kind, identifier)

    return ChannelRoutingCache(loader=_loader, ttl_seconds=30.0)


def build_file_bundle_cache(*, settings, runtime_config_cache):
    """Construct a :class:`FileBundleCache`.

    Requires both ``settings.hub.endpoint`` and the
    ``surogate_hub_sdk`` import to succeed.  Hub is required for
    serving any agent — raises rather than returning ``None`` on
    misconfig so a broken pod fails at boot.
    """
    from surogates.runtime import (
        AgentFileBundle,
        FileBundleCache,
        HubBundleClient,
    )
    from surogates.runtime.bundle_accessor import _BundleSpec
    from surogates.runtime.bundle_cache import (
        _L2DiskCache, _L2ReadThroughHub,
    )

    hub_settings = getattr(settings, "hub", None)
    if hub_settings is None or not hub_settings.endpoint:
        raise RuntimeError(
            "File bundle cache requires ``settings.hub.endpoint``; "
            "Hub is mandatory in shared mode",
        )

    from surogate_hub_sdk import ApiClient, Configuration, ObjectsApi
    from pathlib import Path

    sdk_config = Configuration(
        host=hub_settings.endpoint,
        username=hub_settings.username,
        password=hub_settings.password,
    )
    # Local dev clusters issue self-signed certs for the Hub ingress.
    # The ops side already disables verification (surogate_hub.py:53);
    # mirror that here so the runtime can reach the same endpoint.
    sdk_config.verify_ssl = False
    api_client = ApiClient(sdk_config)
    objects_api = ObjectsApi(api_client)
    l2 = _L2DiskCache(
        root=Path.home() / ".surogate" / "bundle-cache",
    )

    async def _bundle_loader(agent_id: str) -> AgentFileBundle:
        payload = await runtime_config_cache.get(agent_id)
        hub_ref = payload.get("bundle_hub_ref")
        version = payload.get("bundle_version")
        if not hub_ref or not version:
            raise LookupError(
                f"agent {agent_id} has no bundle configured",
            )
        spec = _BundleSpec.parse(hub_ref)
        hub_client = HubBundleClient(
            objects_api=objects_api,
            user=spec.user,
            repository=spec.repository,
        )
        read_through = _L2ReadThroughHub(
            agent_id=agent_id, hub=hub_client, l2=l2,
        )
        return AgentFileBundle(
            agent_id=agent_id,
            hub_ref=hub_ref,
            version=version,
            client=read_through,
        )

    return FileBundleCache(
        loader=_bundle_loader, ttl_seconds=30.0, l2=l2,
    )


def build_system_bundle_cache(*, settings):
    """Construct a :class:`SystemBundleCache` for ``platform/system-skills``.

    Same Hub-endpoint precondition as :func:`build_file_bundle_cache`.
    The latest ``v*`` tag is resolved lazily on first ``get()`` so a
    Hub outage at boot does NOT block the api / worker startup —
    sessions that need skills will fail fast at session-start instead
    of preventing the process from starting at all.

    The hub client is wrapped in the same ``_L2ReadThroughHub`` /
    ``_L2DiskCache`` pair the per-agent bundle uses so a worker
    restart does not have to re-pull every system-skill file from
    Hub.  The L2 cache keys on the literal repo id
    (``platform/system-skills``) instead of an ``agent_id``.
    """
    from surogates.runtime import (
        AgentFileBundle,
        HubBundleClient,
        SYSTEM_SKILLS_REPO,
        SystemBundleCache,
    )
    from surogates.runtime.bundle_accessor import _BundleSpec
    from surogates.runtime.bundle_cache import (
        _L2DiskCache,
        _L2ReadThroughHub,
    )

    hub_settings = getattr(settings, "hub", None)
    if hub_settings is None or not hub_settings.endpoint:
        raise RuntimeError(
            "System bundle cache requires ``settings.hub.endpoint``; "
            "Hub is mandatory in shared mode",
        )

    from pathlib import Path

    from surogate_hub_sdk import (
        ApiClient, Configuration, ObjectsApi, TagsApi,
    )

    sdk_config = Configuration(
        host=hub_settings.endpoint,
        username=hub_settings.username,
        password=hub_settings.password,
    )
    # Local dev clusters issue self-signed certs for the Hub ingress.
    # Mirror build_file_bundle_cache: the platform side already
    # disables verification, so we match it for parity.
    sdk_config.verify_ssl = False
    api_client = ApiClient(sdk_config)
    objects_api = ObjectsApi(api_client)
    tags_api = TagsApi(api_client)
    l2 = _L2DiskCache(
        root=Path.home() / ".surogate" / "bundle-cache",
    )
    spec = _BundleSpec.parse(SYSTEM_SKILLS_REPO)

    async def _resolve_latest_tag() -> str:
        """Return the largest ``v\\d+`` tag id on
        ``platform/system-skills``.

        Raises :class:`LookupError` when the repo has no ``v*`` tag
        yet — the operator must run ``surogate-ops seed-builtin-skills``
        before any session that depends on system skills can resolve
        Layer 1a.
        """
        import asyncio

        # 1000 tags would mean the catalog has been published 1000
        # times — far above realistic operational cadence.  Use
        # ``amount=1000`` and warn if pagination is present so we
        # spot drift early.
        page = await asyncio.to_thread(
            tags_api.list_tags,
            user=spec.user,
            repository=spec.repository,
            amount=1000,
        )
        best = -1
        best_label: str | None = None
        for ref in getattr(page, "results", None) or []:
            label = getattr(ref, "id", "") or ""
            if not label.startswith("v"):
                continue
            try:
                n = int(label[1:])
            except ValueError:
                continue
            if n > best:
                best, best_label = n, label
        if best_label is None:
            raise LookupError(
                f"{SYSTEM_SKILLS_REPO} has no v* tag yet; run "
                "`surogate-ops seed-builtin-skills` first.",
            )
        pagination = getattr(page, "pagination", None)
        if pagination is not None and getattr(pagination, "has_more", False):
            logger.warning(
                "platform/system-skills tag list is paginated "
                "(>1000 v* tags) — pick a higher amount or paginate.",
            )
        return best_label

    async def _loader():
        version = await _resolve_latest_tag()
        hub_client = HubBundleClient(
            objects_api=objects_api,
            user=spec.user,
            repository=spec.repository,
        )
        read_through = _L2ReadThroughHub(
            agent_id=SYSTEM_SKILLS_REPO, hub=hub_client, l2=l2,
        )
        return AgentFileBundle(
            agent_id=SYSTEM_SKILLS_REPO,  # informational only
            hub_ref=SYSTEM_SKILLS_REPO,
            version=version,
            client=read_through,
        )

    return SystemBundleCache(loader=_loader)


async def _shutdown_shared_runtime_plumbing(app: FastAPI) -> None:
    task = getattr(app.state, "runtime_invalidator_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 — cancellation expected here
            pass
        app.state.runtime_invalidator_task = None

    client = getattr(app.state, "platform_client", None)
    if client is not None:
        await client.aclose()
        app.state.platform_client = None

    # Clear cache references on shutdown so a hot reload cannot inherit
    # stale entries from the prior process state.
    if hasattr(app.state, "firebase_config_cache"):
        app.state.firebase_config_cache = None
    if hasattr(app.state, "slug_resolver_cache"):
        app.state.slug_resolver_cache = None
    if hasattr(app.state, "rate_limiter"):
        app.state.rate_limiter = None
    if hasattr(app.state, "file_bundle_cache"):
        app.state.file_bundle_cache = None
    if hasattr(app.state, "memory_cache"):
        app.state.memory_cache = None
    if hasattr(app.state, "channel_routing_cache"):
        app.state.channel_routing_cache = None
    if hasattr(app.state, "system_bundle_cache"):
        app.state.system_bundle_cache = None


def create_app() -> FastAPI:
    """Build and return the FastAPI application.

    Called by uvicorn as a factory (``uvicorn surogates.api.app:create_app --factory``).
    """
    settings = load_settings()

    app = FastAPI(
        title="Surogates",
        description="Managed Agents Platform",
        version="0.1.0",
        lifespan=lifespan,
    )

    # --- CORS ------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- structured logging ------------------------------------------------
    from surogates.logging_config import configure_logging

    configure_logging(
        level=settings.api.log_level
        if hasattr(settings.api, "log_level")
        else logging.INFO
    )

    # --- middleware -------------------------------------------------------
    from surogates.api.middleware.api_prefix import StripApiPrefixMiddleware
    from surogates.api.middleware.auth import setup_auth_middleware
    from surogates.api.middleware.rate_limit import setup_rate_limit_middleware
    from surogates.api.middleware.tenant import setup_tenant_middleware
    from surogates.api.middleware.trace import setup_trace_middleware
    from surogates.api.middleware.website_cors import setup_website_cors_middleware

    # Trace middleware must be registered AFTER the others so that
    # Starlette executes it FIRST (outermost layer).
    setup_auth_middleware(app, settings)
    setup_tenant_middleware(app, settings)
    setup_rate_limit_middleware(app, settings)
    # Website CORS must sit OUTSIDE the global CORS middleware so it
    # can intercept preflights for ``/v1/website/*`` before the global
    # allow-list kicks in, and overwrite response headers with the
    # per-agent decision.
    setup_website_cors_middleware(app)
    setup_trace_middleware(app, settings)

    # The /api prefix strip must be the OUTERMOST layer so every
    # downstream middleware (auth, tenant, rate-limit, trace) and the
    # router see the canonical /v1/... path.
    app.add_middleware(StripApiPrefixMiddleware)

    # --- routes ----------------------------------------------------------
    from surogates.api.routes import (
        admin,
        admin_credentials,
        admin_mcp,
        admin_service_accounts,
        agents,
        artifacts,
        ask_user_question,
        auth,
        board,
        browser,
        coding_agents,
        composio,
        events,
        feedback,
        health,
        inbox,
        memory,
        missions,
        prompts,
        scheduled_work,
        sessions,
        skills,
        tools,
        transparency,
        website,
        workspace,
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(auth.router, prefix="/v1", tags=["auth"])
    app.include_router(sessions.router, prefix="/v1", tags=["sessions"])
    app.include_router(board.router, prefix="/v1", tags=["board"])
    app.include_router(events.router, prefix="/v1", tags=["events"])
    app.include_router(feedback.router, prefix="/v1", tags=["feedback"])
    # Service-account feedback (automated judges from the API channel)
    # reaches the same handler through the SA-token path prefix.
    app.include_router(feedback.router, prefix="/v1/api", tags=["feedback"])
    app.include_router(tools.router, prefix="/v1", tags=["tools"])
    app.include_router(skills.read_router, prefix="/v1", tags=["skills"])
    app.include_router(skills.write_router, prefix="/v1", tags=["skills"])
    # Service-account callers (ops's Work-chat slash menu mints bare
    # ``surg_sk_`` tokens; the auth middleware only allows those on
    # ``/v1/api/*`` routes — see
    # ``tenant.auth.middleware._tenant_context_from_token``) reach the
    # read endpoints via this second mount.  Mutating endpoints stay
    # JWT-only because ``write_router`` is not mounted here.  Mirrors
    # the feedback + missions pattern above.
    app.include_router(skills.read_router, prefix="/v1/api", tags=["skills"])
    app.include_router(agents.router, prefix="/v1", tags=["agents"])
    # Also at /v1/api so the ops server can forward sub-agent catalog CRUD
    # under a bare ``surg_sk_`` ops-chat service-account token (the auth
    # middleware only accepts those on /v1/api/*).  SA contexts have
    # ``user_id=None``, so writes land in the org-shared layer.
    app.include_router(agents.router, prefix="/v1/api", tags=["agents"])
    app.include_router(composio.router, prefix="/v1", tags=["composio"])
    app.include_router(coding_agents.router, prefix="/v1", tags=["coding-agents"])
    # Also at /v1/api so the ops server can forward connect/list/disconnect
    # under a bare ``surg_sk_`` ops-chat service-account token (the auth
    # middleware only accepts those on /v1/api/*).  Mirrors missions/feedback.
    app.include_router(coding_agents.router, prefix="/v1/api", tags=["coding-agents"])
    app.include_router(memory.router, prefix="/v1", tags=["memory"])
    app.include_router(prompts.router, prefix="/v1", tags=["prompts"])
    app.include_router(scheduled_work.router, prefix="/v1", tags=["scheduled-work"])
    app.include_router(transparency.router, prefix="/v1", tags=["transparency"])
    app.include_router(website.router, prefix="/v1", tags=["website"])
    app.include_router(workspace.router, prefix="/v1", tags=["workspace"])
    app.include_router(artifacts.router, prefix="/v1", tags=["artifacts"])
    app.include_router(browser.router, prefix="/v1", tags=["browser"])
    app.include_router(
        ask_user_question.router, prefix="/v1", tags=["ask_user_question"],
    )
    app.include_router(inbox.router, prefix="/v1", tags=["inbox"])
    app.include_router(missions.router, prefix="/v1", tags=["missions"])
    # Service-account callers (ops's Work-chat forwarding path mints
    # bare ``surg_sk_`` tokens; the auth middleware only allows those
    # on ``/v1/api/*`` routes — see
    # ``tenant.auth.middleware._tenant_context_from_token``) reach the
    # same handlers via this second mount.  Read routes here are
    # principal-scoped by ``_principal_owns``, and mutating routes
    # (pause/resume/cancel) authorize against the same predicate, so
    # exposing the surface at /v1/api/missions is safe — an SA token
    # only sees its own rows.  Mirrors the feedback router mount above.
    app.include_router(missions.router, prefix="/v1/api", tags=["missions"])
    app.include_router(admin.router, prefix="/v1/admin", tags=["admin"])
    app.include_router(admin_mcp.router, prefix="/v1/admin", tags=["admin"])
    app.include_router(admin_credentials.router, prefix="/v1/admin", tags=["admin"])
    app.include_router(
        admin_service_accounts.router, prefix="/v1/admin", tags=["admin"]
    )

    # --- frontend SPA ----------------------------------------------------
    # The catch-all route must be registered LAST so it does not shadow
    # the API routers above.
    from surogates.api.frontend import setup_frontend

    build_path = Path(__file__).resolve().parent.parent / "web" / "dist"
    if setup_frontend(app, build_path):
        logger.info("Frontend loaded from %s", build_path)
    else:
        logger.info("Frontend not found at %s (skipping SPA mount)", build_path)

    return app
