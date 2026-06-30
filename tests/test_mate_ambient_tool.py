import json

import pytest

from surogates.tools.mate_ambient import handle_mate_ambient_post


class FakeRedis:
    def __init__(self): self.kv = {}
    async def incr(self, k): self.kv[k] = int(self.kv.get(k, 0)) + 1; return self.kv[k]
    async def expire(self, k, s): pass
    async def get(self, k): return self.kv.get(k)
    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True


class FakeDelivery:
    def __init__(self): self.enqueued = []
    async def enqueue(self, session_id, event_id, channel, destination, payload):
        self.enqueued.append((channel, destination, payload)); return 1


def _ctx(**over):
    base = dict(
        agent_id="ag",
        session_config={"slack_channel_id": "C1", "slack_team_id": "T1"},
        session_id="s1",
        redis=FakeRedis(),
        delivery=FakeDelivery(),
        caps={
            "confidence_threshold": 0.7,
            "max_proactive_posts_per_day": 5,
            "min_seconds_between_posts": 0,
            "quiet_thread_minutes": 120,
        },
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_posts_when_confident_and_under_cap():
    ctx = _ctx()
    out = json.loads(await handle_mate_ambient_post(
        {"target_thread": "t1", "message": "ci is red", "confidence": 0.9, "rationale": "r"},
        **ctx,
    ))
    assert out["posted"] is True
    assert ctx["delivery"].enqueued
    channel, dest, payload = ctx["delivery"].enqueued[0]
    assert channel == "slack"
    assert dest["channel_id"] == "C1"
    assert dest["thread_ts"] == "t1"
    assert payload["content"] == "ci is red"


@pytest.mark.asyncio
async def test_suppressed_when_low_confidence():
    ctx = _ctx()
    out = json.loads(await handle_mate_ambient_post(
        {"target_thread": "t1", "message": "maybe", "confidence": 0.4},
        **ctx,
    ))
    assert out["posted"] is False
    assert ctx["delivery"].enqueued == []


@pytest.mark.asyncio
async def test_suppressed_when_over_daily_cap():
    ctx = _ctx(caps={**_ctx()["caps"], "max_proactive_posts_per_day": 1})
    args = {"target_thread": "t1", "message": "x", "confidence": 0.9}
    await handle_mate_ambient_post(args, **ctx)          # first ok
    out = json.loads(await handle_mate_ambient_post(args, **ctx))  # second blocked
    assert out["posted"] is False
    assert len(ctx["delivery"].enqueued) == 1


@pytest.mark.asyncio
async def test_no_channel_bound_is_suppressed():
    ctx = _ctx(session_config={})
    out = json.loads(await handle_mate_ambient_post(
        {"message": "x", "confidence": 0.9}, **ctx,
    ))
    assert out["posted"] is False
    assert ctx["delivery"].enqueued == []


def test_mate_ambient_post_routes_to_harness():
    from surogates.tools.router import TOOL_LOCATIONS, ToolLocation
    assert TOOL_LOCATIONS["mate_ambient_post"] == ToolLocation.HARNESS


def test_register_adds_tool_to_registry():
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.mate_ambient import register
    reg = ToolRegistry()
    register(reg)
    names = {s["function"]["name"] for s in reg.get_schemas({"mate_ambient_post"})}
    assert "mate_ambient_post" in names


@pytest.mark.asyncio
async def test_adapter_blocks_non_ambient_session():
    from surogates.tools.mate_ambient import _mate_ambient_post_handler
    out = json.loads(await _mate_ambient_post_handler(
        {"message": "x", "confidence": 0.9},
        session_config={"slack_channel_id": "C1"},  # no "ambient": True
        agent_id="ag", session_id="s1", redis=FakeRedis(), session_factory=None,
    ))
    assert out["posted"] is False
    assert "not an ambient session" in out["reason"]
