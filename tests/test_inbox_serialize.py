# Copyright (c) 2026, Invergent SA, developed by Flavius Burca
# SPDX-License-Identifier: AGPL-3.0-only
import uuid
from datetime import datetime, timedelta, timezone
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
    out = _serialize_item(_item(sid), {"agent_id": "agent-x", "agent_slug": "agent-x-slug"})
    assert out["agent_id"] == "agent-x"
    assert out["agent_slug"] == "agent-x-slug"


def test_serialize_item_dates_a_question_by_the_tools_wait():
    """The answer window is the server's rule; clients read it rather
    than each mirroring the constant."""
    from surogates.tools.builtin.ask_user_question import (
        ASK_USER_QUESTION_MAX_WAIT_SECONDS,
    )

    item = _item(uuid.uuid4())
    item.kind = "input_required"

    expires_at = _serialize_item(item)["expires_at"]

    assert expires_at == (
        item.created_at + timedelta(seconds=ASK_USER_QUESTION_MAX_WAIT_SECONDS)
    ).isoformat()


def test_serialize_item_leaves_kinds_without_a_deadline_open():
    """Only a question stops being actionable on a clock."""
    assert _serialize_item(_item(uuid.uuid4()))["expires_at"] is None


def test_serialize_item_keeps_a_missing_user_id_null():
    """Service-account-owned rows carry no user; str() would emit "None"."""
    item = _item(uuid.uuid4())
    item.user_id = None
    assert _serialize_item(item)["user_id"] is None


def test_serialize_item_defaults_agent_fields_to_none():
    out = _serialize_item(_item(uuid.uuid4()))
    assert out["agent_id"] is None
    assert out["agent_slug"] is None


class _FakeStore:
    def __init__(self, mapping):
        self._mapping = mapping
    async def get_agent_ids_for_sessions(self, session_ids):
        return {s: self._mapping[s] for s in session_ids if s in self._mapping}


class _FakeCache:
    def __init__(self, slugs):
        self._slugs = slugs
    async def get(self, agent_id):
        if agent_id not in self._slugs:
            raise LookupError(agent_id)
        return {"slug": self._slugs[agent_id]}


def _request(store, cache):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        session_store=store, runtime_config_cache=cache)))


@pytest.mark.asyncio
async def test_resolve_agent_fields_maps_owner_and_slug():
    sid = uuid.uuid4()
    req = _request(_FakeStore({sid: "agent-x"}), _FakeCache({"agent-x": "agent-x-slug"}))
    fields = await _resolve_agent_fields(req, [sid])
    assert fields[sid] == {"agent_id": "agent-x", "agent_slug": "agent-x-slug"}


@pytest.mark.asyncio
async def test_resolve_agent_fields_slug_none_on_cache_miss():
    sid = uuid.uuid4()
    req = _request(_FakeStore({sid: "agent-x"}), _FakeCache({}))
    fields = await _resolve_agent_fields(req, [sid])
    assert fields[sid] == {"agent_id": "agent-x", "agent_slug": None}
