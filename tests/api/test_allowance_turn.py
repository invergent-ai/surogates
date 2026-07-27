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
async def test_exhausted_allowance_raises_402():
    client = _FakePlatformClient(error=AllowanceExhaustedError("allowance_exhausted"))
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
    assert exc.value.detail == {"code": "allowance_exhausted"}
    assert store.updates == []


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
