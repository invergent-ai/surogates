"""Tests for the channels deps factory wiring (runner._make_deps_factory).

Verifies that the per-event factory selects the resolver by the channel's
identity_policy and wires the pairing producer (mint a code + privately deliver
the link prompt) for linked mode.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.channels.runner import _make_deps_factory


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


def _routing(policy=None):
    config = {"identity_policy": policy} if policy else {}
    return SimpleNamespace(
        org_id="o1", agent_id="a1", platform="slack", identifier="A0", config=config,
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
    await deps.pairing_sender("o1", "slack", msg, "CODE-123")

    assert plat.sent and plat.sent[0]["sender_id"] == "U1"
    assert "CODE-123" in plat.sent[0]["text"]
    assert "https://studio.example/link" in plat.sent[0]["text"]


def test_factory_selects_resolver_by_policy():
    factory = _make_deps_factory(
        session_store=object(), redis=_FakeRedis(), session_factory=object(),
    )
    plat = _RecordingPlatform()
    shadow_deps = factory("slack", _routing(), {}, plat)          # default → shadow
    linked_deps = factory("slack", _routing("linked"), {}, plat)
    assert shadow_deps.resolve_identity is not linked_deps.resolve_identity
