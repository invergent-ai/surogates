"""Rehearsal numbers route from the runtime DB; every other id still asks ops."""

from __future__ import annotations

import pytest

from surogates.api.app import build_channel_routing_cache
from tests.test_rehearsal_models import rsf  # noqa: F401
from tests.test_rehearsal_store import make_run


class FakePlatform:
    def __init__(self):
        self.asked: list[tuple[str, str]] = []

    async def get_channel_routing(self, kind, identifier):
        self.asked.append((kind, identifier))
        return {"org_id": "o", "agent_id": "real", "config": {}}


@pytest.mark.asyncio
async def test_rehearsal_number_never_reaches_ops(rsf):  # noqa: F811
    run = await make_run(rsf)
    platform = FakePlatform()
    cache = build_channel_routing_cache(settings=None, platform_client=platform, session_factory=rsf)

    routed = await cache.get(f"whatsapp:{run.phone_number_id}")
    assert routed["agent_id"] == "a1" and routed["config"]["rehearsal_run_id"] == str(run.id)
    assert platform.asked == []

    assert (await cache.get("whatsapp:100000000000001"))["agent_id"] == "real"
    assert platform.asked == [("whatsapp", "100000000000001")]
