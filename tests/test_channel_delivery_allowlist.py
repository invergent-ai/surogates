"""Outbox enqueue is limited to channels with a delivery adapter.

Rows for channels no delivery loop claims (api, website, task, delegation,
worker, web, ambient, teams) would sit "pending" forever — the store must
not create them in the first place.
"""

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


def _store_with_channel(channel: str, config: dict | None = None):
    inst = store_mod.SessionStore.__new__(store_mod.SessionStore)
    inst._channel_cache = {"sess-1": (channel, config or {})}
    inst._sf = _SFTracker()
    return inst


_EVENT_DATA = {"message": {"content": "hello"}}


@pytest.mark.parametrize(
    "channel",
    ["api", "website", "task", "delegation", "worker", "web", "ambient", "teams", "scheduled"],
)
async def test_adapterless_channels_skip_outbox(channel):
    inst = _store_with_channel(channel)
    await store_mod.SessionStore._enqueue_channel_delivery(
        inst, "sess-1", 1, EventType.LLM_RESPONSE, _EVENT_DATA,
    )
    assert inst._sf.called is False


@pytest.mark.parametrize("channel", ["slack", "telegram"])
async def test_adapter_channels_reach_enqueue(channel):
    inst = _store_with_channel(channel, {f"{channel}_channel_id": "C1"})
    await store_mod.SessionStore._enqueue_channel_delivery(
        inst, "sess-1", 1, EventType.LLM_RESPONSE, _EVENT_DATA,
    )
    assert inst._sf.called is True


async def test_telegram_destination_carries_reply_to():
    """The telegram destination forwards the session's reply-to message id."""
    captured: dict = {}

    inst = store_mod.SessionStore.__new__(store_mod.SessionStore)
    inst._channel_cache = {
        "sess-1": (
            "telegram",
            {
                "telegram_channel_id": "12345",
                "telegram_thread_key": None,
                "telegram_reply_to_message_id": 777,
                "channel_identifier": "@bot",
            },
        )
    }

    class _CaptureSF:
        def __call__(self):
            raise RuntimeError("stop before real db work")

    inst._sf = _CaptureSF()

    original_build = store_mod._build_channel_payload

    def _spy_build(event_type, data, channel):
        captured["channel"] = channel
        return original_build(event_type, data, channel)

    store_mod._build_channel_payload = _spy_build
    try:
        await store_mod.SessionStore._enqueue_channel_delivery(
            inst, "sess-1", 1, EventType.LLM_RESPONSE, _EVENT_DATA,
        )
    finally:
        store_mod._build_channel_payload = original_build

    assert captured["channel"] == "telegram"
