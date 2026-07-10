"""Worker-side settlement of monetized website turns.

``_settle_commerce_reservation`` runs inside ``_complete_session``: it
takes the ``commerce_reservations`` list from the LIVE session config
(follow-up messages may have appended holds after wake start), debits
the wake's summed LLM usage against the oldest hold, and releases the
rest — the total already covers their consumption. Failures leave the
holds to the ops reservation reaper; never a crash in the completion
path.
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
    def __init__(self, reservations=None):
        self.reservations = reservations
        self.pops: list[tuple] = []

    async def pop_session_config_key(self, session_id, key):
        self.pops.append((session_id, key))
        taken, self.reservations = self.reservations, None
        return taken


class _FakePlatformClient:
    def __init__(self, *, error=None):
        self.error = error
        self.debits: list[dict] = []

    async def commerce_debit(self, agent_id, **kwargs):
        if self.error is not None:
            raise self.error
        self.debits.append({"agent_id": agent_id, **kwargs})
        return {"debited_tokens": kwargs["actual_tokens"]}


def _session(*, channel="website", config=None):
    return SimpleNamespace(
        id=uuid.uuid4(), agent_id="a-1", channel=channel, config=config or {},
    )


_R1 = {
    "entitlement_id": "ent-1",
    "reserved_tokens": 500,
    "reservation_id": "res-1",
}
_R2 = {
    "entitlement_id": "ent-1",
    "reserved_tokens": 40,
    "reservation_id": "res-2",
}


def _tracker(input_tokens: int, output_tokens: int) -> SessionCostTracker:
    tracker = SessionCostTracker()
    tracker.record_call(input_tokens, output_tokens, 0.01)
    return tracker


@pytest.mark.asyncio
async def test_settles_total_usage_against_the_oldest_hold():
    client = _FakePlatformClient()
    store = _FakeStore(reservations=[_R1])
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(
        _session(), _tracker(150, 90),
    )

    assert client.debits == [
        {
            "agent_id": "a-1",
            "entitlement_id": "ent-1",
            "reserved_tokens": 500,
            "actual_tokens": 240,
            "reservation_id": "res-1",
        },
    ]


@pytest.mark.asyncio
async def test_extra_holds_release_with_zero_usage():
    """Steer messages folded into the wake pinned extra holds; the
    total charged on the first already covers them."""
    client = _FakePlatformClient()
    store = _FakeStore(reservations=[_R1, _R2])
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(
        _session(), _tracker(1000, 500),
    )

    assert [d["reservation_id"] for d in client.debits] == ["res-1", "res-2"]
    assert [d["actual_tokens"] for d in client.debits] == [1500, 0]


@pytest.mark.asyncio
async def test_website_session_pops_even_when_wake_config_is_stale():
    """The hold may have been appended after the wake loaded the
    session row — website sessions always take the live list."""
    client = _FakePlatformClient()
    store = _FakeStore(reservations=[_R1])
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(
        _session(config={}), _tracker(10, 5),
    )

    assert store.pops != []
    assert len(client.debits) == 1


@pytest.mark.asyncio
async def test_non_website_channel_without_pin_skips_the_round_trip():
    client = _FakePlatformClient()
    store = _FakeStore(reservations=[_R1])
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(
        _session(channel="web", config={}), _tracker(10, 5),
    )

    assert store.pops == []
    assert client.debits == []


@pytest.mark.asyncio
async def test_missing_tracker_consumes_each_reserved_floor():
    client = _FakePlatformClient()
    store = _FakeStore(reservations=[_R1, _R2])
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(_session(), None)

    assert [d["actual_tokens"] for d in client.debits] == [500, 40]


@pytest.mark.asyncio
async def test_debit_failure_never_raises():
    client = _FakePlatformClient(error=RuntimeError("ops down"))
    store = _FakeStore(reservations=[_R1])
    harness = _Harness(platform_client=client, store=store)

    await harness._settle_commerce_reservation(
        _session(), _tracker(10, 5),
    )


@pytest.mark.asyncio
async def test_missing_platform_client_leaves_holds_to_the_reaper():
    store = _FakeStore(reservations=[_R1])
    harness = _Harness(platform_client=None, store=store)

    await harness._settle_commerce_reservation(
        _session(), _tracker(10, 5),
    )

    assert store.pops == []
