"""Tests for the org-scoped pairing code store.

Pairing entries and rate-limit keys are scoped by ``(org_id, platform,
platform_user_id)`` so a code minted in one org cannot be consumed while logged
into another and bind the same platform user id there.
"""

from __future__ import annotations

import pytest

from surogates.channels.pairing import PairingStore


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


@pytest.mark.asyncio
async def test_pairing_entry_and_rate_key_are_org_scoped():
    store = PairingStore(_FakeRedis())
    code = await store.create("org-A", "slack", "U1", {"n": "x"})
    assert (await store.get(code))["org_id"] == "org-A"

    # The same platform user in a different org gets an independent code.
    code_b = await store.create("org-B", "slack", "U1")
    assert code_b is not None and code_b != code

    assert (await store.resolve(code))["org_id"] == "org-A"
    assert (await store.resolve(code_b))["org_id"] == "org-B"


@pytest.mark.asyncio
async def test_pairing_reuses_live_code_per_org_user():
    store = PairingStore(_FakeRedis())
    first = await store.create("org-A", "slack", "U1")
    second = await store.create("org-A", "slack", "U1")
    assert first == second, "a still-live code is reused for the same (org, platform, user)"
