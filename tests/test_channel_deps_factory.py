"""Tests for the channels deps factory wiring (runner._make_deps_factory).

Verifies that the per-event factory selects the resolver by the channel's
identity_policy and wires the pairing producer (mint a code + privately deliver
the link prompt) for linked mode.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from surogates.channels.runner import _make_deps_factory, _MultiCache


class _FakeRedis:
    def __init__(self):
        self.kv = {}

    async def get(self, k):
        return self.kv.get(k)

    async def exists(self, k):
        return 1 if k in self.kv else 0

    async def setex(self, k, ttl, v):
        self.kv[k] = v

    async def getdel(self, k):
        return self.kv.pop(k, None)


class _RecordingPlatform:
    def __init__(self):
        self.sent = []

    async def send_private(self, creds, *, sender_id, chat_id, is_dm, text):
        self.sent.append({"sender_id": sender_id, "text": text})
        return True


def _routing(policy=None, api_web_url=""):
    config = {"identity_policy": policy} if policy else {}
    return SimpleNamespace(
        org_id="o1", agent_id="a1", platform="slack", identifier="A0", config=config,
        api_web_url=api_web_url,
    )


@pytest.mark.asyncio
async def test_factory_wires_pairing_producer_and_link_prompt():
    factory = _make_deps_factory(
        session_store=object(), redis=_FakeRedis(), session_factory=object(),
        link_url_base="https://studio.example",
    )
    plat = _RecordingPlatform()
    deps = factory("slack", _routing("linked"), {"bot_token": "x"}, plat)

    assert deps.pairing is not None
    assert deps.pairing_sender is not None

    msg = SimpleNamespace(platform_user_id="U1", identifier="C1", is_dm=True)
    delivered = await deps.pairing_sender("o1", "slack", msg, "CODE-123")

    assert delivered is True, "a successful private send reports delivered"
    assert plat.sent and plat.sent[0]["sender_id"] == "U1"
    assert "CODE-123" in plat.sent[0]["text"]
    assert "https://studio.example/link" in plat.sent[0]["text"]


@pytest.mark.asyncio
async def test_link_prompt_prefers_the_per_agent_api_web_url():
    """The link must point at the agent's own host, not a shared one.

    Login resolves the org from the Host subdomain, so a code redeemed on a
    generic host authenticates against the wrong org and the user can never
    sign in.  ``api_web_url`` is the agent's own base URL and must win over
    the global ``link_url_base``.
    """
    factory = _make_deps_factory(
        session_store=object(), redis=_FakeRedis(), session_factory=object(),
        link_url_base="https://studio.example",
    )
    plat = _RecordingPlatform()
    routing = _routing("linked", api_web_url="https://acme.cloud.surogate.ai")
    deps = factory("slack", routing, {"bot_token": "x"}, plat)

    msg = SimpleNamespace(platform_user_id="U1", identifier="C1", is_dm=True)
    await deps.pairing_sender("o1", "slack", msg, "CODE-123")

    text = plat.sent[0]["text"]
    assert "https://acme.cloud.surogate.ai/link" in text
    assert "studio.example" not in text, "the shared host must not win"


@pytest.mark.asyncio
async def test_link_prompt_without_any_base_url_names_studio_generically():
    """No per-agent URL and no configured base → no link at all.

    A link to a host that cannot log the user in is worse than none, so the
    fallback stays the generic instruction.
    """
    factory = _make_deps_factory(
        session_store=object(), redis=_FakeRedis(), session_factory=object(),
    )
    plat = _RecordingPlatform()
    deps = factory("slack", _routing("linked"), {"bot_token": "x"}, plat)

    msg = SimpleNamespace(platform_user_id="U1", identifier="C1", is_dm=True)
    await deps.pairing_sender("o1", "slack", msg, "CODE-123")

    text = plat.sent[0]["text"]
    assert "http" not in text
    assert "Surogate Studio" in text


class _FailingPlatform:
    """send_private exists but reports non-delivery (e.g. the user blocked the bot)."""

    async def send_private(self, creds, *, sender_id, chat_id, is_dm, text):
        return False


class _NoPrivatePlatform:
    """A platform with no private-addressing capability at all."""


@pytest.mark.asyncio
async def test_pairing_sender_reports_undelivered_when_send_private_fails():
    factory = _make_deps_factory(
        session_store=object(), redis=_FakeRedis(), session_factory=object(),
    )
    deps = factory("slack", _routing("linked"), {"bot_token": "x"}, _FailingPlatform())

    msg = SimpleNamespace(platform_user_id="U1", identifier="C1", is_dm=True)
    delivered = await deps.pairing_sender("o1", "slack", msg, "CODE-123")

    assert delivered is False, "a failed private send must report not-delivered"


@pytest.mark.asyncio
async def test_pairing_sender_reports_undelivered_when_no_send_private():
    factory = _make_deps_factory(
        session_store=object(), redis=_FakeRedis(), session_factory=object(),
    )
    deps = factory("slack", _routing("linked"), {}, _NoPrivatePlatform())

    msg = SimpleNamespace(platform_user_id="U1", identifier="C1", is_dm=True)
    delivered = await deps.pairing_sender("o1", "slack", msg, "CODE-123")

    assert delivered is False, "no send_private capability → not-delivered"


def test_factory_selects_resolver_by_policy():
    factory = _make_deps_factory(
        session_store=object(), redis=_FakeRedis(), session_factory=object(),
    )
    plat = _RecordingPlatform()
    shadow_deps = factory("slack", _routing(), {}, plat)          # default → shadow
    linked_deps = factory("slack", _routing("linked"), {}, plat)
    assert shadow_deps.resolve_identity is not linked_deps.resolve_identity


def test_factory_exposes_identity_caches_for_invalidation():
    """The factory exposes both resolvers' caches so the channels process can
    wire them into the cross-process invalidator (invalidate-on-link)."""
    factory = _make_deps_factory(
        session_store=object(), redis=_FakeRedis(), session_factory=object(),
    )
    caches = factory.identity_caches
    assert len(caches) == 2, "shadow + linked resolver caches"
    for c in caches:
        assert hasattr(c, "invalidate")


def test_multicache_fans_invalidate_to_every_cache():
    """_MultiCache lets the invalidator evict a key from both the shadow and
    linked identity caches with a single invalidate(key) call."""
    a, b = MagicMock(), MagicMock()
    _MultiCache([a, b]).invalidate("slack\x00U1\x00org-1")
    a.invalidate.assert_called_once_with("slack\x00U1\x00org-1")
    b.invalidate.assert_called_once_with("slack\x00U1\x00org-1")
