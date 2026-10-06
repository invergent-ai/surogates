"""Calls beyond what the speech services can serve hear "busy" instead of dead air. Real Redis."""
from __future__ import annotations

import pytest

from surogates.voice.capacity import CallSlots

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_a_call_over_capacity_is_refused_and_a_freed_slot_is_reused(redis_client):
    slots = CallSlots(redis_client, capacity=2, key="voice:test:slots")
    await redis_client.delete("voice:test:slots")
    assert await slots.take("a", hold_seconds=600) and await slots.take("b", hold_seconds=600)
    assert not await slots.take("c", hold_seconds=600)
    await slots.release("a")
    assert await slots.take("c", hold_seconds=600)


async def test_a_slot_held_by_a_crashed_worker_expires(redis_client):
    slots = CallSlots(redis_client, capacity=1, key="voice:test:slots")
    await redis_client.delete("voice:test:slots")
    assert await slots.take("crashed", hold_seconds=-1)  # its hold already ran out: nobody released it
    assert await slots.take("next", hold_seconds=600)
