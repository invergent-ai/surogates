# tests/test_channel_backfill_coordinator.py
import uuid
from types import SimpleNamespace
from surogates.channels.channel_backfill import (
    BackfillLimits, warm_cache, maybe_seed_session, cache_key,
)
from surogates.session.events import EventType

class FakeRedis:
    def __init__(self): self.kv = {}
    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.kv: return None
        self.kv[k] = v; return True
    async def get(self, k): return self.kv.get(k)
    async def delete(self, *ks):
        for k in ks: self.kv.pop(k, None)

class FakePlatform:
    def __init__(self, result): self._result = result; self.calls = 0
    async def fetch_channel_context(self, *, creds, channel_id, limits):
        self.calls += 1
        return self._result

class FakeStore:
    def __init__(self, events=None, config=None):
        self.events = events or []
        self.config = config or {}
        self.emitted = []
        self.config_updates = []
    async def get_session(self, session_id):
        return SimpleNamespace(config=self.config)
    async def get_events(self, session_id, *, types=None, **kw):
        if types:
            tv = {t.value for t in types}
            return [e for e in self.events if e.type in tv]
        return list(self.events)
    async def emit_synthetic_user_message(self, session_id, *, content, synthetic, metadata=None):
        self.emitted.append((content, synthetic, metadata)); return 4242
    async def update_session_config_key(self, session_id, key, value):
        self.config_updates.append((key, value))

def _routing():
    return SimpleNamespace(org_id="o1", agent_id="a1", identifier="A0X", platform="slack")

from surogates.channels.channel_backfill import ChannelMeta, RawMessage

async def test_warm_cache_writes_block():
    now = 1000.0
    plat = FakePlatform((ChannelMeta("eng", "", ""), [RawMessage(now - 1, "Al", "hi")]))
    r = FakeRedis()
    ok = await warm_cache(platform=plat, creds={"bot_token": "x"}, redis=r,
                          org_id="o1", agent_id="a1", identifier="A0X",
                          channel_id="C1", limits=BackfillLimits(), now=now)
    assert ok is True
    assert await r.get(cache_key(org_id="o1", agent_id="a1", kind="slack",
                                 identifier="A0X", channel_id="C1"))

async def test_warm_cache_marks_negative_on_empty_fetch():
    plat = FakePlatform(None)
    r = FakeRedis()
    ok = await warm_cache(platform=plat, creds={"bot_token": "x"}, redis=r,
                          org_id="o1", agent_id="a1", identifier="A0X",
                          channel_id="C1", limits=BackfillLimits(), now=1.0)
    assert ok is False
    k = cache_key(org_id="o1", agent_id="a1", kind="slack", identifier="A0X", channel_id="C1")
    assert await r.get(f"{k}:neg")

async def test_seed_emits_once_and_marks_config():
    now = 1000.0
    plat = FakePlatform((ChannelMeta("eng", "t", "p"), [RawMessage(now - 1, "Al", "hi")]))
    store = FakeStore(events=[])
    r = FakeRedis()
    sid = uuid.uuid4()
    eid = await maybe_seed_session(store=store, redis=r, platform=plat,
        creds={"bot_token": "x"}, routing=_routing(), session_id=sid,
        channel_id="C1", limits=BackfillLimits(), now=now)
    assert eid == 4242
    assert len(store.emitted) == 1
    content, synthetic, meta = store.emitted[0]
    assert synthetic == "channel_history_backfill"
    assert "channel context" in content
    assert store.config_updates and store.config_updates[0][0] == "history_backfill"

async def test_seed_skips_when_prior_real_user_message():
    real = SimpleNamespace(type=EventType.USER_MESSAGE.value, data={"content": "hey"})
    store = FakeStore(events=[real])
    plat = FakePlatform((ChannelMeta("e", "", ""), [RawMessage(1.0, "A", "x")]))
    eid = await maybe_seed_session(store=store, redis=FakeRedis(), platform=plat,
        creds={"bot_token": "x"}, routing=_routing(), session_id=uuid.uuid4(),
        channel_id="C1", limits=BackfillLimits(), now=2.0)
    assert eid is None
    assert store.emitted == []

async def test_seed_skips_when_config_marker_exists():
    store = FakeStore(events=[], config={"history_backfill": {"event_id": 10}})
    plat = FakePlatform((ChannelMeta("e", "", ""), [RawMessage(1.0, "A", "x")]))
    eid = await maybe_seed_session(store=store, redis=FakeRedis(), platform=plat,
        creds={"bot_token": "x"}, routing=_routing(), session_id=uuid.uuid4(),
        channel_id="C1", limits=BackfillLimits(), now=2.0)
    assert eid is None
    assert store.emitted == []

async def test_seed_skips_when_prior_synthetic_seed():
    seed = SimpleNamespace(type=EventType.USER_MESSAGE.value,
                           data={"synthetic": "channel_history_backfill"})
    store = FakeStore(events=[seed])
    plat = FakePlatform((ChannelMeta("e", "", ""), [RawMessage(1.0, "A", "x")]))
    eid = await maybe_seed_session(store=store, redis=FakeRedis(), platform=plat,
        creds={"bot_token": "x"}, routing=_routing(), session_id=uuid.uuid4(),
        channel_id="C1", limits=BackfillLimits(), now=2.0)
    assert eid is None

async def test_seed_never_raises_on_store_failure():
    class BoomStore(FakeStore):
        async def emit_synthetic_user_message(self, *a, **k): raise RuntimeError("db down")
    plat = FakePlatform((ChannelMeta("e", "", ""), [RawMessage(1.0, "A", "x")]))
    eid = await maybe_seed_session(store=BoomStore(events=[]), redis=FakeRedis(), platform=plat,
        creds={"bot_token": "x"}, routing=_routing(), session_id=uuid.uuid4(),
        channel_id="C1", limits=BackfillLimits(), now=2.0)
    assert eid is None  # swallowed
