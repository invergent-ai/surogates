"""The ``surogates rehearsal`` process: worlds plus the internal API."""

from __future__ import annotations

import asyncio
import logging

import httpx
import uvicorn
from fastapi import FastAPI

from surogates.channels.identity import get_or_create_channel_identity
from surogates.rehearsal import api, discord, graph, slack, teams, telegram
from surogates.rehearsal.actor import Actor

logger = logging.getLogger(__name__)


def create_app(*, settings, session_factory, vault, http, provision=get_or_create_channel_identity) -> FastAPI:
    app = FastAPI(title="Surogate Rehearsal")
    app.state.settings, app.state.session_factory = settings, session_factory
    app.state.vault, app.state.http, app.state.provision = vault, http, provision
    app.include_router(api.router)
    app.include_router(graph.router)
    app.include_router(telegram.router)
    app.include_router(slack.router)
    app.include_router(teams.router)
    app.include_router(discord.router)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    return app


async def run_rehearsal(settings) -> None:
    from surogates.db.engine import async_engine_from_settings, async_session_factory
    from surogates.tenant.credentials import CredentialVault

    if not settings.encryption_key or not settings.rehearsal.internal_token:
        raise SystemExit("rehearsal needs encryption_key and rehearsal.internal_token")
    engine = async_engine_from_settings(settings.db)
    sf = async_session_factory(engine)
    vault = CredentialVault(sf, encryption_key=settings.encryption_key.encode("utf-8"))
    async with httpx.AsyncClient(timeout=10) as http:
        app = create_app(settings=settings, session_factory=sf, vault=vault, http=http)
        actor = asyncio.create_task(Actor(app.state).run())
        config = uvicorn.Config(app, host="0.0.0.0", port=settings.rehearsal.port, log_config=None)
        logger.info("[rehearsal] serving on :%s", settings.rehearsal.port)
        await uvicorn.Server(config).serve()
        actor.cancel()
    await engine.dispose()
