"""ChannelWebhookDispatcher — FastAPI app that receives and routes inbound webhooks.

Security-critical ordering for every request:
  1. Read raw bytes (needed verbatim for HMAC / signature verification).
  2. identifier_of(request, None) — identifier comes from the PATH; the body is
     NOT parsed yet (parsing untrusted JSON before tenant resolution would leak a
     liveness oracle). If identifier_of raises (bad path) → treat as unknown.
  3. resolve_tenant → if None (or step 2 raised), return 200 fast-ack immediately
     with ZERO side effects: no credential lookup, no body parse, no verify, no
     pipeline. A garbage body to an unknown identifier must therefore also 200 —
     we never reveal which identifiers are provisioned via a 400/404 oracle.
  4. resolve_channel_credentials (requires org_id from step 3).
  5. verify(request, raw_bytes, creds=creds) (sync; awaited defensively if a future
     impl returns a coroutine):
       - VerificationResult: honour ``accepted`` — if True return status_code +
         response_body; if False return 401. No pipeline either way.
       - falsy          → return 401; pipeline NOT called.
       - True           → continue.
  6. Parse the JSON body — only now, after a known identifier + passing verify. A
     malformed body on a known + verified request → 400.
  7. handle_non_message_update (if the platform declares it) → if True, return 200.
  8. parse(body) → 400 on exception, 200 if None; otherwise:
  9. build routing object + deps; await pipeline.handle.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from surogates.channels.credentials import resolve_channel_credentials
from surogates.channels.inbound import ChannelInboundPipeline, InboundMessage, PipelineDeps
from surogates.channels.registry import ChannelPlatform, ChannelRegistry, VerificationResult
from surogates.channels.resolve import resolve_tenant

__all__ = ["ChannelWebhookDispatcher", "ChannelDeliveryDispatcher", "ChannelWebhookReconciler"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing object
# ---------------------------------------------------------------------------


@dataclass
class _RoutingObject:
    """Minimal routing carrier passed to the pipeline and deps_factory."""

    org_id: str
    agent_id: str
    platform: str
    identifier: str


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class ChannelWebhookDispatcher:
    """Receives inbound platform webhooks, verifies them, and routes to the pipeline.

    Constructor parameters
    ----------------------
    cache:
        ChannelRoutingCache (or any object with ``async get(key) -> dict | None``).
    vault:
        Credential vault with ``async resolve_ref(ref, *, org_id) -> str | None``.
    pipeline:
        :class:`~surogates.channels.inbound.ChannelInboundPipeline` instance.
    deps_factory:
        Callable ``(kind, routing_obj, creds, platform) -> PipelineDeps``.  Called
        once per verified message to build per-event dependencies (adapter state,
        pairing sender).  The other deps are long-lived runtime singletons and
        should be closed over by the factory.
    settings:
        Application settings; passed to ``registry.enabled_platforms(settings)`` to
        determine which platforms are active.
    registry:
        :class:`~surogates.channels.registry.ChannelRegistry` to query platforms
        from.  Defaults to the module-level singleton when not supplied.
    """

    def __init__(
        self,
        *,
        cache: Any,
        vault: Any,
        pipeline: Any,  # ChannelInboundPipeline or compatible
        deps_factory: Callable[[str, Any, dict, Any], PipelineDeps],
        settings: Any,
        registry: ChannelRegistry | None = None,
    ) -> None:
        if registry is None:
            from surogates.channels.registry import registry as _default_registry
            registry = _default_registry
        self._cache = cache
        self._vault = vault
        self._pipeline = pipeline
        self._deps_factory = deps_factory
        self._settings = settings
        self._registry = registry

    # ------------------------------------------------------------------
    # App factory
    # ------------------------------------------------------------------

    def build_app(self) -> FastAPI:
        """Build and return a FastAPI app with routes for all enabled webhook platforms."""
        app = FastAPI(title="Surogate Channel Webhook Dispatcher")
        platforms = self._registry.enabled_platforms(self._settings)
        for platform in platforms:
            if platform.topology != "webhook":
                continue
            self._mount_platform(app, platform)
        return app

    # ------------------------------------------------------------------
    # Route mounting
    # ------------------------------------------------------------------

    def _mount_platform(self, app: FastAPI, platform: ChannelPlatform) -> None:
        """Register POST routes for *platform*'s main path and interactive_paths.

        The main JSON Events route uses :meth:`_make_handler`.  Each path in
        ``interactive_paths`` uses :meth:`_make_interactive_handler` instead,
        which parses an ``application/x-www-form-urlencoded`` body and
        delegates to ``platform.handle_interactive``.  Both handlers share the
        same secure front-half (raw bytes → identifier → resolve_tenant →
        creds → verify) via :meth:`_resolve_and_verify`.
        """
        handler = self._make_handler(platform)
        app.add_api_route(platform.route_path(), handler, methods=["POST"])

        interactive = getattr(platform, "interactive_paths", ())
        if interactive:
            interactive_handler = self._make_interactive_handler(platform)
            for path in interactive:
                app.add_api_route(path, interactive_handler, methods=["POST"])

    # ------------------------------------------------------------------
    # Shared secure front-half
    # ------------------------------------------------------------------

    async def _resolve_and_verify(
        self,
        platform: ChannelPlatform,
        request: Request,
        raw_bytes: bytes,
    ):
        """Shared secure front-half for all routes on *platform*.

        Executes the security-critical ordering:
          1. Extract identifier from path (body is NOT consulted).
          2. Resolve tenant — unknown → ``None`` (caller should fast-ack 200).
          3. Resolve channel credentials.
          4. Verify over raw bytes — falsy or non-accepted VerificationResult
             → returns the appropriate :class:`Response` (caller should return it).

        Returns
        -------
        tuple[str, str, dict, dict, _RoutingObject, Response | None]
            ``(identifier, org_id, config, creds, routing, error_response)``

            * ``error_response`` is ``None`` when all steps passed.  When it
              is a :class:`Response`, the caller must return it immediately
              without further processing.
        """
        cache = self._cache
        vault = self._vault

        # Step 1: Extract identifier from path only (body=None).
        try:
            identifier = platform.identifier_of(request, None)
        except Exception:
            logger.debug(
                "[dispatcher] identifier_of raised on %s — treating as unknown, acking 200",
                platform.kind, exc_info=True,
            )
            return None, None, {}, {}, None, Response(status_code=200)

        # Step 2: Resolve tenant.
        resolved = await resolve_tenant(cache, platform.kind, identifier)
        if resolved is None:
            logger.debug(
                "[dispatcher] %s:%s — unknown identifier, acking 200",
                platform.kind, identifier,
            )
            return identifier, None, {}, {}, None, Response(status_code=200)

        org_id: str = resolved["org_id"]
        agent_id: str = resolved["agent_id"]
        config: dict = resolved.get("config") or {}

        # Step 3: Resolve credentials.
        creds = await resolve_channel_credentials(
            vault=vault,
            kind=platform.kind,
            identifier=identifier,
            org_id=org_id,
            refs=platform.descriptor.vault_refs(identifier),
        )

        # Step 4: Verify over raw bytes.
        try:
            v = platform.verify(request, raw_bytes, creds=creds)
            if inspect.isawaitable(v):
                v = await v
        except Exception:
            logger.warning(
                "[dispatcher] verify raised on %s:%s", platform.kind, identifier,
                exc_info=True,
            )
            return identifier, org_id, config, creds, None, Response(status_code=401)

        if isinstance(v, VerificationResult):
            if not v.accepted:
                logger.info(
                    "[dispatcher] %s:%s — handshake rejected, returning 401",
                    platform.kind, identifier,
                )
                return identifier, org_id, config, creds, None, Response(status_code=401)
            # Accepted handshake — return the prescribed response.
            if v.response_body is None:
                return identifier, org_id, config, creds, None, Response(status_code=v.status_code)
            if isinstance(v.response_body, dict):
                return (
                    identifier, org_id, config, creds, None,
                    JSONResponse(content=v.response_body, status_code=v.status_code),
                )
            return (
                identifier, org_id, config, creds, None,
                PlainTextResponse(content=str(v.response_body), status_code=v.status_code),
            )

        if not v:
            logger.info(
                "[dispatcher] %s:%s — verification failed, returning 401",
                platform.kind, identifier,
            )
            return identifier, org_id, config, creds, None, Response(status_code=401)

        routing = _RoutingObject(
            org_id=org_id,
            agent_id=agent_id,
            platform=platform.kind,
            identifier=identifier,
        )
        return identifier, org_id, config, creds, routing, None

    # ------------------------------------------------------------------
    # Per-platform handler factory
    # ------------------------------------------------------------------

    def _make_handler(self, platform: ChannelPlatform):
        """Return an async FastAPI route handler for JSON Events API requests.

        Implements the security-critical ordering documented at the top of this
        module.  The secure front-half (steps 1–5) is shared with the
        interactive handler via :meth:`_resolve_and_verify`.
        """
        pipeline = self._pipeline
        deps_factory = self._deps_factory
        self_ = self

        async def _handler(request: Request) -> Response:
            # ----------------------------------------------------------------
            # Steps 1–5: raw bytes → identifier → tenant → creds → verify.
            # Security: the body is never parsed before verify passes; an
            # unknown identifier gets a fast-ack 200 with zero side effects.
            # See module docstring for the full ordering contract.
            # ----------------------------------------------------------------
            raw_bytes = await request.body()
            _id, org_id, config, creds, routing, err = await self_._resolve_and_verify(
                platform, request, raw_bytes
            )
            if err is not None:
                return err

            # ----------------------------------------------------------------
            # Step 6: Parse the JSON body — only now, after a known identifier
            # and a passing verify.  A malformed body on a verified request is a
            # genuine 400 (the sender is authenticated, so the oracle concern of
            # step 2 no longer applies).
            # ----------------------------------------------------------------
            try:
                body: Any = await request.json()
            except Exception:
                logger.info(
                    "[dispatcher] %s:%s — malformed body on verified request, returning 400",
                    platform.kind, _id,
                )
                return Response(status_code=400)

            # ----------------------------------------------------------------
            # Step 7: Optional non-message update hook.
            # ----------------------------------------------------------------
            handle_nmu = getattr(platform, "handle_non_message_update", None)
            if handle_nmu is not None:
                deps = deps_factory(platform.kind, routing, creds, platform)
                try:
                    handled = await handle_nmu(body, routing=routing, creds=creds, deps=deps)
                except Exception:
                    logger.warning(
                        "[dispatcher] handle_non_message_update raised on %s:%s",
                        platform.kind, _id, exc_info=True,
                    )
                    return Response(status_code=200)
                if handled:
                    return Response(status_code=200)
                # Fall through — still need deps for pipeline below.
            else:
                deps = deps_factory(platform.kind, routing, creds, platform)

            # ----------------------------------------------------------------
            # Step 8: Parse into a normalised message.
            #
            # creds are forwarded as an optional keyword argument so platforms
            # that need async credential-dependent initialisation (e.g. Slack
            # auth.test for bot_user_id) can receive them without requiring a
            # separate pre-flight call.  Platforms whose parse does not accept
            # creds silently ignore the kwarg via their own signature.
            #
            # parse may return a coroutine (async platforms).  We await
            # defensively — same pattern used for verify above.
            # ----------------------------------------------------------------
            try:
                msg_or_coro = platform.parse(body, creds=creds)
                if inspect.isawaitable(msg_or_coro):
                    msg = await msg_or_coro
                else:
                    msg = msg_or_coro
            except Exception:
                logger.warning(
                    "[dispatcher] parse raised on %s:%s", platform.kind, _id,
                    exc_info=True,
                )
                return Response(status_code=400)

            if msg is None:
                # Non-message event (reaction, edit, etc.) — ack and move on.
                return Response(status_code=200)

            # ----------------------------------------------------------------
            # Step 8b: Optional enrich hook (e.g. async user name resolution).
            #
            # If the platform declares an ``enrich`` method, call it with the
            # parsed message and the resolved creds.  The enriched message
            # replaces the original before it reaches the pipeline.
            # ----------------------------------------------------------------
            enrich = getattr(platform, "enrich", None)
            if enrich is not None:
                try:
                    msg = await enrich(msg, creds=creds)
                except Exception:
                    logger.warning(
                        "[dispatcher] enrich raised on %s:%s — using unenriched message",
                        platform.kind, _id,
                        exc_info=True,
                    )

            # ----------------------------------------------------------------
            # Step 9: Run inbound pipeline.
            # ----------------------------------------------------------------
            await pipeline.handle(msg, routing=routing, config=config, deps=deps)
            return Response(status_code=200)

        # Assign a unique name so FastAPI doesn't complain about duplicate routes.
        _handler.__name__ = f"_dispatch_{platform.kind}"
        return _handler

    def _make_interactive_handler(self, platform: ChannelPlatform):
        """Return an async FastAPI route handler for form-encoded interactive requests.

        Used for interactive_paths (slash commands, button clicks).

        Security ordering mirrors the JSON Events handler:
          Steps 1–5 are shared via :meth:`_resolve_and_verify` (raw bytes →
          identifier → tenant → creds → verify).  Only after verify passes is
          the form body parsed (``application/x-www-form-urlencoded``).

        The handler delegates to ``platform.handle_interactive`` which returns
        one of:
          - :class:`~surogates.channels.inbound.InboundMessage` — forward
            through enrich + pipeline, return 200.
          - :class:`~fastapi.responses.Response` — return directly.
          - ``None`` — silent 200 ack.

        Only platforms that declare ``interactive_paths`` AND implement
        ``handle_interactive`` get this handler.  The dispatcher checks for the
        method via ``getattr``; platforms without it will hit this handler but
        return 200 silently (they declared the paths but forgot the method).
        """
        pipeline = self._pipeline
        deps_factory = self._deps_factory
        self_ = self

        async def _interactive_handler(request: Request) -> Response:
            # ----------------------------------------------------------------
            # Steps 1–5 (shared with JSON handler): raw bytes → identifier →
            # tenant → creds → verify.
            # ----------------------------------------------------------------
            raw_bytes = await request.body()
            _id, _org, config, creds, routing, err = await self_._resolve_and_verify(
                platform, request, raw_bytes
            )
            if err is not None:
                return err

            # ----------------------------------------------------------------
            # Step 6 (interactive): Parse the form body — only AFTER verify.
            # ----------------------------------------------------------------
            try:
                form: dict[str, str] = dict(
                    urllib.parse.parse_qsl(raw_bytes.decode("utf-8", errors="replace"))
                )
            except Exception:
                logger.info(
                    "[dispatcher] %s:%s — malformed form body on interactive route, returning 400",
                    platform.kind, _id,
                )
                return Response(status_code=400)

            # ----------------------------------------------------------------
            # Step 7: Delegate to the platform's interactive handler.
            # ----------------------------------------------------------------
            handle_interactive = getattr(platform, "handle_interactive", None)
            if handle_interactive is None:
                # Platform declared interactive_paths but no handler — ack 200.
                logger.debug(
                    "[dispatcher] %s:%s — no handle_interactive method, acking 200",
                    platform.kind, _id,
                )
                return Response(status_code=200)

            # Resolve the path template from the matched route so the platform
            # can dispatch on slash-vs-interact without inspecting the URL again.
            path_template: str = request.scope.get("path", "")
            # FastAPI stores the route pattern in scope["endpoint"] or via
            # request.scope["router"]; the cleanest portable approach is to use
            # the raw path and match against the declared interactive_paths.
            interactive_paths = getattr(platform, "interactive_paths", ())
            matched_template = path_template
            for tmpl in interactive_paths:
                # Convert path template to a simple prefix check by replacing
                # {param} placeholders with the actual path segment.
                # For Slack: "/slack/{app_id}/commands" → ends with "/commands"
                # This avoids importing starlette routing internals.
                suffix = tmpl.split("}")[-1]  # everything after last "}"
                if path_template.endswith(suffix):
                    matched_template = tmpl
                    break

            try:
                result = await handle_interactive(
                    matched_template,
                    form,
                    request=request,
                    creds=creds,
                    routing=routing,
                )
            except Exception:
                logger.warning(
                    "[dispatcher] handle_interactive raised on %s:%s",
                    platform.kind, _id, exc_info=True,
                )
                return Response(status_code=200)

            # ----------------------------------------------------------------
            # Step 8: Dispatch on result type.
            # ----------------------------------------------------------------
            if result is None:
                return Response(status_code=200)

            if isinstance(result, InboundMessage):
                # Run through enrich + pipeline just like the main handler.
                enrich = getattr(platform, "enrich", None)
                if enrich is not None:
                    try:
                        result = await enrich(result, creds=creds)
                    except Exception:
                        logger.warning(
                            "[dispatcher] enrich raised on interactive %s:%s — using unenriched",
                            platform.kind, _id, exc_info=True,
                        )

                deps = deps_factory(platform.kind, routing, creds, platform)
                await pipeline.handle(result, routing=routing, config=config, deps=deps)
                return Response(status_code=200)

            # FastAPI Response (or any Response subclass) — return directly.
            return result

        _interactive_handler.__name__ = f"_dispatch_interactive_{platform.kind}"
        return _interactive_handler


# ---------------------------------------------------------------------------
# Outbound delivery dispatcher
# ---------------------------------------------------------------------------


class ChannelDeliveryDispatcher:
    """Claims pending outbox items for a platform and delivers them via its API.

    Constructor parameters
    ----------------------
    cache:
        ChannelRoutingCache (or any object with ``async get(key) -> dict | None``).
        Used to resolve the tenant (org_id) for each outbox item's
        ``channel_identifier`` so credentials can be fetched from the vault.
    vault:
        Credential vault with ``async resolve_ref(ref, *, org_id) -> str | None``.
    delivery_service:
        :class:`~surogates.channels.delivery.DeliveryService` used to claim
        batches and mark items delivered or failed.
    """

    _BATCH_LIMIT = 20
    _SLEEP_EMPTY = 2.0
    _SLEEP_ERROR = 5.0

    def __init__(
        self,
        *,
        cache: Any,
        vault: Any,
        delivery_service: Any,
        redis: Any = None,
    ) -> None:
        self._cache = cache
        self._vault = vault
        self._delivery = delivery_service
        self._redis = redis

    # ------------------------------------------------------------------
    # Core batch method (testable; all per-item logic lives here)
    # ------------------------------------------------------------------

    async def deliver_batch(self, platform: ChannelPlatform) -> int:
        """Claim and deliver one batch of pending outbox items for *platform*.

        Isolation contract
        ------------------
        Each item is processed independently.  A failing send (either a
        :class:`~surogates.channels.base.SendResult` with ``success=False``
        or an unexpected exception) marks that specific item failed and
        continues to the next — one bad item never aborts the batch.

        Returns
        -------
        int
            The number of items processed in this batch (claimed count,
            regardless of success/failure).
        """
        worker_id = f"{platform.kind}-{os.getpid()}"
        items = await self._delivery.claim_batch(
            platform.kind, worker_id, limit=self._BATCH_LIMIT
        )

        for item in items:
            try:
                await self._deliver_item(platform, item)
            except Exception as exc:
                # Safety net: _deliver_item already catches per-item errors
                # internally, but guard the outer loop too.
                logger.exception(
                    "[delivery] Unexpected error processing outbox %d on %s",
                    item.id, platform.kind,
                )
                try:
                    await self._delivery.mark_failed(item.id, str(exc))
                except Exception:
                    pass

        return len(items)

    # ------------------------------------------------------------------
    # Long-running loop (thin; all logic in deliver_batch)
    # ------------------------------------------------------------------

    async def _delivery_loop(self, platform: ChannelPlatform) -> None:
        """Repeatedly call :meth:`deliver_batch` until cancelled.

        Empty batches are followed by a short sleep to avoid tight-polling
        the database.  The CLI (a separate task) starts one loop per enabled
        platform.
        """
        while True:
            try:
                n = await self.deliver_batch(platform)
                if not n:
                    await asyncio.sleep(self._SLEEP_EMPTY)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "[delivery] Loop error on %s — backing off %.1fs",
                    platform.kind, self._SLEEP_ERROR,
                )
                await asyncio.sleep(self._SLEEP_ERROR)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _deliver_item(self, platform: ChannelPlatform, item: Any) -> None:
        """Process a single outbox item: resolve creds and call platform.send."""
        # 1. Extract channel identifier.
        identifier: str = item.destination.get("channel_identifier", "")
        if not identifier:
            logger.warning(
                "[delivery] Outbox %d has no channel_identifier — marking failed",
                item.id,
            )
            await self._delivery.mark_failed(item.id, "missing channel_identifier")
            return

        # 2. Resolve tenant from the routing cache.
        resolved = await resolve_tenant(self._cache, platform.kind, identifier)
        if resolved is None:
            logger.warning(
                "[delivery] Outbox %d identifier %r not found in routing cache — "
                "channel may have been deprovisioned",
                item.id, identifier,
            )
            await self._delivery.mark_failed(
                item.id, f"channel deprovisioned: {identifier}"
            )
            return

        org_id: str = resolved["org_id"]

        # 3. Resolve credentials.
        creds = await resolve_channel_credentials(
            vault=self._vault,
            kind=platform.kind,
            identifier=identifier,
            org_id=org_id,
            refs=platform.descriptor.vault_refs(identifier),
        )

        # 4. Send via the platform, with per-item exception isolation.
        try:
            result = await platform.send(item, creds=creds)
        except Exception as exc:
            logger.error(
                "[delivery] platform.send raised for outbox %d (%s): %s",
                item.id, platform.kind, exc,
            )
            await self._delivery.mark_failed(item.id, str(exc))
            return

        if result.success:
            await self._delivery.mark_delivered(
                item.id, provider_message_id=result.message_id
            )
            # Mark the sent message (and its thread, if any) as bot-authored.
            # Best-effort: a Redis error here must NOT re-mark the item as failed.
            if self._redis is not None and result.message_id:
                try:
                    from surogates.channels.channel_state import ChannelAdapterState
                    agent_id: str = resolved.get("agent_id", "")
                    if agent_id:
                        state = ChannelAdapterState(self._redis, agent_id=agent_id, platform=platform.kind)
                        await state.mark_bot_message(result.message_id)
                        thread: str | None = item.destination.get("thread_ts") or item.destination.get("message_thread_id")
                        if thread:
                            await state.mark_bot_message(thread)
                except Exception:
                    logger.warning(
                        "[delivery] mark_bot_message failed for outbox %d — ignoring (best-effort)",
                        item.id,
                    )
        else:
            error = result.error or "send failed"
            await self._delivery.mark_failed(item.id, error)


# ---------------------------------------------------------------------------
# Webhook self-registration reconciler
# ---------------------------------------------------------------------------


class ChannelWebhookReconciler:
    """Registers (and re-registers) webhooks for ``api``-registration platforms.

    On startup :meth:`register_all` is called for each enabled platform whose
    ``descriptor.webhook_registration == "api"`` (e.g. Telegram).  Platforms
    with ``"manual"`` registration (e.g. Slack — the operator registers the
    URL in the developer console) are silently skipped.

    :meth:`run` then subscribes to ``channel_routing_changed:<kind>:<identifier>``
    Redis pub/sub messages and calls :meth:`handle_routing_change` for each so a
    newly-provisioned channel's webhook is registered without a full restart.

    Constructor parameters
    ----------------------
    platform_client:
        ``PlatformClient`` (or compatible) with an async
        ``list_channel_routings(kind) -> list[dict]`` method.
    vault:
        Credential vault with ``async resolve_ref(ref, *, org_id) -> str | None``.
    public_url:
        Base URL at which the channel dispatcher is reachable from the internet
        (e.g. ``"https://channels.surogate.ai"``).  Trailing slash is stripped
        before appending ``route_path``.
    settings:
        Application settings; passed to ``registry.enabled_platforms(settings)``
        to determine which platforms are active.
    registry:
        :class:`~surogates.channels.registry.ChannelRegistry` to query platforms
        from.  Defaults to the module-level singleton when not supplied.
    """

    def __init__(
        self,
        *,
        platform_client: Any,
        vault: Any,
        public_url: str,
        settings: Any,
        registry: ChannelRegistry | None = None,
    ) -> None:
        if registry is None:
            from surogates.channels.registry import registry as _default_registry
            registry = _default_registry
        self._platform_client = platform_client
        self._vault = vault
        self._public_url = public_url.rstrip("/")
        self._settings = settings
        self._registry = registry

    # ------------------------------------------------------------------
    # Core testable methods
    # ------------------------------------------------------------------

    async def register_all(self, platform: ChannelPlatform) -> None:
        """Register webhooks for every active routing of *platform*.

        Skips platforms with ``descriptor.webhook_registration != "api"``.
        Per-identifier failures are logged and skipped — they never abort
        the rest of the batch.
        """
        if platform.descriptor.webhook_registration != "api":
            return

        routings = await self._platform_client.list_channel_routings(platform.kind)
        for routing in routings:
            identifier: str = routing.get("channel_identifier", "")
            if not identifier:
                logger.warning(
                    "[reconcile] %s routing row missing channel_identifier — skipped",
                    platform.kind,
                )
                continue
            org_id: str = routing.get("org_id", "")
            try:
                await self._register_one(platform, identifier, org_id)
            except Exception:
                logger.exception(
                    "[reconcile] Failed to register webhook for %s:%s — skipping",
                    platform.kind, identifier,
                )

    async def handle_routing_change(self, kind: str, identifier: str) -> None:
        """Re-register the webhook for a single *kind*/*identifier* pair.

        Looks up the platform from the registry.  Silently returns if the
        platform is not enabled or not ``api``-registration.
        """
        platform = self._registry.get(kind)
        if platform is None:
            logger.debug(
                "[reconcile] routing_change for unknown kind %r — no platform registered",
                kind,
            )
            return
        if platform.descriptor.webhook_registration != "api":
            return

        # Fetch the current routing row so we have the org_id for vault lookup.
        routings = await self._platform_client.list_channel_routings(kind)
        routing = next(
            (r for r in routings if r.get("channel_identifier") == identifier),
            None,
        )
        if routing is None:
            logger.debug(
                "[reconcile] routing_change for %s:%s — no routing row found (deprovisioned?)",
                kind, identifier,
            )
            return

        org_id: str = routing.get("org_id", "")
        try:
            await self._register_one(platform, identifier, org_id)
        except Exception:
            logger.exception(
                "[reconcile] Failed to re-register webhook for %s:%s",
                kind, identifier,
            )

    # ------------------------------------------------------------------
    # Long-running pubsub loop (thin — all logic in the two methods above)
    # ------------------------------------------------------------------

    async def run(self, redis: Any) -> None:
        """Start-up loop: register all, then subscribe to routing changes.

        1. Calls :meth:`register_all` for every enabled ``api`` platform.
        2. Subscribes to ``channel_routing_changed:<kind>:<identifier>``
           pub/sub messages and calls :meth:`handle_routing_change` for each.

        Runs until cancelled (e.g. FastAPI lifespan shutdown).
        """
        enabled = [
            p for p in self._registry.enabled_platforms(self._settings)
            if p.descriptor.webhook_registration == "api"
        ]

        for platform in enabled:
            try:
                await self.register_all(platform)
            except Exception:
                logger.exception(
                    "[reconcile] register_all failed for %s — continuing",
                    platform.kind,
                )

        pubsub = redis.pubsub()
        try:
            await pubsub.psubscribe("channel_routing_changed:*")
            async for msg in pubsub.listen():
                if msg.get("type") != "pmessage":
                    continue
                channel = msg.get("channel") or ""
                if isinstance(channel, bytes):
                    channel = channel.decode()
                # channel name format: channel_routing_changed:<kind>:<identifier>
                # Strip the fixed prefix then split on the FIRST colon only so
                # identifiers that themselves contain colons (e.g. "telegram:@bot"
                # isn't our case, but guarded defensively) are handled correctly.
                suffix = channel.removeprefix("channel_routing_changed:")
                if ":" not in suffix:
                    continue
                kind, identifier = suffix.split(":", 1)
                if not kind or not identifier:
                    continue
                try:
                    await self.handle_routing_change(kind, identifier)
                except Exception:
                    logger.exception(
                        "[reconcile] handle_routing_change raised for %s:%s",
                        kind, identifier,
                    )
        finally:
            try:
                await pubsub.aclose()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _register_one(
        self, platform: ChannelPlatform, identifier: str, org_id: str,
    ) -> None:
        """Resolve creds and call ``platform.descriptor.register_webhook``."""
        creds = await resolve_channel_credentials(
            vault=self._vault,
            kind=platform.kind,
            identifier=identifier,
            org_id=org_id,
            refs=platform.descriptor.vault_refs(identifier),
        )
        url = self._public_url + platform.route_path(identifier)
        await platform.descriptor.register_webhook(identifier, url, creds)
