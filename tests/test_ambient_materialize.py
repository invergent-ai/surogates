import uuid

import pytest

from surogates.ambient.materialize import materialize_ambient_tick
from surogates.ambient.store import AmbientSchedule


class FakeSessionStore:
    def __init__(self):
        self.created = []
        self.synthetic = []
    async def create_session(self, *, session_id, user_id, org_id, agent_id, channel, model, config):
        self.created.append((session_id, channel, config))
    async def emit_synthetic_user_message(self, session_id, *, content, synthetic, metadata=None):
        self.synthetic.append((session_id, content, synthetic)); return 1


class FakeAmbientStore:
    def __init__(self): self.fired = []
    async def mark_fired(self, schedule, *, ambient_session_id):
        self.fired.append((schedule.id, ambient_session_id))


class FakeSettings:
    class llm:
        model = "surogate"


def _schedule(**over):
    base = dict(
        id=uuid.uuid4(), org_id=uuid.uuid4(), agent_id="ag", platform="slack",
        channel_id="C1", source_session_id=uuid.uuid4(), ambient_session_id=None,
        cadence_seconds=1800, status="active", config={"slack_team_id": "T1"},
    )
    base.update(over)
    return AmbientSchedule(**base)


@pytest.mark.asyncio
async def test_creates_ambient_session_and_injects_prompt(monkeypatch):
    enqueued = []
    async def fake_enqueue(redis, *, org_id, agent_id, session_id):
        enqueued.append(session_id)
    monkeypatch.setattr("surogates.ambient.materialize.enqueue_session", fake_enqueue)
    async def fake_changes(*a, **k): return []
    monkeypatch.setattr("surogates.ambient.materialize.recent_task_changes", fake_changes)

    store = FakeSessionStore()
    amb = FakeAmbientStore()
    sid = await materialize_ambient_tick(
        _schedule(), session_store=store, ambient_store=amb,
        session_factory=None, settings=FakeSettings(), redis=None,
    )
    assert any(c[1] == "ambient" and c[2].get("slack_channel_id") == "C1" for c in store.created)
    assert store.synthetic and store.synthetic[0][2] == "ambient_tick"
    assert enqueued == [sid]
    assert amb.fired and amb.fired[0][1] == sid


@pytest.mark.asyncio
async def test_reuses_existing_ambient_session(monkeypatch):
    async def fake_enqueue(redis, *, org_id, agent_id, session_id): pass
    monkeypatch.setattr("surogates.ambient.materialize.enqueue_session", fake_enqueue)
    async def fake_changes(*a, **k): return []
    monkeypatch.setattr("surogates.ambient.materialize.recent_task_changes", fake_changes)

    store = FakeSessionStore()
    existing = uuid.uuid4()
    sid = await materialize_ambient_tick(
        _schedule(ambient_session_id=existing), session_store=store,
        ambient_store=FakeAmbientStore(), session_factory=None,
        settings=FakeSettings(), redis=None,
    )
    assert sid == existing
    assert store.created == []  # reused, not recreated
