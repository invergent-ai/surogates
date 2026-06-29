"""Channel webhook service runner.

Assembles the channel-adapter pieces into one runnable webhook service
process and provides:

- :func:`build_channels_app` — pure-construction factory (no network, no
  serving) that wires the inbound pipeline, dispatcher, delivery dispatcher
  and reconciler.  Testable with mocks.

- :func:`run_channels` — the thin process runner that boots real resources
  (engine, Redis, …), calls :func:`build_channels_app`, starts background
  tasks, serves the FastAPI app via uvicorn, and shuts down cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response

from surogates.channels.channel_observations import append_channel_observation
from surogates.channels.dispatcher import (
    ChannelDeliveryDispatcher,
    ChannelWebhookDispatcher,
    ChannelWebhookReconciler,
)
from surogates.channels.identity import (
    get_or_create_channel_session,
    make_cached_identity_resolver,
    resolve_real_identity,
)
from surogates.channels.inbound import ChannelInboundPipeline, PipelineDeps
from surogates.channels.pairing import PairingStore
from surogates.channels.registry import ChannelRegistry
from surogates.channels.channel_state import ChannelAdapterState
from surogates.config import enqueue_session

logger = logging.getLogger(__name__)

__all__ = ["build_channels_app", "run_channels"]


class _MultiCache:
    """Fan an ``invalidate(key)`` out to several caches.

    The deps factory builds two identity caches (one provisioning ``shadow``
    resolver, one resolve-only ``linked`` resolver).  A ``channel_identity_changed``
    invalidation must evict the key from whichever holds it, so the invalidator
    is wired with this composite rather than a single cache.
    """

    def __init__(self, caches: Any) -> None:
        self._caches = tuple(caches)

    def invalidate(self, key: str) -> None:
        for cache in self._caches:
            cache.invalidate(key)


def _injection_blocks_filename(name: str) -> bool:
    """Return True if the filename looks like a prompt-injection attempt."""
    try:
        from surogates.session.attachment_ingest import get_injection_detector
        return bool(get_injection_detector().detect(name, source="slack_channel").is_injection)
    except Exception:
        return False


def _apply_total_inline_budget_to_attachments(attachments: list) -> None:
    """Apply the cross-file total inline budget over a batch of doc attachments."""
    from surogates.session.attachment_ingest import _apply_inline_total_budget
    outcomes = [
        (a.get("inlined_text"), a.get("inlined_render_kind"), a.get("inline_skip_reason"))
        for a in attachments
    ]
    for a, (text, _kind, reason) in zip(attachments, _apply_inline_total_budget(outcomes)):
        if text is None and reason is not None and a.get("inlined_text") is not None:
            a.pop("inlined_text", None)
            a.pop("inlined_render_kind", None)
            a["inline_skip_reason"] = reason


def _make_deps_factory(
    *,
    session_store: Any,
    redis: Any,
    session_factory: Any,
    mate_settings_cache: Any = None,
    link_url_base: str = "",
    storage: Any = None,
) -> Any:
    """Return a deps_factory for :class:`ChannelWebhookDispatcher`.

    The factory is called once per verified inbound event.  It fills STATIC
    deps (singletons) from closures and constructs PER-EVENT deps (adapter
    state scoped to the routing's agent_id, and the resolver/producer selected
    by the channel's ``identity_policy``) on each call.

    ``identity_policy`` resolver
    ----------------------------
    ``shadow`` (Mate) uses the provisioning resolver — an unknown sender is
    auto-provisioned.  ``linked`` (multi-user assistant) uses a resolve-only
    resolver over :func:`resolve_real_identity` (no provisioning) plus the
    pairing producer, so an unknown sender is privately prompted to link their
    real account.  Both caches are process-wide and memoize the per-message
    lookup.

    ``follow_enabled`` resolver
    ---------------------------
    Built from ``mate_settings_cache`` (a
    :class:`~surogates.runtime.mate_settings_cache.MateSettingsCache`).
    When ``mate_settings_cache`` is ``None`` the resolver is omitted and
    non-mention non-DM messages are always DROPPED — follow toggles propagate
    within the cache TTL (30 s) without a process restart.
    """
    from surogates.runtime.mate_settings_cache import mate_cache_key

    # Process-wide identity caches: one provisioning resolver for ``shadow``,
    # one resolve-only resolver for ``linked`` (provision=None → returns the
    # real identity or None, never auto-provisions).
    shadow_resolver = make_cached_identity_resolver(session_factory)
    linked_resolver = make_cached_identity_resolver(
        session_factory, resolve=resolve_real_identity, provision=None,
    )
    pairing = PairingStore(redis)

    def _link_prompt(code: str) -> str:
        where = (
            f"{link_url_base.rstrip('/')}/link"
            if link_url_base
            else "Surogate Studio (Settings → Channels)"
        )
        return (
            "To talk to me as your own Surogate assistant, link your account: "
            f"enter code {code} at {where}."
        )

    async def _resolve_follow(agent_id: str, platform: str, channel_id: str) -> bool:
        if mate_settings_cache is None or not channel_id:
            return False
        s = await mate_settings_cache.get(mate_cache_key(agent_id, platform, channel_id))
        return bool(s and s.get("follow_enabled"))

    def _factory(kind: str, routing: Any, creds: dict, platform: Any) -> PipelineDeps:
        import time

        state = ChannelAdapterState(redis, agent_id=routing.agent_id, platform=kind)

        policy = (getattr(routing, "config", None) or {}).get("identity_policy", "shadow")
        resolve_identity = linked_resolver if policy == "linked" else shadow_resolver

        async def _pairing_sender(org_id: Any, plat: str, msg: Any, code: str) -> bool:
            """Privately deliver the link prompt; return whether it reached the sender.

            The caller uses the return value to decide the inbound outcome: a
            delivered prompt is ``PAIRING_PROMPTED``, an undelivered one is a
            silent ``DROPPED`` (claiming PAIRING_PROMPTED when nothing reached
            the sender would be a dead-end the user can never escape).
            """
            send_private = getattr(platform, "send_private", None)
            if send_private is None:
                # A platform that can't privately address the sender must not
                # print a usable code into a shared channel — withhold it.
                logger.warning(
                    "[channels] %s has no send_private — link code for %s not delivered",
                    kind, msg.platform_user_id,
                )
                return False
            delivered = await send_private(
                creds,
                sender_id=msg.platform_user_id,
                chat_id=msg.identifier,
                is_dm=msg.is_dm,
                text=_link_prompt(code),
            )
            if not delivered:
                # Private delivery failed (e.g. the user blocked the bot).  The
                # code stays in Redis until its TTL; the sender's next message
                # re-attempts delivery with the same still-live code.
                logger.warning(
                    "[channels] %s send_private failed for %s — link prompt not delivered",
                    kind, msg.platform_user_id,
                )
            return bool(delivered)

        async def _progress(session_id: Any, channel_id: str, thread_ts: Any) -> None:
            post = getattr(platform, "post_thinking_placeholder", None)
            if post is None:  # non-Slack platforms: no placeholder in v1
                return
            try:
                from surogates.channels.channel_progress import post_placeholder_once
                await post_placeholder_once(
                    redis, kind, session_id,
                    post=lambda: post(creds=creds, channel=channel_id, thread_ts=thread_ts),
                    channel=channel_id, thread_ts=thread_ts,
                )
            except Exception:
                logger.warning("[channels] thinking-placeholder post failed", exc_info=True)

        async def _backfill(session_id: Any, channel_id: str, rt: Any) -> Any:
            fetch = getattr(platform, "fetch_channel_context", None)
            if fetch is None:  # non-Slack platforms: no backfill in v1
                return None
            from surogates.channels.channel_backfill import (
                BackfillLimits,
                maybe_seed_session,
            )
            limits = BackfillLimits.from_config(
                (getattr(rt, "config", None) or {}).get("history_backfill"),
            )
            return await maybe_seed_session(
                store=session_store, redis=redis, platform=platform, creds=creds,
                routing=rt, session_id=session_id, channel_id=channel_id,
                limits=limits, now=time.time(),
            )

        async def _attachments(session_id: Any, msg: Any) -> dict:
            download = getattr(platform, "download_file", None)
            if download is None or storage is None or not getattr(msg, "files", None):
                return {"images": [], "attachments": [], "note": ""}
            from pathlib import PurePosixPath
            from surogates.session.attachment_ingest import (
                ingest_attachment_bytes,
                safe_display_name,
                workspace_root_id,
            )
            session = await session_store.get_session(session_id)
            cfg = (getattr(session, "config", None) or {})
            bucket = cfg.get("storage_bucket") or ""
            root_id = workspace_root_id(session)
            ts = getattr(msg, "ts", "") or "0"
            images: list = []
            attachments: list = []
            skipped: list[str] = []
            MAX_FILES = 10
            MAX_BYTES = 20 * 1024 * 1024
            for index, f in enumerate(msg.files):
                if index >= MAX_FILES:
                    skipped.append(safe_display_name(f.filename))
                    continue
                safe = PurePosixPath(f.filename).name
                if safe in ("", ".", ".."):
                    skipped.append("(invalid filename)")
                    continue
                if _injection_blocks_filename(safe):
                    skipped.append("(blocked)")
                    continue
                if f.size is not None and f.size > MAX_BYTES:
                    skipped.append(safe_display_name(safe))
                    continue
                is_image = (f.mime_type or "").lower().startswith("image/")
                if not is_image and not bucket:
                    skipped.append(safe_display_name(safe))
                    continue
                data = await download(creds=creds, url=f.url, max_bytes=MAX_BYTES)
                if data is None:
                    skipped.append(safe_display_name(safe))
                    continue
                path = f"uploads/slack/{ts}-{index}-{safe}"
                try:
                    out = await ingest_attachment_bytes(
                        storage, session=session, root_id=root_id, bucket=bucket,
                        path=path, filename=safe, mime_type=f.mime_type or "application/octet-stream", data=data,
                    )
                except Exception:
                    logger.warning(
                        "[channels] ingest failed for %s", safe, exc_info=True,
                    )
                    skipped.append(safe_display_name(safe))
                    continue
                if "image" in out:
                    images.append(out["image"])
                else:
                    attachments.append(out["attachment"])
            _apply_total_inline_budget_to_attachments(attachments)
            note = (
                f"[shared file(s) not read: {', '.join(skipped)}]" if skipped else ""
            )
            return {"images": images, "attachments": attachments, "note": note}

        async def _pending_input(session_id: Any) -> dict | None:
            try:
                from surogates.session.interactive_input import pending_input_for_session
                return await pending_input_for_session(session_store, session_id=session_id)
            except Exception:
                logger.warning("[channels] pending_input lookup failed", exc_info=True)
                return None

        async def _input_nudge(session_id: Any, msg: Any, text: str) -> None:
            post = getattr(platform, "post_input_nudge", None)
            if post is None:
                return
            await post(
                creds=creds,
                channel=msg.identifier,
                thread_ts=msg.thread_key,
                text=text,
            )

        return PipelineDeps(
            session_store=session_store,
            redis=redis,
            state=state,
            firehose_append=append_channel_observation,
            get_or_create_session=get_or_create_channel_session,
            enqueue_session=enqueue_session,
            resolve_identity=resolve_identity,
            session_factory=session_factory,
            follow_enabled=_resolve_follow,
            pairing=pairing,
            pairing_sender=_pairing_sender,
            backfill=_backfill,
            progress=_progress,
            attachments=_attachments,
            pending_input=_pending_input,
            input_nudge=_input_nudge,
        )

    # Expose both resolvers' caches so the channels process can wire them into
    # the cross-process invalidator (invalidate-on-link).
    _factory.identity_caches = (shadow_resolver.cache, linked_resolver.cache)
    return _factory


def build_channels_app(
    settings: Any,
    *,
    redis: Any,
    session_factory: Any,
    vault: Any,
    platform_client: Any,
    cache: Any,
    delivery_service: Any,
    session_store: Any,
    mate_settings_cache: Any = None,
    registry: ChannelRegistry | None = None,
    storage: Any = None,
) -> tuple[FastAPI, ChannelDeliveryDispatcher, ChannelWebhookReconciler]:
    """Construct the channel webhook FastAPI app and related dispatchers.

    This is a pure-construction function — no network I/O, no serving.

    Parameters
    ----------
    settings:
        Application settings (``settings.channels`` drives enablement).
    redis:
        Async Redis client (long-lived singleton from the caller).
    session_factory:
        SQLAlchemy ``async_sessionmaker``.
    vault:
        :class:`~surogates.tenant.credentials.CredentialVault` or ``None``.
    platform_client:
        :class:`~surogates.runtime.PlatformClient` for the reconciler's
        ``list_channel_routings`` calls.
    cache:
        :class:`~surogates.runtime.ChannelRoutingCache` for tenant resolution.
    delivery_service:
        :class:`~surogates.channels.delivery.DeliveryService` for the delivery
        dispatcher.
    session_store:
        :class:`~surogates.session.store.SessionStore`.
    mate_settings_cache:
        :class:`~surogates.runtime.mate_settings_cache.MateSettingsCache` used
        to resolve per-channel follow settings for the firehose gate.  When
        ``None`` (e.g. in tests that don't need follow), non-mention non-DM
        messages are always DROPPED.
    registry:
        Optional :class:`ChannelRegistry` override (defaults to the
        module-level singleton).  Pass a private registry in tests to avoid
        contaminating the global state.

    Returns
    -------
    tuple[FastAPI, ChannelDeliveryDispatcher, ChannelWebhookReconciler]
        ``(app, delivery_dispatcher, reconciler)`` — the caller starts the
        delivery loops and reconciler as asyncio tasks, then serves ``app``.
    """
    if registry is None:
        from surogates.channels.registry import registry as _global_registry
        registry = _global_registry

    pipeline = ChannelInboundPipeline()
    deps_factory = _make_deps_factory(
        session_store=session_store,
        redis=redis,
        session_factory=session_factory,
        mate_settings_cache=mate_settings_cache,
        link_url_base=getattr(getattr(settings, "channels", None), "studio_url", ""),
        storage=storage,
    )

    dispatcher = ChannelWebhookDispatcher(
        cache=cache,
        vault=vault,
        pipeline=pipeline,
        deps_factory=deps_factory,
        settings=settings,
        registry=registry,
    )

    delivery_dispatcher = ChannelDeliveryDispatcher(
        cache=cache,
        vault=vault,
        delivery_service=delivery_service,
        redis=redis,
        storage=storage,
        session_store=session_store,
    )

    public_url: str = getattr(getattr(settings, "channels", None), "public_url", "")
    reconciler = ChannelWebhookReconciler(
        platform_client=platform_client,
        vault=vault,
        public_url=public_url,
        settings=settings,
        registry=registry,
    )

    app = dispatcher.build_app()
    # Hand the per-message identity caches to the caller so it can wire the
    # cross-process invalidator (link_channel publishes channel_identity_changed
    # on bind; the channels pod evicts the just-linked sender's stale entry).
    app.state.channel_identity_caches = deps_factory.identity_caches

    @app.get("/health")
    async def _health() -> Response:
        return Response(status_code=200)

    return app, delivery_dispatcher, reconciler


async def run_channels(settings: Any, kind: str | None = None) -> None:
    """Bootstrap all resources and run the channel webhook service.

    Serves the FastAPI app on ``settings.channels.port``, starts one
    delivery loop per enabled platform, starts the reconciler pub/sub
    loop, and shuts down cleanly on SIGINT/SIGTERM.

    Parameters
    ----------
    settings:
        Application settings (loaded by ``cmd_channels`` from the config
        file + env vars).
    kind:
        If given, restrict delivery loops to this single platform kind
        (scaling-class escape hatch).  ``None`` = all enabled platforms.
    """
    import uvicorn
    from redis.asyncio import Redis

    from surogates.channels.delivery import DeliveryService
    from surogates.db.engine import async_engine_from_settings, async_session_factory
    from surogates.runtime import ChannelRoutingCache, PlatformClient
    from surogates.session.store import SessionStore
    from surogates.tenant.credentials import CredentialVault

    from surogates.api.app import build_channel_routing_cache, build_mate_settings_cache

    # Self-register built-in platform adapters so the registry is populated
    # before we call enabled_platforms.  The import is a no-op if no
    # platforms/ package exists yet (zero platforms case).
    try:
        import surogates.channels.platforms  # noqa: F401
    except ImportError:
        logger.debug("[channels] surogates.channels.platforms not found — no built-in platforms registered")

    engine = async_engine_from_settings(settings.db)
    sf = async_session_factory(engine)
    redis = Redis.from_url(settings.redis.url)
    session_store = SessionStore(sf, redis=redis)

    vault: CredentialVault | None = None
    if settings.encryption_key:
        try:
            vault = CredentialVault(sf, encryption_key=settings.encryption_key.encode("utf-8"))
        except Exception:
            logger.warning("[channels] Invalid encryption_key; credential vault disabled")

    client = PlatformClient(
        base_url=settings.platform_api_url,
        token=settings.platform_api_token,
    )
    cache = build_channel_routing_cache(settings=settings, platform_client=client)
    mate_cache = build_mate_settings_cache(settings=settings, platform_client=client)

    delivery_service = DeliveryService(session_factory=sf, redis_client=redis)

    from surogates.storage.backend import create_backend
    storage = create_backend(settings)

    app, delivery_dispatcher, reconciler = build_channels_app(
        settings,
        redis=redis,
        session_factory=sf,
        vault=vault,
        platform_client=client,
        cache=cache,
        delivery_service=delivery_service,
        session_store=session_store,
        mate_settings_cache=mate_cache,
        storage=storage,
    )
    # Routing / mate-settings caches age out within their TTL (30 s) without a
    # restart.  The per-message identity cache, however, negative-caches an
    # 'unknown sender' so a just-linked user would stay unrecognized for the
    # whole TTL — so we DO run an invalidator here, wired to the identity caches,
    # and link_channel publishes ``channel_identity_changed`` on bind to evict
    # that entry at once.
    from surogates.runtime import run_invalidator

    # Determine which platforms to run delivery loops for.
    from surogates.channels.registry import registry as _registry
    enabled = _registry.enabled_platforms(settings)
    if kind is not None:
        enabled = [p for p in enabled if p.kind == kind]

    # Background tasks.
    tasks: list[asyncio.Task] = []

    invalidator_task = asyncio.create_task(
        run_invalidator(
            redis,
            channel_identity_cache=_MultiCache(app.state.channel_identity_caches),
        ),
        name="channel-identity-invalidator",
    )
    tasks.append(invalidator_task)

    for platform in enabled:
        task = asyncio.create_task(
            delivery_dispatcher._delivery_loop(platform),
            name=f"channel-delivery-{platform.kind}",
        )
        tasks.append(task)

    reconciler_task = asyncio.create_task(
        reconciler.run(redis),
        name="channel-reconciler",
    )
    tasks.append(reconciler_task)

    # Serve via uvicorn.
    channels_settings = getattr(settings, "channels", None)
    port = getattr(channels_settings, "port", 8001)

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()

    def _handle_signal():
        logger.info("[channels] Shutdown signal received")
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        await server.serve()
    finally:
        logger.info("[channels] Cancelling background tasks")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await redis.aclose()
        await engine.dispose()
        logger.info("[channels] Shutdown complete")
