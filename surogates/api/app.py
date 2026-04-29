"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from surogates.audit import AuditStore
from surogates.config import load_settings
from surogates.db.engine import async_engine_from_settings, async_session_factory
from surogates.harness.prompt_library import default_library as default_prompt_library
from surogates.session.store import SessionStore
from surogates.storage.backend import create_backend
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
            session_factory, encryption_key=encryption_key.encode("utf-8"),
        )
    except ValueError:
        logger.error(
            "SUROGATES_ENCRYPTION_KEY is not a valid Fernet key; "
            "credential vault disabled.",
        )
        return None


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
    app.state.session_store = SessionStore(app.state.session_factory, redis=app.state.redis)
    app.state.audit_store = AuditStore(app.state.session_factory)
    app.state.storage = create_backend(settings)
    # Optional embedding client for the KB retrieval layer. Returns
    # None when settings.embeddings.enabled=False; downstream code
    # treats None as "BM25 only" / "skip embedding writes".
    from surogates.storage.embeddings import create_embedding_client
    app.state.embedder = create_embedding_client(settings.embeddings)

    app.state.credential_vault = _build_vault(
        settings.encryption_key, app.state.session_factory,
    )

    from surogates.channels.pairing import PairingStore
    app.state.pairing_store = PairingStore(redis=app.state.redis)

    logger.info("Surogates API started (workers=%d)", settings.api.workers)

    yield

    # -- shutdown ---------------------------------------------------------
    logger.info("Surogates API shutting down")
    await app.state.redis.aclose()
    await engine.dispose()


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

    configure_logging(level=settings.api.log_level if hasattr(settings.api, "log_level") else logging.INFO)

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
        auth,
        clarify,
        events,
        feedback,
        health,
        knowledge_bases,
        memory,
        prompts,
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
    app.include_router(events.router, prefix="/v1", tags=["events"])
    app.include_router(feedback.router, prefix="/v1", tags=["feedback"])
    # Service-account feedback (automated judges from the API channel)
    # reaches the same handler through the SA-token path prefix.
    app.include_router(feedback.router, prefix="/v1/api", tags=["feedback"])
    app.include_router(tools.router, prefix="/v1", tags=["tools"])
    app.include_router(skills.router, prefix="/v1", tags=["skills"])
    app.include_router(
        knowledge_bases.router, prefix="/v1", tags=["knowledge_bases"],
    )
    app.include_router(agents.router, prefix="/v1", tags=["agents"])
    app.include_router(memory.router, prefix="/v1", tags=["memory"])
    app.include_router(prompts.router, prefix="/v1", tags=["prompts"])
    app.include_router(transparency.router, prefix="/v1", tags=["transparency"])
    app.include_router(website.router, prefix="/v1", tags=["website"])
    app.include_router(workspace.router, prefix="/v1", tags=["workspace"])
    app.include_router(artifacts.router, prefix="/v1", tags=["artifacts"])
    app.include_router(clarify.router, prefix="/v1", tags=["clarify"])
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
