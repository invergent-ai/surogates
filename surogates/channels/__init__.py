"""Channel adapters -- the only user interface.

The Surogates platform has no CLI or TUI.  All user interaction flows
through *channels*:

* **web** -- The REST API + SSE stream.  This is the primary channel and
  is always available.  The browser SPA talks directly to the FastAPI
  routes; no adapter process is needed.

* **slack**, **teams**, **telegram** -- Messaging-platform adapters that
  run as long-lived async processes, bridging platform-native events into
  Surogates sessions.  Phase 2.

* **webhook** -- A generic HTTP callback adapter for custom integrations.
  Phase 2.

Public API
----------
.. autofunction:: start_channel
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from surogates.config import Settings

__all__ = ["start_channel"]

logger = logging.getLogger(__name__)

# Registry of channel type -> (module_path, class_name).  Using strings
# avoids importing heavy platform SDKs at module load time.
_ADAPTER_REGISTRY: dict[str, tuple[str, str]] = {
    "slack": ("surogates.channels.slack", "SlackAdapter"),
    "teams": ("surogates.channels.teams", "TeamsAdapter"),
    "telegram": ("surogates.channels.telegram", "TelegramAdapter"),
    "webhook": ("surogates.channels.webhook", "WebhookAdapter"),
}


async def start_channel(channel_type: str, settings: Settings) -> None:
    """Start a channel adapter by type.

    Called by ``surogate channel <type>`` (the CLI entry-point).  The
    function instantiates the requested adapter, connects it, installs
    signal handlers for graceful shutdown, and blocks until a termination
    signal is received.

    The **web** channel is not started through this function -- it is the
    FastAPI application itself (``surogate api``).

    Parameters
    ----------
    channel_type:
        One of ``'slack'``, ``'teams'``, ``'telegram'``, ``'webhook'``.
    settings:
        Fully loaded application settings.

    Raises
    ------
    ValueError
        If *channel_type* is ``'web'`` (use ``surogate api`` instead) or
        is not a recognised channel name.
    NotImplementedError
        Phase 2 adapters raise this from ``connect()``.
    """
    if channel_type == "web":
        raise ValueError(
            "The web channel is the REST API itself.  "
            "Start it with `surogate api`, not `surogate channel web`."
        )

    entry = _ADAPTER_REGISTRY.get(channel_type)
    if entry is None:
        valid = ", ".join(sorted(_ADAPTER_REGISTRY))
        raise ValueError(
            f"Unknown channel type {channel_type!r}.  "
            f"Valid types: {valid}"
        )

    module_path, class_name = entry
    logger.info("Starting %s channel adapter ...", channel_type)

    # Lazy-import the adapter module and class.
    import importlib

    module = importlib.import_module(module_path)
    adapter_cls = getattr(module, class_name)

    # Build infrastructure dependencies.
    from redis.asyncio import Redis

    from surogates.channels.delivery import DeliveryService
    from surogates.db.engine import async_engine_from_settings, async_session_factory
    from surogates.session.store import SessionStore

    engine = async_engine_from_settings(settings.db)
    session_factory = async_session_factory(engine)
    redis_client = Redis.from_url(settings.redis.url)
    delivery_service = DeliveryService(session_factory, redis_client)
    session_store = SessionStore(session_factory, redis=redis_client)

    # Instantiate the adapter with full dependencies.
    if channel_type == "slack":
        adapter = adapter_cls(
            slack_settings=settings.slack,
            api_settings=settings.api,
            delivery_service=delivery_service,
            session_store=session_store,
            session_factory=session_factory,
            redis_client=redis_client,
        )
    else:
        adapter = adapter_cls(
            settings={},
            delivery_service=delivery_service,
            session_store=session_store,
            session_factory=session_factory,
            redis_client=redis_client,
        )

    # Set up graceful shutdown.
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig: signal.Signals) -> None:
        logger.info("Received %s -- shutting down %s adapter", sig.name, channel_type)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_shutdown, sig)

    try:
        await adapter.connect()
        logger.info("%s adapter connected -- waiting for shutdown signal", channel_type)
        await shutdown_event.wait()
    finally:
        logger.info("Disconnecting %s adapter ...", channel_type)
        try:
            await adapter.disconnect()
        except NotImplementedError:
            pass
        await redis_client.aclose()
        await engine.dispose()
        logger.info("%s adapter shut down", channel_type)
