"""Seeded turns become real events without waking the worker."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response

from surogates.api.routes.sessions import SeedTurn, seed_turn_events
from surogates.session.events import EventType


def test_user_turn_becomes_a_user_message_event():
    events = seed_turn_events([SeedTurn(role="user", content="2+2?")])
    assert events == [(EventType.USER_MESSAGE, {"content": "2+2?"})]


def test_assistant_turn_becomes_an_llm_response_event():
    # The response contract is {"message": {"content": ...}}; a bare
    # {"content": ...} would not be read back as an assistant message.
    events = seed_turn_events([SeedTurn(role="assistant", content="4")])
    assert events == [(EventType.LLM_RESPONSE, {"message": {"content": "4"}})]


def test_order_is_preserved():
    events = seed_turn_events([
        SeedTurn(role="user", content="one"),
        SeedTurn(role="assistant", content="two"),
        SeedTurn(role="user", content="three"),
    ])
    assert [t for t, _ in events] == [
        EventType.USER_MESSAGE,
        EventType.LLM_RESPONSE,
        EventType.USER_MESSAGE,
    ]


def test_empty_seed_produces_no_events():
    assert seed_turn_events([]) == []
    assert seed_turn_events(None) == []


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        SeedTurn(role="system", content="x")


class _RecordingStore:
    def __init__(self):
        self.emitted = []

    async def emit_event(self, session_id, event_type, data):
        self.emitted.append((event_type, data))
        return len(self.emitted)

    async def create_session(self, **kwargs):
        return SimpleNamespace(
            id=kwargs["session_id"],
            org_id=kwargs["org_id"],
            agent_id=kwargs["agent_id"],
            channel=kwargs["channel"],
            status="active",
            model=kwargs["model"],
            config=kwargs["config"],
        )


async def test_seeded_turns_are_emitted_in_order():
    from surogates.api.routes.sessions import emit_seed_turns

    store = _RecordingStore()
    await emit_seed_turns(
        store,
        session_id="s-1",
        turns=[
            SeedTurn(role="user", content="one"),
            SeedTurn(role="assistant", content="two"),
        ],
    )
    assert store.emitted == [
        (EventType.USER_MESSAGE, {"content": "one"}),
        (EventType.LLM_RESPONSE, {"message": {"content": "two"}}),
    ]


async def test_no_seed_emits_nothing():
    from surogates.api.routes.sessions import emit_seed_turns

    store = _RecordingStore()
    await emit_seed_turns(store, session_id="s-1", turns=None)
    assert store.emitted == []


async def test_web_create_session_does_not_seed():
    # Only create_api_session seeds. The web route builds a session for a
    # human, where a caller-written transcript would be a forgery: drive
    # the actual web route with seed_turns set and confirm no seed events
    # ever reach the store, rather than asserting on the route's source.
    from surogates.api.routes import sessions as sessions_route
    from surogates.config import Settings
    from surogates.runtime import build_agent_runtime_context
    from surogates.tenant.context import TenantContext

    class _Storage:
        async def create_bucket(self, bucket):
            return None

        def resolve_workspace_path(self, bucket, session_id):
            return f"/bucket-root/{bucket}/{session_id}"

    org_id, user_id = uuid4(), uuid4()
    store = _RecordingStore()
    settings = Settings()
    settings.storage.bucket = "test-bucket"
    request = SimpleNamespace(
        url=SimpleNamespace(path="/v1/sessions"),
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=settings,
                session_store=store,
                storage=_Storage(),
                redis=None,
            ),
        ),
    )
    tenant = TenantContext(
        org_id=org_id,
        user_id=user_id,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/assets",
    )
    agent_runtime = build_agent_runtime_context({
        "agent_id": "support-bot",
        "org_id": str(org_id),
        "project_id": "test-project",
        "enabled": True,
        "version": 1,
        "storage_key_prefix": "",
        "multi_session": True,
    })

    await sessions_route.create_session(
        sessions_route.CreateSessionRequest(
            seed_turns=[SeedTurn(role="user", content="forged")],
        ),
        request,
        Response(),
        tenant,
        agent_runtime,
    )

    assert store.emitted == []
