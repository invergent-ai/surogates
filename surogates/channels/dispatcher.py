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
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from surogates.channels.credentials import resolve_channel_credentials
from surogates.channels.inbound import ChannelInboundPipeline, PipelineDeps
from surogates.channels.registry import ChannelPlatform, ChannelRegistry, VerificationResult
from surogates.channels.resolve import resolve_tenant

__all__ = ["ChannelWebhookDispatcher", "ChannelDeliveryDispatcher"]

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
        """Register POST routes for *platform*'s main path and interactive_paths."""
        paths: list[str] = [platform.route_path()]
        interactive = getattr(platform, "interactive_paths", ())
        paths.extend(interactive)

        handler = self._make_handler(platform)
        for path in paths:
            app.add_api_route(path, handler, methods=["POST"])

    # ------------------------------------------------------------------
    # Per-platform handler factory
    # ------------------------------------------------------------------

    def _make_handler(self, platform: ChannelPlatform):
        """Return an async FastAPI route handler closed over *platform*."""
        cache = self._cache
        vault = self._vault
        pipeline = self._pipeline
        deps_factory = self._deps_factory

        async def _handler(request: Request) -> Response:
            # ----------------------------------------------------------------
            # Step 1: Read raw bytes (verbatim, for signature verification).
            # The JSON body is deliberately NOT parsed here — see step 2.
            # ----------------------------------------------------------------
            raw_bytes = await request.body()

            # ----------------------------------------------------------------
            # Step 2: Extract the workspace / channel identifier from the PATH.
            #
            # We pass body=None: our identifiers are path-based, so identifier_of
            # reads request.path_params and never touches the (untrusted) body.
            # Parsing the body before the tenant is resolved would (a) leak a
            # liveness oracle via a 400 on malformed JSON and (b) run untrusted
            # input through the JSON parser for unprovisioned identifiers. If the
            # path is malformed and identifier_of raises, treat it as an unknown
            # identifier (fast-ack 200 in step 3), NOT a 400.
            # ----------------------------------------------------------------
            try:
                identifier = platform.identifier_of(request, None)
            except Exception:
                logger.debug(
                    "[dispatcher] identifier_of raised on %s — treating as unknown, acking 200",
                    platform.kind, exc_info=True,
                )
                return Response(status_code=200)

            # ----------------------------------------------------------------
            # Step 3: Resolve tenant.  Unknown identifier → fast-ack 200 with
            # ZERO side effects (no creds, no body parse, no verify, no pipeline).
            # ----------------------------------------------------------------
            resolved = await resolve_tenant(cache, platform.kind, identifier)
            if resolved is None:
                logger.debug(
                    "[dispatcher] %s:%s — unknown identifier, acking 200",
                    platform.kind, identifier,
                )
                return Response(status_code=200)

            org_id: str = resolved["org_id"]
            agent_id: str = resolved["agent_id"]
            config: dict = resolved.get("config") or {}

            # ----------------------------------------------------------------
            # Step 4: Resolve credentials (only after a KNOWN identifier).
            # ----------------------------------------------------------------
            creds = await resolve_channel_credentials(
                vault=vault,
                kind=platform.kind,
                identifier=identifier,
                org_id=org_id,
                refs=platform.descriptor.vault_refs(identifier),
            )

            # ----------------------------------------------------------------
            # Step 5: Verify over the raw bytes.  MUST pass before the body is
            # parsed or the pipeline runs.
            #
            # verify is synchronous by contract (HMAC/secret compare over already-
            # resolved creds, no I/O).  We await defensively so a future async
            # impl can't silently bypass verification (a returned coroutine is
            # truthy and would otherwise sail past the falsy check below).
            # ----------------------------------------------------------------
            try:
                v = platform.verify(request, raw_bytes, creds=creds)
                if inspect.isawaitable(v):
                    v = await v
            except Exception:
                logger.warning(
                    "[dispatcher] verify raised on %s:%s", platform.kind, identifier,
                    exc_info=True,
                )
                return Response(status_code=401)

            if isinstance(v, VerificationResult):
                # Handshake / challenge path. Honour ``accepted``: only emit the
                # prescribed response when the handshake was actually accepted;
                # a rejected handshake is a 401 regardless of its status_code.
                if not v.accepted:
                    logger.info(
                        "[dispatcher] %s:%s — handshake rejected, returning 401",
                        platform.kind, identifier,
                    )
                    return Response(status_code=401)
                if v.response_body is None:
                    return Response(status_code=v.status_code)
                if isinstance(v.response_body, dict):
                    return JSONResponse(content=v.response_body, status_code=v.status_code)
                return PlainTextResponse(content=str(v.response_body), status_code=v.status_code)

            if not v:
                logger.info(
                    "[dispatcher] %s:%s — verification failed, returning 401",
                    platform.kind, identifier,
                )
                return Response(status_code=401)

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
                    platform.kind, identifier,
                )
                return Response(status_code=400)

            # ----------------------------------------------------------------
            # Build routing object (used from step 7 onward).
            # ----------------------------------------------------------------
            routing = _RoutingObject(org_id=org_id, agent_id=agent_id, platform=platform.kind)

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
                        platform.kind, identifier, exc_info=True,
                    )
                    return Response(status_code=200)
                if handled:
                    return Response(status_code=200)
                # Fall through — still need deps for pipeline below.
            else:
                deps = deps_factory(platform.kind, routing, creds, platform)

            # ----------------------------------------------------------------
            # Step 8: Parse into a normalised message.
            # ----------------------------------------------------------------
            try:
                msg = platform.parse(body)
            except Exception:
                logger.warning(
                    "[dispatcher] parse raised on %s:%s", platform.kind, identifier,
                    exc_info=True,
                )
                return Response(status_code=400)

            if msg is None:
                # Non-message event (reaction, edit, etc.) — ack and move on.
                return Response(status_code=200)

            # ----------------------------------------------------------------
            # Step 9: Run inbound pipeline.
            # ----------------------------------------------------------------
            await pipeline.handle(msg, routing=routing, config=config, deps=deps)
            return Response(status_code=200)

        # Assign a unique name so FastAPI doesn't complain about duplicate routes.
        _handler.__name__ = f"_dispatch_{platform.kind}"
        return _handler


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
    ) -> None:
        self._cache = cache
        self._vault = vault
        self._delivery = delivery_service

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
        else:
            error = result.error or "send failed"
            await self._delivery.mark_failed(item.id, error)
