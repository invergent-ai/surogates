"""Per-end-user allowance enforcement on the web channel.

``authorize_allowance_turn`` applies to every signed-in end-user (any
identity provider, regardless of commerce mode) — closing the
Firebase-only / free-mode gaps of the commerce gate — but only when ops
projects a positive ``end_user_token_allowance``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from surogates.api.routes._commerce_turn import authorize_allowance_turn
from surogates.runtime.platform_client import AllowanceExhaustedError


class _FakeCache:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, key):
        if key not in self._rows:
            raise LookupError(key)
        return self._rows[key]


class _FakeStore:
    def __init__(self):
        self.updates: list[tuple] = []

    async def append_session_config_list(self, session_id, key, value):
        self.updates.append((session_id, key, value))


class _FakePlatformClient:
    def __init__(self, *, receipt=None, error=None):
        self.receipt = receipt
        self.error = error
        self.calls: list[dict] = []

    async def allowance_authorize(self, agent_id, **kwargs):
        self.calls.append({"agent_id": agent_id, **kwargs})
        if self.error is not None:
            raise self.error
        return self.receipt


def _request(*, runtime_payload=None, platform_client=None, store=None):
    state = SimpleNamespace()
    if runtime_payload is not None:
        state.runtime_config_cache = _FakeCache({"a-1": runtime_payload})
    if platform_client is not None:
        state.platform_client = platform_client
    if store is not None:
        state.session_store = store
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _session():
    return SimpleNamespace(id=uuid.uuid4(), agent_id="a-1", config={})


@pytest.mark.asyncio
async def test_no_allowance_projected_is_a_noop():
    """No ``end_user_token_allowance`` in the payload → no client call, no
    hold (uncapped agents, the default, are untouched)."""
    client = _FakePlatformClient(receipt={"allowance_id": "x"})
    store = _FakeStore()
    request = _request(
        runtime_payload={"commerce_mode": "free"},
        platform_client=client,
        store=store,
    )
    await authorize_allowance_turn(
        request, _session(), "hello", end_user_id="db-user-1",
    )
    assert client.calls == []
    assert store.updates == []


@pytest.mark.asyncio
async def test_capped_user_reserves_and_pins_receipt():
    client = _FakePlatformClient(
        receipt={
            "allowance_id": "al-1",
            "reserved_tokens": 12,
            "reservation_id": "r-1",
        },
    )
    store = _FakeStore()
    request = _request(
        runtime_payload={"end_user_token_allowance": 1000},
        platform_client=client,
        store=store,
    )
    session = _session()
    # A database (non-Firebase) end-user is metered too — the hatch is closed.
    await authorize_allowance_turn(
        request, session, "some message content", end_user_id="db-user-1",
    )
    assert len(client.calls) == 1
    assert client.calls[0]["end_user_id"] == "db-user-1"
    assert client.calls[0]["estimated_tokens"] > 0
    assert store.updates == [
        (
            session.id,
            "allowance_reservations",
            {"allowance_id": "al-1", "reserved_tokens": 12, "reservation_id": "r-1"},
        )
    ]


@pytest.mark.asyncio
async def test_always_reserves_even_without_a_projected_cap():
    """The per-buyer website embed forces authorization against the
    buyer's purchased allowance even when the agent projects no default
    cap (``end_user_token_allowance`` absent)."""
    client = _FakePlatformClient(
        receipt={
            "allowance_id": "al-e",
            "reserved_tokens": 5,
            "reservation_id": "r-e",
        },
    )
    store = _FakeStore()
    request = _request(
        runtime_payload={},  # no end_user_token_allowance projected
        platform_client=client,
        store=store,
    )
    await authorize_allowance_turn(
        request, _session(), "hello there", end_user_id="buyer-1", always=True,
    )
    assert len(client.calls) == 1
    assert client.calls[0]["end_user_id"] == "buyer-1"
    assert store.updates and store.updates[0][1] == "allowance_reservations"


@pytest.mark.asyncio
async def test_without_always_no_projection_stays_a_noop():
    """The default (non-embed) path is still a no-op with no projection."""
    client = _FakePlatformClient(receipt={"allowance_id": "x"})
    store = _FakeStore()
    request = _request(runtime_payload={}, platform_client=client, store=store)
    await authorize_allowance_turn(
        request, _session(), "hi", end_user_id="u1",
    )
    assert client.calls == []


@pytest.mark.asyncio
async def test_uncapped_receipt_pins_no_hold():
    """Ops may report an uncapped user (empty allowance_id) even with the
    flag on — nothing to settle, so no hold is pinned."""
    client = _FakePlatformClient(
        receipt={"allowance_id": "", "reserved_tokens": 0, "reservation_id": ""},
    )
    store = _FakeStore()
    request = _request(
        runtime_payload={"end_user_token_allowance": 1000},
        platform_client=client,
        store=store,
    )
    await authorize_allowance_turn(
        request, _session(), "hi", end_user_id="u1",
    )
    assert len(client.calls) == 1
    assert store.updates == []


@pytest.mark.asyncio
async def test_exhausted_allowance_raises_402_with_buy_url():
    """The 402 carries the agent's buy-page URL so the client can render a
    real buy prompt (matching the commerce gate's structured detail)."""
    client = _FakePlatformClient(error=AllowanceExhaustedError("allowance_exhausted"))
    store = _FakeStore()
    request = _request(
        runtime_payload={
            "end_user_token_allowance": 50,
            "commerce_buy_url": "https://buy.example/agent",
        },
        platform_client=client,
        store=store,
    )
    with pytest.raises(HTTPException) as exc:
        await authorize_allowance_turn(
            request, _session(), "content", end_user_id="u1",
        )
    assert exc.value.status_code == 402
    assert exc.value.detail == {
        "code": "allowance_exhausted",
        "buy_url": "https://buy.example/agent",
    }
    assert store.updates == []


@pytest.mark.asyncio
async def test_subscription_required_402_carries_code_and_null_buy_url():
    """No buy URL projected → the key is still present (value None) so the
    client's paywall branch is uniform."""
    client = _FakePlatformClient(error=AllowanceExhaustedError("subscription_required"))
    store = _FakeStore()
    request = _request(
        runtime_payload={"end_user_token_allowance": 50},
        platform_client=client,
        store=store,
    )
    with pytest.raises(HTTPException) as exc:
        await authorize_allowance_turn(
            request, _session(), "content", end_user_id="u1",
        )
    assert exc.value.status_code == 402
    assert exc.value.detail == {"code": "subscription_required", "buy_url": None}


@pytest.mark.asyncio
async def test_metering_plane_error_fails_closed_503():
    client = _FakePlatformClient(error=RuntimeError("ops down"))
    store = _FakeStore()
    request = _request(
        runtime_payload={"end_user_token_allowance": 50},
        platform_client=client,
        store=store,
    )
    with pytest.raises(HTTPException) as exc:
        await authorize_allowance_turn(
            request, _session(), "content", end_user_id="u1",
        )
    assert exc.value.status_code == 503


# ── Package plumbing: channel forwarding + entitlements pinning ───────


class _ReconcilingStore(_FakeStore):
    def __init__(self):
        super().__init__()
        self.reconciled: list[tuple] = []

    async def reconcile_session_config_key(self, session_id, key, value):
        self.reconciled.append((session_id, key, value))


@pytest.mark.asyncio
async def test_channel_is_forwarded_to_ops():
    client = _FakePlatformClient(
        receipt={"allowance_id": "", "reserved_tokens": 0},
    )
    store = _ReconcilingStore()
    request = _request(
        runtime_payload={"end_user_token_allowance": 1000},
        platform_client=client,
        store=store,
    )
    await authorize_allowance_turn(
        request, _session(), "hi", end_user_id="u1", channel="web",
    )
    assert client.calls[0]["channel"] == "web"


@pytest.mark.asyncio
async def test_features_receipt_pins_entitlements_on_the_session():
    client = _FakePlatformClient(
        receipt={
            "allowance_id": "al-1",
            "reserved_tokens": 5,
            "reservation_id": "r-1",
            "features": {"capabilities": ["code"], "channels": ["website"]},
        },
    )
    store = _ReconcilingStore()
    request = _request(
        runtime_payload={"end_user_token_allowance": 1000},
        platform_client=client,
        store=store,
    )
    session = _session()
    await authorize_allowance_turn(
        request, session, "hello", end_user_id="u1", channel="website",
    )
    assert store.reconciled == [
        (
            session.id,
            "entitlements",
            {"capabilities": ["code"], "channels": ["website"]},
        ),
    ]


@pytest.mark.asyncio
async def test_unrestricted_receipt_clears_a_stale_pin():
    """A user whose package went away (cancelled sub, cleared offer)
    must not keep yesterday's restrictions pinned on the session."""
    client = _FakePlatformClient(
        receipt={"allowance_id": "", "reserved_tokens": 0, "features": None},
    )
    store = _ReconcilingStore()
    request = _request(
        runtime_payload={"end_user_token_allowance": 1000},
        platform_client=client,
        store=store,
    )
    session = _session()
    session.config = {"entitlements": {"capabilities": []}}
    await authorize_allowance_turn(
        request, session, "hi", end_user_id="u1",
    )
    assert store.reconciled == [(session.id, "entitlements", None)]


@pytest.mark.asyncio
async def test_steady_state_pin_is_write_free():
    """Same package as already pinned → no store round trip at all."""
    features = {"capabilities": ["code"]}
    client = _FakePlatformClient(
        receipt={
            "allowance_id": "", "reserved_tokens": 0, "features": features,
        },
    )
    store = _ReconcilingStore()
    request = _request(
        runtime_payload={"end_user_token_allowance": 1000},
        platform_client=client,
        store=store,
    )
    session = _session()
    session.config = {"entitlements": {"capabilities": ["code"]}}
    await authorize_allowance_turn(
        request, session, "hi", end_user_id="u1",
    )
    assert store.reconciled == []


@pytest.mark.asyncio
async def test_channel_not_included_402_carries_code_and_buy_url():
    client = _FakePlatformClient(
        error=AllowanceExhaustedError("channel_not_included"),
    )
    store = _ReconcilingStore()
    request = _request(
        runtime_payload={
            "end_user_token_allowance": 50,
            "commerce_buy_url": "https://buy.example/agent",
        },
        platform_client=client,
        store=store,
    )
    with pytest.raises(HTTPException) as exc:
        await authorize_allowance_turn(
            request, _session(), "content", end_user_id="u1", channel="slack",
        )
    assert exc.value.status_code == 402
    assert exc.value.detail == {
        "code": "channel_not_included",
        "buy_url": "https://buy.example/agent",
    }


@pytest.mark.asyncio
async def test_uncapped_agent_pins_the_projected_default_package():
    """An uncapped agent never phones home per turn, so the agent's
    default package (projected as ``default_user_features``) must be
    pinned locally — otherwise a free unlimited agent with a restricted
    default serves every capability unrestricted."""
    client = _FakePlatformClient(receipt={"allowance_id": "x"})
    store = _ReconcilingStore()
    request = _request(
        runtime_payload={
            "commerce_mode": "free",
            "default_user_features": {"capabilities": ["loop"]},
        },
        platform_client=client,
        store=store,
    )
    session = _session()
    await authorize_allowance_turn(
        request, session, "hello", end_user_id="u1",
    )
    assert client.calls == []
    assert store.reconciled == [
        (session.id, "entitlements", {"capabilities": ["loop"]}),
    ]
