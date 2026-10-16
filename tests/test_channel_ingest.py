"""``ingest()``: the post-verify half webhooks and socket platforms share."""

from __future__ import annotations

from surogates.channels.dispatcher import ChannelWebhookDispatcher
from surogates.channels.registry import ChannelRegistry

from tests.test_channel_dispatcher import (
    AGENT_ID,
    IDENTIFIER,
    KNOWN_URL,
    ORG_ID,
    UNKNOWN_IDENTIFIER,
    _deps_factory,
    _FakeCache,
    _FakePipeline,
    _FakePlatform,
    _FakeVault,
    _post,
    _settings,
)


class _SocketPlatform(_FakePlatform):
    topology = "socket"


def _dispatcher(platform, cache=None, vault=None, pipeline=None):
    reg = ChannelRegistry()
    reg.register(platform)
    return ChannelWebhookDispatcher(
        cache=cache or _FakeCache(),
        vault=vault or _FakeVault(),
        pipeline=pipeline or _FakePipeline(),
        deps_factory=_deps_factory,
        settings=_settings({platform.kind}),
        registry=reg,
    )


async def test_a_socket_event_reaches_the_pipeline_with_the_resolved_routing():
    platform, pipeline, vault = _SocketPlatform(), _FakePipeline(), _FakeVault()
    d = _dispatcher(platform, vault=vault, pipeline=pipeline)

    status = await d.ingest_from_socket(platform, IDENTIFIER, {"t": "MESSAGE_CREATE"})

    assert status == 200
    assert platform.parse_calls == [{"t": "MESSAGE_CREATE"}]
    (call,) = pipeline.calls
    assert (call["routing"].org_id, call["routing"].agent_id) == (ORG_ID, AGENT_ID)
    assert call["routing"].identifier == IDENTIFIER
    assert call["config"] == {"require_mention": False}
    assert [org for _ref, org in vault.calls] == [ORG_ID]


async def test_a_socket_event_for_an_unknown_bot_has_no_side_effects():
    platform, pipeline, vault = _SocketPlatform(), _FakePipeline(), _FakeVault()
    d = _dispatcher(platform, vault=vault, pipeline=pipeline)

    assert await d.ingest_from_socket(platform, UNKNOWN_IDENTIFIER, {}) == 200
    assert vault.calls == [] and platform.parse_calls == [] and pipeline.calls == []


async def test_parse_failure_is_a_400_on_both_paths():
    class _Broken(_SocketPlatform):
        def parse(self, body, *, creds=None, identifier=None):
            raise ValueError("bad")

    platform = _Broken()
    d = _dispatcher(platform)
    assert await d.ingest_from_socket(platform, IDENTIFIER, {}) == 400

    webhook = _Broken()
    webhook.topology = "webhook"
    app = _dispatcher(webhook).build_app()
    assert (await _post(app, KNOWN_URL)).status_code == 400


async def test_socket_platforms_get_no_webhook_route():
    app = _dispatcher(_SocketPlatform()).build_app()
    assert (await _post(app, KNOWN_URL)).status_code == 404
