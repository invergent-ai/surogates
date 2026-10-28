"""``keel-link``: Keel's private door into the runtime.

Its own process (``surogates keel-link``) on its own port, serving only
the Keel routes and ``/health``.  It reads the same ``app.state`` as the
API, but has no JWT middleware: each Keel route checks the link key
itself.  It must stay internal, never behind a public ingress, and
:func:`~surogates.api.app.create_app` never mounts its routes.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from surogates.api.app import runtime_app_state
from surogates.config import load_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with runtime_app_state(app, name="Surogates keel-link"):
        yield


def create_keel_link_app() -> FastAPI:
    """Build the keel-link application.

    Called by uvicorn as a factory
    (``uvicorn surogates.api.keel_link:create_keel_link_app --factory``).
    """
    settings = load_settings()

    from surogates.logging_config import configure_logging

    configure_logging(level=settings.log_level)

    app = FastAPI(
        title="Surogates keel-link",
        description="Keel's internal link to the runtime",
        version="0.1.0",
        lifespan=_lifespan,
        # Nothing here is for browsing: no schema, no docs pages.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    from surogates.api.routes import health, keel, keel_analysis, keel_case_agents, keel_items, keel_reruns

    app.include_router(health.router, tags=["health"])
    app.include_router(keel.router, prefix="/v1", tags=["keel"])
    app.include_router(keel_items.router, prefix="/v1", tags=["keel"])
    app.include_router(keel_case_agents.router, prefix="/v1", tags=["keel"])
    app.include_router(keel_analysis.router, prefix="/v1", tags=["keel"])
    app.include_router(keel_reruns.router, prefix="/v1", tags=["keel"])
    if not settings.keel.link_key:
        logger.warning("SUROGATES_KEEL_LINK_KEY is empty: keel-link refuses every call.")
    return app
