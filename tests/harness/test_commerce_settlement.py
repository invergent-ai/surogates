"""Worker-side settlement of monetized website turns.

``_settle_commerce_reservation`` runs inside ``_complete_session``:
it debits the turn's summed LLM usage against the reservation the
website channel pinned on ``session.config`` and clears the pin.
Failures leave the hold to the ops reservation reaper — never a
crash in the completion path.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from surogates.harness.cost_tracker import SessionCostTracker
from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin


class _Harness(ArtifactCompletionMixin):
    def __init__(self, *, platform_client, store):
        self._platform_client = platform_client
        self._store = store


class _FakeStore:
    def __init__(self):
        self.cleared: list[tuple] = []

    async def clear_session_config_key(self, session_id, key):
        self.cleared.append((session_id, key))


class _FakePlatformClient:
    def __init__(self, *, error=None):
        self.error = error
        self.debits: list[dict] = []

    async def commerce_debit(self, agent_id, **kwargs):
        if self.error is not None:
            raise self.error
        self.debits.append({"agent_id": agent_id, **kwargs})
        return {"debited_tokens": kwargs["actual_tokens"]}


def _session(reservation=None):
    config = {}
    if reservation is not None:
        config["commerce_reservation"] = reservation
    return SimpleNamespace(id=uuid.uuid4(), agent_id="a-1", config=config)


_RESERVATION = {
    "entitlement_id": "ent-1",
    "reserved_tokens": 500,
    "reservation_id": "res-1",
}


@pytest.mark.asyncio
async def test_settles_actual_usage_and_clears_pin():
    client = _FakePlatformClient()
    store = _FakeStore()
    harness = _Harness(platform_client=client, store=store)
    tracker = SessionCostTracker()
    tracker.record_call(150, 90, 0.01)
    session = _session(_RESERVATION)

    await harness._settle_commerce_reservation(session, tracker)

    assert client.debits == [
        {
            "agent_id": "a-1",
            "entitlement_id": "ent-1",
            "reserved_tokens": 500,
            "actual_tokens": 240,
            "reservation_id": "res-1",
        },
    ]
    assert store.cleared == [(session.id, "commerce_reservation")]


@pytest.mark.asyncio
async def test_no_reservation_is_a_noop():
    client = _FakePlatformClient()
    store = _FakeStore()
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(_session(), SessionCostTracker())

    assert client.debits == []
    assert store.cleared == []


@pytest.mark.asyncio
async def test_missing_tracker_consumes_the_reserved_floor():
    client = _FakePlatformClient()
    store = _FakeStore()
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(_session(_RESERVATION), None)

    assert client.debits[0]["actual_tokens"] == 500


@pytest.mark.asyncio
async def test_debit_failure_keeps_the_pin_for_the_reaper():
    client = _FakePlatformClient(error=RuntimeError("ops down"))
    store = _FakeStore()
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(
        _session(_RESERVATION), SessionCostTracker(),
    )

    assert store.cleared == []


@pytest.mark.asyncio
async def test_missing_platform_client_leaves_hold_to_the_reaper():
    store = _FakeStore()
    harness = _Harness(platform_client=None, store=store)

    await harness._settle_commerce_reservation(
        _session(_RESERVATION), SessionCostTracker(),
    )

    assert store.cleared == []
