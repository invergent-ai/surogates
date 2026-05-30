"""Tests for the shared work-queue enqueue contract.

Plan 2 / Task 12.  Today: one Redis sorted-set per agent
(``surogates:work_queue:<agent_id>``).  Plan 2: a single shared
sorted-set ``surogates:work_queue`` whose members encode
``<org_id>|<agent_id>|<session_id>`` so the dispatcher can extract
the tenant for the gate check without a DB round-trip per dequeue.
"""

from __future__ import annotations

from collections import defaultdict

import pytest


class _FakeRedis:
    def __init__(self) -> None:
        self._zsets: dict[str, list[tuple[str, float]]] = defaultdict(list)

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        added = 0
        for member, score in mapping.items():
            self._zsets[key].append((member, score))
            added += 1
        return added

    def _members(self, key: str) -> list[str]:
        return [m for m, _ in self._zsets.get(key, [])]


@pytest.mark.asyncio
async def test_enqueue_session_writes_to_shared_key():
    from surogates.config import (
        SHARED_WORK_QUEUE_KEY, enqueue_session,
    )

    r = _FakeRedis()
    await enqueue_session(
        r, org_id="o-1", agent_id="a-1",
        session_id="s-1", priority=5,
    )
    assert "o-1|a-1|s-1" in r._members(SHARED_WORK_QUEUE_KEY)


@pytest.mark.asyncio
async def test_enqueue_session_priority_becomes_zset_score():
    from surogates.config import (
        SHARED_WORK_QUEUE_KEY, enqueue_session,
    )

    r = _FakeRedis()
    await enqueue_session(
        r, org_id="o-1", agent_id="a-1",
        session_id="s-1", priority=7,
    )
    scores = [s for _, s in r._zsets[SHARED_WORK_QUEUE_KEY]]
    assert scores == [7]


@pytest.mark.asyncio
async def test_enqueue_session_rejects_pipe_in_identifiers():
    """The member-encoding splits on '|' so an identifier containing
    a pipe would corrupt parsing on the dequeue side.  Fail-fast at
    enqueue so the bad row never lands in the queue."""
    from surogates.config import enqueue_session

    r = _FakeRedis()
    with pytest.raises(ValueError, match=r"\|"):
        await enqueue_session(
            r, org_id="o-1|with|pipes", agent_id="a-1",
            session_id="s-1", priority=0,
        )


def test_parse_queue_member_round_trips():
    from surogates.config import encode_queue_member, parse_queue_member

    member = encode_queue_member(
        org_id="o-1", agent_id="a-1", session_id="s-1",
    )
    org_id, agent_id, session_id = parse_queue_member(member)
    assert (org_id, agent_id, session_id) == ("o-1", "a-1", "s-1")
