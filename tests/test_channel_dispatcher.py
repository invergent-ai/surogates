"""Tests for surogates.channels.dispatcher.ChannelWebhookDispatcher.

Covers the security-critical dispatch flow:
  a. build_app mounts one route per enabled webhook platform + interactive_paths
  b. malformed JSON body on a verified request → 400
  c. known identifier + failing verify → 401; pipeline NOT called
  d. known identifier + verified → parse then pipeline.handle called once with
     routing carrying resolved org_id/agent_id/platform and the resolved config
  e. unknown identifier → 200 fast-ack; NO credential lookup, NO parse, NO pipeline
  f. verify returning VerificationResult → that status + body returned; pipeline NOT called
  g. handle_non_message_update returning True → 200; pipeline NOT called
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from surogates.channels.dispatcher import ChannelWebhookDispatcher
from surogates.channels.inbound import InboundMessage, InboundOutcome, PipelineDeps
from surogates.channels.registry import ChannelDescriptor, ChannelRegistry, VerificationResult
from surogates.channels.base import SendResult


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ORG_ID = "org-11111111-1111-1111-1111-111111111111"
AGENT_ID = "agent-aaaaaaaaa"
IDENTIFIER = "FAKE_APP_ID"
UNKNOWN_IDENTIFIER = "NOT_PROVISIONED"

# Concrete URL paths for the path-based fake platforms.
KNOWN_URL = f"/channels/fake/{IDENTIFIER}"
UNKNOWN_URL = f"/channels/fake/{UNKNOWN_IDENTIFIER}"
INTERACTIVE_KNOWN_URL = f"/channels/fake_interactive/{IDENTIFIER}"


# ---------------------------------------------------------------------------
# Fake platform
# ---------------------------------------------------------------------------


def _make_msg(**kw) -> InboundMessage:
    defaults = dict(
        kind="text",
        identifier=IDENTIFIER,
        thread_key=None,
        platform_user_id="U1",
        user_name="alice",
        text="hello",
        media_urls=[],
        media_types=[],
        is_dm=True,
        is_mention=False,
        ts="1000.0001",
        source={},
    )
    defaults.update(kw)
    return InboundMessage(**defaults)


class _FakePlatform:
    """Minimal PATH-based webhook ChannelPlatform for dispatcher tests.

    Models the production case: the workspace identifier lives in the URL path
    (``request.path_params``), NOT in the request body.  ``identifier_of`` is
    therefore safe to call with ``body=None`` and never touches the (untrusted)
    JSON body before the tenant is resolved + verified.
    """

    kind = "fake"
    topology = "webhook"
    descriptor = ChannelDescriptor(
        vault_refs=lambda ident: {"token": f"fake/{ident}/token"},
        config_keys=("fake_token",),
        webhook_registration="manual",
    )

    def __init__(self):
        self._verify_return: bool | VerificationResult = True
        self._parse_return: InboundMessage | None = _make_msg()
        self.parse_calls: list[Any] = []
        self.identifier_calls: list[Any] = []
        self.non_msg_return: bool = False

    def route_path(self, identifier=None) -> str:
        # Path-parameterised: FastAPI binds {app_id} into request.path_params.
        return "/channels/fake/{app_id}"

    def identifier_of(self, request, body) -> str:
        # Identifier comes from the PATH only; body must NOT be consulted.
        self.identifier_calls.append(body)
        assert body is None, "dispatcher must pass body=None for path-based platforms"
        return request.path_params["app_id"]

    def verify(self, request, body, *, creds) -> bool | VerificationResult:
        return self._verify_return

    def parse(self, body, *, creds=None, identifier=None) -> InboundMessage | None:
        self.parse_calls.append(body)
        return self._parse_return

    async def send(self, item, *, creds) -> SendResult:
        return SendResult(success=True)


class _FakePlatformWithInteractive(_FakePlatform):
    """Platform that declares interactive_paths and handle_non_message_update."""

    kind = "fake_interactive"
    interactive_paths = ("/channels/fake_interactive/{app_id}/actions",)

    def route_path(self, identifier=None) -> str:
        return "/channels/fake_interactive/{app_id}"

    async def handle_non_message_update(self, body, *, routing, creds, deps) -> bool:
        return self.non_msg_return


# ---------------------------------------------------------------------------
# Fake cache, vault, pipeline
# ---------------------------------------------------------------------------


class _FakeCache:
    """Routing cache: returns tenant for IDENTIFIER, None for everything else."""

    def __init__(self, data: dict | None = None) -> None:
        self._data: dict = data or {
            f"fake:{IDENTIFIER}": {
                "org_id": ORG_ID,
                "agent_id": AGENT_ID,
                "config": {"require_mention": False},
            },
            f"fake_interactive:{IDENTIFIER}": {
                "org_id": ORG_ID,
                "agent_id": AGENT_ID,
                "config": {"require_mention": False},
            },
        }
        self.calls: list[str] = []

    async def get(self, key: str) -> dict | None:
        self.calls.append(key)
        return self._data.get(key)


class _FakeVault:
    """Vault stub that records resolve_ref calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve_ref(self, ref: str, *, org_id: str) -> str | None:
        self.calls.append((ref, org_id))
        return "fake-token-value"


class _FakePipeline:
    """Records handle() calls for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._outcome = InboundOutcome.PROCESSED

    async def handle(self, msg, *, routing, config, deps) -> InboundOutcome:
        self.calls.append({
            "msg": msg,
            "routing": routing,
            "config": config,
            "deps": deps,
        })
        return self._outcome


# ---------------------------------------------------------------------------
# Settings stubs
# ---------------------------------------------------------------------------


class _FakeChannelCfg:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled


def _settings(enabled_kinds: set[str]) -> Any:
    return SimpleNamespace(
        channels={k: _FakeChannelCfg(enabled=k in enabled_kinds) for k in enabled_kinds},
    )


# ---------------------------------------------------------------------------
# Helpers to build the app under test
# ---------------------------------------------------------------------------


def _deps_factory(kind, routing, creds, platform) -> PipelineDeps:
    """Trivial deps_factory stub — returns a PipelineDeps with all-None fields."""
    async def _noop(*a, **kw):
        pass

    return PipelineDeps(
        session_store=None,
        redis=None,
        state=None,
        pairing=None,
        firehose_append=_noop,
        get_or_create_session=_noop,
        enqueue_session=_noop,
        resolve_identity=_noop,
        session_factory=None,
        pairing_sender=_noop,
    )


def _make_app(
    platform: _FakePlatform | None = None,
    cache: _FakeCache | None = None,
    vault: _FakeVault | None = None,
    pipeline: _FakePipeline | None = None,
    extra_kinds: set[str] | None = None,
) -> tuple[FastAPI, _FakePlatform, _FakeCache, _FakeVault, _FakePipeline]:
    platform = platform or _FakePlatform()
    cache = cache or _FakeCache()
    vault = vault or _FakeVault()
    pipeline = pipeline or _FakePipeline()

    reg = ChannelRegistry()
    reg.register(platform)

    kinds = {platform.kind} | (extra_kinds or set())
    settings = _settings(enabled_kinds=kinds)

    dispatcher = ChannelWebhookDispatcher(
        cache=cache,
        vault=vault,
        pipeline=pipeline,
        deps_factory=_deps_factory,
        settings=settings,
        registry=reg,
    )
    app = dispatcher.build_app()
    return app, platform, cache, vault, pipeline


async def _post(app: FastAPI, path: str, body: dict | None = None, raw: bytes | None = None):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as c:
        if raw is not None:
            return await c.post(
                path,
                content=raw,
                headers={"content-type": "application/json"},
            )
        return await c.post(path, json=body if body is not None else {"text": "hi"})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRoutesMounting:
    async def test_mounts_route_for_enabled_webhook_platform(self):
        """build_app registers POST {route_path()} for the enabled platform."""
        app, platform, *_ = _make_app()
        r = await _post(app, KNOWN_URL)
        # Any non-404 means the route was found
        assert r.status_code != 404

    async def test_mounts_interactive_path(self):
        """build_app also registers POST for each interactive_path."""
        platform = _FakePlatformWithInteractive()
        cache = _FakeCache(data={
            f"fake_interactive:{IDENTIFIER}": {
                "org_id": ORG_ID,
                "agent_id": AGENT_ID,
                "config": {},
            }
        })
        reg = ChannelRegistry()
        reg.register(platform)
        settings = _settings(enabled_kinds={"fake_interactive"})
        dispatcher = ChannelWebhookDispatcher(
            cache=cache,
            vault=_FakeVault(),
            pipeline=_FakePipeline(),
            deps_factory=_deps_factory,
            settings=settings,
            registry=reg,
        )
        app = dispatcher.build_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            r = await c.post(
                f"/channels/fake_interactive/{IDENTIFIER}/actions",
                json={"type": "block_actions"},
            )
        assert r.status_code != 404

    async def test_socket_platform_not_mounted(self):
        """Platforms with topology='socket' get no route."""

        class _SocketPlatform(_FakePlatform):
            kind = "socket_fake"
            topology = "socket"

        reg = ChannelRegistry()
        p = _SocketPlatform()
        reg.register(p)
        settings = _settings(enabled_kinds={"socket_fake"})
        dispatcher = ChannelWebhookDispatcher(
            cache=_FakeCache(data={}),
            vault=_FakeVault(),
            pipeline=_FakePipeline(),
            deps_factory=_deps_factory,
            settings=settings,
            registry=reg,
        )
        app = dispatcher.build_app()
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 404


class TestUnknownIdentifier:
    async def test_unknown_identifier_returns_200(self):
        """Unknown identifier → fast-ack 200."""
        app, _, cache, vault, pipeline = _make_app()
        r = await _post(app, UNKNOWN_URL)
        assert r.status_code == 200

    async def test_unknown_identifier_no_vault_call(self):
        """Unknown identifier → NO credential lookup."""
        app, _, cache, vault, pipeline = _make_app()
        await _post(app, UNKNOWN_URL)
        assert vault.calls == [], "vault must not be called for unknown identifier"

    async def test_unknown_identifier_no_pipeline_call(self):
        """Unknown identifier → pipeline NOT called."""
        app, _, cache, vault, pipeline = _make_app()
        await _post(app, UNKNOWN_URL)
        assert pipeline.calls == []

    async def test_unknown_identifier_no_parse_call(self):
        """Unknown identifier → parse NOT called."""
        app, platform, cache, vault, pipeline = _make_app()
        await _post(app, UNKNOWN_URL)
        assert platform.parse_calls == []

    async def test_unknown_identifier_garbage_body_returns_200_no_oracle(self):
        """Garbage body to an UNKNOWN identifier → 200 fast-ack (no 400/404 oracle).

        Security: the response must be identical to a clean unknown-identifier
        request so an attacker cannot probe which identifiers are provisioned by
        sending malformed bodies. The body is never parsed, the vault is never
        consulted, parse and pipeline never run.
        """
        app, platform, cache, vault, pipeline = _make_app()
        r = await _post(app, UNKNOWN_URL, raw=b"<<< not json at all >>>")
        assert r.status_code == 200
        assert vault.calls == []
        assert platform.parse_calls == []
        assert pipeline.calls == []
        # The body must never have been consulted for the identifier either.
        assert platform.identifier_calls == [None]


class TestFailedVerification:
    async def test_bad_signature_returns_401(self):
        """Known identifier + verify returns False → 401."""
        platform = _FakePlatform()
        platform._verify_return = False
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 401

    async def test_bad_signature_pipeline_not_called(self):
        """Known identifier + verify returns False → pipeline NOT called."""
        platform = _FakePlatform()
        platform._verify_return = False
        app, _, _, _, pipeline = _make_app(platform=platform)
        await _post(app, KNOWN_URL)
        assert pipeline.calls == []

    async def test_bad_signature_body_not_parsed(self):
        """verify failing → the body is never parsed (parse not called)."""
        platform = _FakePlatform()
        platform._verify_return = False
        app, _, _, _, pipeline = _make_app(platform=platform)
        await _post(app, KNOWN_URL)
        assert platform.parse_calls == []


class TestVerificationResult:
    async def test_handshake_returns_verification_result_status(self):
        """verify returns accepted VerificationResult → respond with that status_code."""
        platform = _FakePlatform()
        platform._verify_return = VerificationResult(
            accepted=True,
            response_body={"challenge": "abc123"},
            status_code=200,
        )
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 200

    async def test_handshake_returns_verification_result_body(self):
        """verify returns accepted VerificationResult → response body matches."""
        platform = _FakePlatform()
        platform._verify_return = VerificationResult(
            accepted=True,
            response_body={"challenge": "abc123"},
            status_code=200,
        )
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL)
        assert r.json() == {"challenge": "abc123"}

    async def test_handshake_pipeline_not_called(self):
        """verify returns accepted VerificationResult → pipeline NOT called."""
        platform = _FakePlatform()
        platform._verify_return = VerificationResult(
            accepted=True,
            response_body={"challenge": "abc123"},
            status_code=200,
        )
        app, _, _, _, pipeline = _make_app(platform=platform)
        await _post(app, KNOWN_URL)
        assert pipeline.calls == []

    async def test_rejected_verification_result_returns_401_not_status(self):
        """accepted=False VerificationResult → 401, regardless of its status_code.

        Security: a rejected handshake must NOT be silently 200'd just because the
        platform set status_code=200 on a VerificationResult. ``accepted`` is the
        authority; a False handshake is a 401.
        """
        platform = _FakePlatform()
        platform._verify_return = VerificationResult(
            accepted=False, response_body={"ok": False}, status_code=200,
        )
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 401
        assert pipeline.calls == []

    async def test_rejected_verification_result_403_also_401(self):
        """accepted=False with status_code=403 still collapses to 401."""
        platform = _FakePlatform()
        platform._verify_return = VerificationResult(accepted=False, status_code=403)
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 401
        assert pipeline.calls == []

    async def test_async_verify_is_awaited(self):
        """A future async verify (returns a coroutine) must be awaited, not treated truthy.

        Defensive: a coroutine object is truthy, so without an isawaitable guard a
        rejecting async verify would sail past the falsy check and let the pipeline
        run. Here the async verify resolves to False → must 401, pipeline not called.
        """
        platform = _FakePlatform()

        async def _async_verify(request, body, *, creds):
            return False

        platform.verify = _async_verify
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 401
        assert pipeline.calls == []

    async def test_async_verify_true_continues(self):
        """An async verify resolving to True is awaited and the pipeline runs."""
        platform = _FakePlatform()

        async def _async_verify(request, body, *, creds):
            return True

        platform.verify = _async_verify
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 200
        assert len(pipeline.calls) == 1


class TestHappyPath:
    async def test_verified_request_calls_pipeline_once(self):
        """Known identifier + verified + valid parse → pipeline.handle called once."""
        app, platform, _, _, pipeline = _make_app()
        await _post(app, KNOWN_URL)
        assert len(pipeline.calls) == 1

    async def test_verified_request_returns_200(self):
        """Known identifier + verified → 200."""
        app, *_ = _make_app()
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 200

    async def test_routing_object_has_org_id(self):
        """Routing object passed to pipeline has resolved org_id."""
        app, _, _, _, pipeline = _make_app()
        await _post(app, KNOWN_URL)
        call = pipeline.calls[0]
        assert call["routing"].org_id == ORG_ID

    async def test_routing_object_has_agent_id(self):
        """Routing object passed to pipeline has resolved agent_id."""
        app, _, _, _, pipeline = _make_app()
        await _post(app, KNOWN_URL)
        call = pipeline.calls[0]
        assert call["routing"].agent_id == AGENT_ID

    async def test_routing_object_has_platform(self):
        """Routing object passed to pipeline carries the platform kind."""
        app, platform, _, _, pipeline = _make_app()
        await _post(app, KNOWN_URL)
        call = pipeline.calls[0]
        assert call["routing"].platform == platform.kind

    async def test_config_passed_to_pipeline(self):
        """Resolved config is forwarded to pipeline.handle."""
        app, _, _, _, pipeline = _make_app()
        await _post(app, KNOWN_URL)
        call = pipeline.calls[0]
        assert call["config"] == {"require_mention": False}

    async def test_vault_called_exactly_once_for_known_identifier(self):
        """Credential lookup happens EXACTLY once for a known identifier.

        An exact count (not >= 1) guards against an accidental double-resolution
        of credentials per request.
        """
        app, _, _, vault, _ = _make_app()
        await _post(app, KNOWN_URL)
        assert len(vault.calls) == 1

    async def test_routing_object_carries_path_identifier(self):
        """Routing object passed to the pipeline carries the path identifier (app_id).

        The delivery loop uses routing.identifier to key resolve_tenant so it can
        look up credentials for the bot-token.  The cache is keyed by the routing/app
        identifier (e.g. Slack app_id), NOT by the chat/channel id embedded in the
        message body.  This test pins that the dispatcher puts the identifier it
        extracted from the URL path into routing.identifier.
        """
        app, _, _, _, pipeline = _make_app()
        await _post(app, KNOWN_URL)
        call = pipeline.calls[0]
        assert hasattr(call["routing"], "identifier"), (
            "routing object must have an 'identifier' attribute"
        )
        assert call["routing"].identifier == IDENTIFIER, (
            f"routing.identifier must be the path identifier ({IDENTIFIER!r}), "
            f"got {call['routing'].identifier!r}"
        )


class TestMalformedBody:
    async def test_malformed_body_after_verification_returns_400(self):
        """Known + verified request with a malformed (non-JSON) body → 400.

        The body is only parsed AFTER verify passes; an authenticated sender that
        posts garbage gets a genuine 400 (no oracle concern — the identifier is
        already known and the request is verified).
        """
        app, platform, _, _, pipeline = _make_app()
        r = await _post(app, KNOWN_URL, raw=b"<<< not json >>>")
        assert r.status_code == 400
        # parse never runs because the body never decoded.
        assert platform.parse_calls == []
        assert pipeline.calls == []

    async def test_unparseable_message_after_verification_returns_400(self):
        """Verified request with valid JSON but platform.parse raises → 400."""
        platform = _FakePlatform()

        def _bad_parse(body, *, creds=None):
            raise ValueError("Cannot parse message")

        platform.parse = _bad_parse
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL, body={"garbage": True})
        assert r.status_code == 400
        assert pipeline.calls == []


class TestParseReturnsNone:
    async def test_parse_none_returns_200_no_pipeline(self):
        """parse returning None → 200, pipeline NOT called."""
        platform = _FakePlatform()
        platform._parse_return = None
        app, _, _, _, pipeline = _make_app(platform=platform)
        r = await _post(app, KNOWN_URL)
        assert r.status_code == 200
        assert pipeline.calls == []


class TestHandleNonMessageUpdate:
    async def test_non_message_update_true_returns_200_no_pipeline(self):
        """handle_non_message_update returning True → 200, pipeline NOT called."""
        platform = _FakePlatformWithInteractive()
        platform.non_msg_return = True
        cache = _FakeCache(data={
            f"fake_interactive:{IDENTIFIER}": {
                "org_id": ORG_ID,
                "agent_id": AGENT_ID,
                "config": {},
            }
        })
        reg = ChannelRegistry()
        reg.register(platform)
        settings = _settings(enabled_kinds={"fake_interactive"})
        pipeline = _FakePipeline()
        dispatcher = ChannelWebhookDispatcher(
            cache=cache,
            vault=_FakeVault(),
            pipeline=pipeline,
            deps_factory=_deps_factory,
            settings=settings,
            registry=reg,
        )
        app = dispatcher.build_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            r = await c.post(
                INTERACTIVE_KNOWN_URL,
                json={"type": "callback"},
            )
        assert r.status_code == 200
        assert pipeline.calls == []

    async def test_non_message_update_false_falls_through_to_pipeline(self):
        """handle_non_message_update returning False → pipeline IS called."""
        platform = _FakePlatformWithInteractive()
        platform.non_msg_return = False  # fall through
        cache = _FakeCache(data={
            f"fake_interactive:{IDENTIFIER}": {
                "org_id": ORG_ID,
                "agent_id": AGENT_ID,
                "config": {},
            }
        })
        reg = ChannelRegistry()
        reg.register(platform)
        settings = _settings(enabled_kinds={"fake_interactive"})
        pipeline = _FakePipeline()
        dispatcher = ChannelWebhookDispatcher(
            cache=cache,
            vault=_FakeVault(),
            pipeline=pipeline,
            deps_factory=_deps_factory,
            settings=settings,
            registry=reg,
        )
        app = dispatcher.build_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            r = await c.post(
                INTERACTIVE_KNOWN_URL,
                json={"text": "hello"},
            )
        assert r.status_code == 200
        assert len(pipeline.calls) == 1


# ---------------------------------------------------------------------------
# Enrich hook
# ---------------------------------------------------------------------------


class _FakePlatformWithEnrich(_FakePlatform):
    """Platform that declares an async enrich hook."""

    kind = "fake_enrich"

    def route_path(self, identifier=None) -> str:
        return "/channels/fake_enrich/{app_id}"

    def __init__(self):
        super().__init__()
        self.enrich_calls: list[Any] = []
        self._enriched_msg: InboundMessage | None = None
        self._enrich_raises: bool = False

    async def enrich(self, msg: InboundMessage, *, creds: dict) -> InboundMessage:
        self.enrich_calls.append((msg, creds))
        if self._enrich_raises:
            raise RuntimeError("enrich blew up")
        # Return the pre-configured enriched message, or a modified copy.
        if self._enriched_msg is not None:
            return self._enriched_msg
        import dataclasses
        return dataclasses.replace(msg, user_name="enriched_user")


class TestEnrichHook:
    """dispatcher calls platform.enrich when present, passes result to pipeline."""

    def _make_app_with_enrich(
        self,
        platform: _FakePlatformWithEnrich | None = None,
    ) -> tuple[Any, _FakePlatformWithEnrich, _FakeCache, _FakeVault, _FakePipeline]:
        platform = platform or _FakePlatformWithEnrich()
        cache = _FakeCache(data={
            f"fake_enrich:{IDENTIFIER}": {
                "org_id": ORG_ID,
                "agent_id": AGENT_ID,
                "config": {"require_mention": False},
            }
        })
        vault = _FakeVault()
        pipeline = _FakePipeline()
        reg = ChannelRegistry()
        reg.register(platform)
        settings = _settings(enabled_kinds={"fake_enrich"})
        dispatcher = ChannelWebhookDispatcher(
            cache=cache,
            vault=vault,
            pipeline=pipeline,
            deps_factory=_deps_factory,
            settings=settings,
            registry=reg,
        )
        app = dispatcher.build_app()
        return app, platform, cache, vault, pipeline

    async def test_enrich_is_called_when_platform_has_enrich(self):
        """When platform.enrich exists, dispatcher calls it before pipeline."""
        app, platform, _, _, pipeline = self._make_app_with_enrich()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            r = await c.post(f"/channels/fake_enrich/{IDENTIFIER}", json={"text": "hi"})

        assert r.status_code == 200
        assert len(platform.enrich_calls) == 1

    async def test_enriched_message_reaches_pipeline(self):
        """pipeline.handle receives the enriched message returned by enrich."""
        platform = _FakePlatformWithEnrich()
        enriched = _make_msg(user_name="enriched_user")
        platform._enriched_msg = enriched
        app, platform, _, _, pipeline = self._make_app_with_enrich(platform=platform)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            await c.post(f"/channels/fake_enrich/{IDENTIFIER}", json={"text": "hi"})

        assert len(pipeline.calls) == 1
        assert pipeline.calls[0]["msg"].user_name == "enriched_user"

    async def test_platform_without_enrich_still_works(self):
        """A platform without enrich has its parse result forwarded directly."""
        app, platform, _, _, pipeline = _make_app()  # uses _FakePlatform (no enrich)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            r = await c.post(KNOWN_URL, json={"text": "hi"})

        assert r.status_code == 200
        assert len(pipeline.calls) == 1
        # Original parse_return user_name should reach pipeline unchanged.
        assert pipeline.calls[0]["msg"].user_name == "alice"

    async def test_enrich_called_with_creds(self):
        """enrich receives the resolved creds dict."""
        app, platform, _, vault, _ = self._make_app_with_enrich()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            await c.post(f"/channels/fake_enrich/{IDENTIFIER}", json={"text": "hi"})

        assert len(platform.enrich_calls) == 1
        _, enrich_creds = platform.enrich_calls[0]
        # Creds must be a dict (vault resolves them to fake-token-value).
        assert isinstance(enrich_creds, dict)

    async def test_enrich_raising_does_not_drop_message(self):
        """An enrich that RAISES must NOT drop the message.

        The dispatcher logs the error and falls through with the UNENRICHED
        message. Guards the ``except Exception`` fall-through in the dispatcher's
        enrich step against future refactors.
        """
        platform = _FakePlatformWithEnrich()
        # parse returns the default fake message (user_name="alice").
        platform._parse_return = _make_msg(user_name="alice")
        platform._enrich_raises = True
        app, platform, _, _, pipeline = self._make_app_with_enrich(platform=platform)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            r = await c.post(f"/channels/fake_enrich/{IDENTIFIER}", json={"text": "hi"})

        # The request still succeeds and the pipeline runs exactly once.
        assert r.status_code == 200
        assert len(platform.enrich_calls) == 1
        assert len(pipeline.calls) == 1
        # The message reaching the pipeline is the ORIGINAL (pre-enrich) one.
        assert pipeline.calls[0]["msg"].user_name == "alice"


# ---------------------------------------------------------------------------
# Slack interactive surface integration tests — form-encoded POSTs
# ---------------------------------------------------------------------------


import hashlib
import hmac
import time
import urllib.parse


SLACK_SIGNING_SECRET = "test_signing_secret_ABC123"
SLACK_APP_ID = "A0TESTAPPID"


def _slack_sig(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Compute the X-Slack-Signature for a given raw body."""
    basestring = b"v0:" + timestamp.encode("ascii") + b":" + raw_body
    mac = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256)
    return f"v0={mac.hexdigest()}"


def _slash_form_body(
    *,
    text: str = "hello",
    channel_id: str = "D100",
    user_id: str = "U42",
    team_id: str = "T999",
    command: str = "/surogates",
) -> bytes:
    """Return a URL-encoded slash command body as bytes."""
    data = {
        "command": command,
        "text": text,
        "channel_id": channel_id,
        "user_id": user_id,
        "team_id": team_id,
    }
    return urllib.parse.urlencode(data).encode("utf-8")


def _interact_form_body(*, action_id: str = "surogates_approve_once") -> bytes:
    """Return a URL-encoded block_actions body as bytes."""
    import json as _json
    payload = {
        "type": "block_actions",
        "actions": [{"action_id": action_id}],
    }
    data = {"payload": _json.dumps(payload)}
    return urllib.parse.urlencode(data).encode("utf-8")


def _slack_headers(raw_body: bytes, secret: str = SLACK_SIGNING_SECRET) -> dict:
    ts = str(int(time.time()))
    sig = _slack_sig(secret, ts, raw_body)
    return {
        "x-slack-request-timestamp": ts,
        "x-slack-signature": sig,
        "content-type": "application/x-www-form-urlencoded",
    }


def _make_slack_dispatcher() -> tuple:
    """Return (app, pipeline, cache) wired with the real SlackPlatform."""
    from surogates.channels.platforms import slack as _slack_mod  # noqa: F401
    from surogates.channels.registry import registry as _default_registry

    pipeline = _FakePipeline()
    vault = _FakeVault()
    # Override vault to return the test signing secret.
    vault._secret = SLACK_SIGNING_SECRET

    async def _vault_resolve(ref, *, org_id):
        # ref format: vault://slack_<cred>_<identifier>
        if "signing_secret" in ref:
            return SLACK_SIGNING_SECRET
        return "xoxb-test-token"

    vault.resolve_ref = _vault_resolve

    slack_platform = _default_registry.get("slack")

    # Per-test isolated registry so we don't conflict with the module singleton.
    from surogates.channels.registry import ChannelRegistry
    reg = ChannelRegistry()
    # Re-register a fresh SlackPlatform to avoid shared auth.test cache state.
    from surogates.channels.platforms.slack import SlackPlatform
    reg.register(SlackPlatform())

    cache_data = {
        f"slack:{SLACK_APP_ID}": {
            "org_id": ORG_ID,
            "agent_id": AGENT_ID,
            "config": {},
        }
    }
    cache = _FakeCache(data=cache_data)

    from types import SimpleNamespace
    settings = SimpleNamespace(
        channels={"slack": SimpleNamespace(enabled=True)}
    )

    from surogates.channels.dispatcher import ChannelWebhookDispatcher
    dispatcher = ChannelWebhookDispatcher(
        cache=cache,
        vault=vault,
        pipeline=pipeline,
        deps_factory=_deps_factory,
        settings=settings,
        registry=reg,
    )
    app = dispatcher.build_app()
    return app, pipeline, cache


class TestSlackInteractiveSurfaces:
    """Integration tests: signed form-encoded POSTs to Slack interactive routes."""

    async def _post_form(self, app, path: str, raw_body: bytes, headers: dict):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            return await c.post(path, content=raw_body, headers=headers)

    async def test_slash_with_text_calls_pipeline_once(self):
        """Signed slash POST with text → pipeline.handle called once."""
        app, pipeline, _ = _make_slack_dispatcher()
        raw_body = _slash_form_body(text="hello")
        headers = _slack_headers(raw_body)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
        ) as mock_cls:
            mock_client = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
            mock_client.auth_test.return_value = {"user_id": "UBOTID"}
            mock_client.users_info.return_value = {
                "user": {"profile": {"display_name": "Alice"}, "name": "alice"}
            }
            mock_cls.return_value = mock_client

            r = await self._post_form(
                app, f"/slack/{SLACK_APP_ID}/commands", raw_body, headers
            )

        assert r.status_code == 200
        assert len(pipeline.calls) == 1

    async def test_slash_pipeline_message_is_dm_with_correct_text(self):
        """Slash command produces a synthetic InboundMessage(is_dm=True, text=<text>)."""
        app, pipeline, _ = _make_slack_dispatcher()
        raw_body = _slash_form_body(text="ask me something")
        headers = _slack_headers(raw_body)

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "surogates.channels.platforms.slack.AsyncWebClient",
        ) as mock_cls:
            mock_client = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
            mock_client.auth_test.return_value = {"user_id": "UBOTID"}
            mock_client.users_info.return_value = {
                "user": {"profile": {"display_name": "Alice"}, "name": "alice"}
            }
            mock_cls.return_value = mock_client

            await self._post_form(
                app, f"/slack/{SLACK_APP_ID}/commands", raw_body, headers
            )

        assert len(pipeline.calls) == 1
        msg = pipeline.calls[0]["msg"]
        assert msg.text == "ask me something"
        assert msg.is_dm is True

    async def test_slash_empty_text_returns_200_pipeline_not_called(self):
        """Empty slash text → 200 with usage message; pipeline NOT called."""
        app, pipeline, _ = _make_slack_dispatcher()
        raw_body = _slash_form_body(text="")
        headers = _slack_headers(raw_body)

        r = await self._post_form(
            app, f"/slack/{SLACK_APP_ID}/commands", raw_body, headers
        )

        assert r.status_code == 200
        assert pipeline.calls == []

    async def test_interact_returns_200_pipeline_not_called(self):
        """Signed block_actions POST → 200 ack; pipeline NOT called."""
        app, pipeline, _ = _make_slack_dispatcher()
        raw_body = _interact_form_body()
        headers = _slack_headers(raw_body)

        r = await self._post_form(
            app, f"/slack/{SLACK_APP_ID}/interact", raw_body, headers
        )

        assert r.status_code == 200
        assert pipeline.calls == []

    async def test_bad_signature_slash_returns_401(self):
        """Bad signing secret on slash command → 401."""
        app, pipeline, _ = _make_slack_dispatcher()
        raw_body = _slash_form_body(text="hello")
        # Sign with a wrong key.
        bad_headers = _slack_headers(raw_body, secret="wrong_secret")

        r = await self._post_form(
            app, f"/slack/{SLACK_APP_ID}/commands", raw_body, bad_headers
        )

        assert r.status_code == 401
        assert pipeline.calls == []

    async def test_bad_signature_interact_returns_401(self):
        """Bad signing secret on /interact → 401."""
        app, pipeline, _ = _make_slack_dispatcher()
        raw_body = _interact_form_body()
        bad_headers = _slack_headers(raw_body, secret="wrong_secret")

        r = await self._post_form(
            app, f"/slack/{SLACK_APP_ID}/interact", raw_body, bad_headers
        )

        assert r.status_code == 401
        assert pipeline.calls == []

    async def test_unknown_app_id_returns_200_fast_ack_pipeline_not_called(self):
        """Unknown app_id in path → 200 fast-ack; no creds, no pipeline."""
        app, pipeline, _ = _make_slack_dispatcher()
        raw_body = _slash_form_body(text="hello")
        headers = _slack_headers(raw_body)

        r = await self._post_form(
            app, "/slack/UNKNOWN_APP_ID/commands", raw_body, headers
        )

        assert r.status_code == 200
        assert pipeline.calls == []
