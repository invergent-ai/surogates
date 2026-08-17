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
    assert events == [
        (EventType.USER_MESSAGE, {"content": "2+2?", "synthetic": "seed"}),
    ]


def test_assistant_turn_becomes_an_llm_response_event():
    # The response contract is {"message": {"role": ..., "content": ...}}; the
    # context replay appends that dict verbatim into the provider's messages
    # array, which rejects a message carrying no role.
    events = seed_turn_events([SeedTurn(role="assistant", content="4")])
    assert events == [(
        EventType.LLM_RESPONSE,
        {
            "message": {"role": "assistant", "content": "4"},
            "synthetic": "seed",
        },
    )]


def test_both_seeded_roles_are_marked_synthetic():
    # The facade reads the last llm.response back as the agent's answer; without
    # a marker a seeded turn is indistinguishable from a real one and a row
    # whose real turn died would be graded against its own recorded answer.
    events = seed_turn_events([
        SeedTurn(role="user", content="q"),
        SeedTurn(role="assistant", content="a"),
    ])
    assert [data["synthetic"] for _, data in events] == ["seed", "seed"]


def test_seeded_assistant_message_replays_with_a_role():
    # Guards the wire shape end-to-end: the replay builder must produce a
    # messages array every OpenAI-compatible provider accepts.
    from surogates.harness.loop_context_replay import ContextReplayMixin

    events = [
        SimpleNamespace(
            type=event_type.value, data=data, id=index,
        )
        for index, (event_type, data) in enumerate(seed_turn_events([
            SeedTurn(role="user", content="one"),
            SeedTurn(role="assistant", content="two"),
        ]))
    ]
    messages = ContextReplayMixin._rebuild_messages(
        ContextReplayMixin(), events, workspace_path="",
    )
    assert [m.get("role") for m in messages] == ["user", "assistant"]


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
        (EventType.USER_MESSAGE, {"content": "one", "synthetic": "seed"}),
        (
            EventType.LLM_RESPONSE,
            {
                "message": {"role": "assistant", "content": "two"},
                "synthetic": "seed",
            },
        ),
    ]


async def test_no_seed_emits_nothing():
    from surogates.api.routes.sessions import emit_seed_turns

    store = _RecordingStore()
    await emit_seed_turns(store, session_id="s-1", turns=None)
    assert store.emitted == []


class _Storage:
    async def create_bucket(self, bucket):
        return None

    def resolve_workspace_path(self, bucket, session_id):
        return f"/bucket-root/{bucket}/{session_id}"


def _route_fixtures(path: str, *, service_account: bool):
    """Request / tenant / runtime triple for driving a real session route."""
    from surogates.config import Settings
    from surogates.runtime import build_agent_runtime_context
    from surogates.tenant.context import TenantContext

    org_id = uuid4()
    store = _RecordingStore()
    settings = Settings()
    settings.storage.bucket = "test-bucket"
    request = SimpleNamespace(
        url=SimpleNamespace(path=path),
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
        user_id=None if service_account else uuid4(),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/assets",
        service_account_id=uuid4() if service_account else None,
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
    return store, request, tenant, agent_runtime


async def test_web_create_session_does_not_seed():
    # Only create_api_session seeds. The web route builds a session for a
    # human, where a caller-written transcript would be a forgery: drive
    # the actual web route with seed_turns set and confirm no seed events
    # ever reach the store, rather than asserting on the route's source.
    from surogates.api.routes import sessions as sessions_route

    store, request, tenant, agent_runtime = _route_fixtures(
        "/v1/sessions", service_account=False,
    )

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


async def test_api_session_seeds_only_when_it_is_an_evaluation():
    # Seeding skips the prompt-injection screen every ordinary message goes
    # through, so it is confined to a session the server itself stamped with
    # an evaluation boundary.
    from surogates.api.routes import sessions as sessions_route

    store, request, tenant, agent_runtime = _route_fixtures(
        "/v1/api/sessions", service_account=True,
    )

    await sessions_route.create_api_session(
        sessions_route.CreateSessionRequest(
            config={"eval_run_id": "run-1"},
            seed_turns=[SeedTurn(role="user", content="one")],
        ),
        request,
        tenant,
        agent_runtime,
    )
    assert [t for t, _ in store.emitted] == [EventType.USER_MESSAGE]


async def test_api_session_without_an_eval_run_id_cannot_seed():
    from fastapi import HTTPException

    from surogates.api.routes import sessions as sessions_route

    store, request, tenant, agent_runtime = _route_fixtures(
        "/v1/api/sessions", service_account=True,
    )

    with pytest.raises(HTTPException) as exc:
        await sessions_route.create_api_session(
            sessions_route.CreateSessionRequest(
                seed_turns=[SeedTurn(role="user", content="one")],
            ),
            request,
            tenant,
            agent_runtime,
        )
    assert exc.value.status_code == 422
    assert store.emitted == []


def test_too_many_seed_turns_is_rejected():
    from pydantic import ValidationError

    from surogates.api.routes.sessions import (
        MAX_SEED_TURNS,
        CreateSessionRequest,
    )

    turns = [
        SeedTurn(role="user", content="x") for _ in range(MAX_SEED_TURNS + 1)
    ]
    with pytest.raises(ValidationError):
        CreateSessionRequest(seed_turns=turns)

    CreateSessionRequest(seed_turns=turns[:MAX_SEED_TURNS])


def test_oversized_seed_content_is_rejected():
    from pydantic import ValidationError

    from surogates.api.routes.sessions import (
        MAX_SEED_CONTENT_LENGTH,
        CreateSessionRequest,
    )

    half = MAX_SEED_CONTENT_LENGTH // 2
    with pytest.raises(ValidationError):
        CreateSessionRequest(seed_turns=[
            SeedTurn(role="user", content="x" * (half + 1)),
            SeedTurn(role="assistant", content="y" * (half + 1)),
        ])

    CreateSessionRequest(seed_turns=[
        SeedTurn(role="user", content="x" * half),
        SeedTurn(role="assistant", content="y" * half),
    ])
