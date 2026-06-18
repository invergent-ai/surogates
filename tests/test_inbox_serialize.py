# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from surogates.api.routes.inbox import _resolve_agent_fields, _serialize_item


def _item(session_id):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=1, org_id=uuid.uuid4(), user_id=uuid.uuid4(), session_id=session_id,
        source_event_id=1, kind="task_complete", status="pending",
        title="t", body=None, payload={}, action_ref=None,
        created_at=now, updated_at=now, read_at=None, responded_at=None,
    )


def test_serialize_item_includes_agent_fields():
    sid = uuid.uuid4()
    out = _serialize_item(_item(sid), {"agent_id": "agent-x", "agent_web_url": "https://x.example"})
    assert out["agent_id"] == "agent-x"
    assert out["agent_web_url"] == "https://x.example"


def test_serialize_item_defaults_agent_fields_to_none():
    out = _serialize_item(_item(uuid.uuid4()))
    assert out["agent_id"] is None
    assert out["agent_web_url"] is None


class _FakeStore:
    def __init__(self, mapping):
        self._mapping = mapping
    async def get_agent_ids_for_sessions(self, session_ids):
        return {s: self._mapping[s] for s in session_ids if s in self._mapping}


class _FakeCache:
    def __init__(self, urls):
        self._urls = urls
    async def get(self, agent_id):
        if agent_id not in self._urls:
            raise LookupError(agent_id)
        return {"api_web_url": self._urls[agent_id]}


def _request(store, cache):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_store=store, runtime_config_cache=cache)))


@pytest.mark.asyncio
async def test_resolve_agent_fields_maps_owner_and_url():
    sid = uuid.uuid4()
    req = _request(_FakeStore({sid: "agent-x"}), _FakeCache({"agent-x": "https://x.example"}))
    fields = await _resolve_agent_fields(req, [sid])
    assert fields[sid] == {"agent_id": "agent-x", "agent_web_url": "https://x.example"}


@pytest.mark.asyncio
async def test_resolve_agent_fields_url_none_on_cache_miss():
    sid = uuid.uuid4()
    req = _request(_FakeStore({sid: "agent-x"}), _FakeCache({}))
    fields = await _resolve_agent_fields(req, [sid])
    assert fields[sid] == {"agent_id": "agent-x", "agent_web_url": None}
