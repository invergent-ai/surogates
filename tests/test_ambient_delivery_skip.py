import pytest

from surogates.session import store as store_mod
from surogates.session.events import EventType


class _SFTracker:
    """Tracks whether the session factory was invoked (i.e. an outbox enqueue
    was attempted).  The store method swallows exceptions, so we assert on a
    flag rather than relying on a raise propagating."""
    def __init__(self): self.called = False
    def __call__(self):
        self.called = True
        raise RuntimeError("stop before real db work")


@pytest.mark.asyncio
async def test_ambient_channel_skips_outbox():
    inst = store_mod.SessionStore.__new__(store_mod.SessionStore)
    inst._channel_cache = {"sess-1": ("ambient", {"slack_channel_id": "C1"})}
    sf = _SFTracker()
    inst._sf = sf
    await store_mod.SessionStore._enqueue_channel_delivery(
        inst, "sess-1", 1, EventType.LLM_RESPONSE,
        {"message": {"content": "private reasoning"}},
    )
    assert sf.called is False  # ambient returned before any outbox enqueue


@pytest.mark.asyncio
async def test_slack_channel_does_enqueue():
    """Control: a slack session with content WOULD reach the outbox enqueue."""
    inst = store_mod.SessionStore.__new__(store_mod.SessionStore)
    inst._channel_cache = {"sess-2": ("slack", {"slack_channel_id": "C1"})}
    sf = _SFTracker()
    inst._sf = sf
    await store_mod.SessionStore._enqueue_channel_delivery(
        inst, "sess-2", 1, EventType.LLM_RESPONSE,
        {"message": {"content": "hi there"}},
    )
    assert sf.called is True  # slack reaches the enqueue path
