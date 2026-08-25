"""The OpenAI-compatible routes, driven through the real application.

Everything here goes through the real middleware stack — auth, tenant,
rate-limit — so the tests prove a request actually reaches the handler with
authorization satisfied, not just that the handler works when called directly.

The agent's worker is not running, so a turn is completed by writing the
events the harness would have written. That keeps the assertions about THIS
layer: session resolution, reconciliation, the wire shape, and the guards.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from surogates.channels.constants import API_CHANNEL
from surogates.session.events import EventType
from surogates.session.store import SessionStore
from surogates.tenant.auth.jwt import create_access_token
from surogates.tenant.auth.service_account import KIND_API_KEY, ServiceAccountStore
from surogates.tenant.credentials import CredentialVault

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

AGENT = "openai-routes-agent"
OTHER_AGENT = "some-other-agent"


@pytest_asyncio.fixture(loop_scope="session")
async def app(session_factory, redis_client, pg_url, redis_url):
    os.environ["SUROGATES_DB_URL"] = pg_url
    os.environ["SUROGATES_REDIS_URL"] = redis_url

    from surogates.api.app import create_app
    from surogates.config import Settings
    from surogates.runtime import (
        agent_runtime_context_dep,
        build_agent_runtime_context,
    )
    from surogates.storage.backend import create_backend

    application = create_app()
    application.state.session_factory = session_factory
    application.state.redis = redis_client
    application.state.session_store = SessionStore(session_factory)
    application.state.settings = Settings()
    application.state.settings.storage.bucket = f"test-openai-{uuid.uuid4()}"
    application.state.storage = create_backend(application.state.settings)
    application.state.credential_vault = CredentialVault(
        session_factory, Fernet.generate_key(),
    )

    def _runtime_context():
        return build_agent_runtime_context({
            "agent_id": AGENT,
            "org_id": "00000000-0000-0000-0000-000000000000",
            "project_id": "test-project",
            "enabled": True,
            "version": 1,
            "storage_key_prefix": "",
        })

    application.dependency_overrides[agent_runtime_context_dep] = _runtime_context
    return application


@pytest_asyncio.fixture(loop_scope="session")
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c


@pytest_asyncio.fixture(loop_scope="session")
async def org_id(session_factory):
    return await create_org(session_factory)


@pytest_asyncio.fixture(loop_scope="session")
async def api_key(session_factory, org_id):
    """A live API key bound to the agent the app resolves to."""
    return await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="openai-test", agent_id=AGENT, kind=KIND_API_KEY,
    )


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def complete_turn(
    store: SessionStore, session_id, *, answer="Bucharest.", deltas=("Buch", "arest."),
    reasoning=None, usage=True, fail=None,
):
    """Write the events the harness would have written for one turn."""
    for chunk in deltas:
        await store.emit_event(session_id, EventType.LLM_DELTA, {"content": chunk})
    if reasoning:
        await store.emit_event(session_id, EventType.LLM_DELTA, {"reasoning": reasoning})
    if fail is None:
        await store.emit_event(
            session_id, EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": answer}},
        )
        data = {"reason": "stop"}
        if usage:
            data["cost_summary"] = {
                "total_input_tokens": 120, "total_output_tokens": 34,
                "total_cache_read_tokens": 80, "total_reasoning_tokens": 9,
                "total_cost_usd": 0.001, "call_count": 2,
            }
        await store.emit_event(session_id, EventType.SESSION_COMPLETE, data)
    else:
        await store.emit_event(session_id, EventType.SESSION_FAIL, {"error": fail})


async def answer_next_turn(app, session_factory, **kwargs):
    """Answer whichever session the next request creates or continues.

    Polls for a session with a pending user message, then writes the turn.
    Started before the request so the route's wait sees a finished turn.
    """
    store = SessionStore(session_factory)

    async def _run():
        from sqlalchemy import select

        from surogates.db.models import Session as SessionRow

        for _ in range(400):
            async with session_factory() as db:
                rows = (await db.execute(
                    select(SessionRow)
                    .where(SessionRow.agent_id == AGENT)
                    .order_by(SessionRow.created_at.desc())
                    .limit(5)
                )).scalars().all()
            for row in rows:
                events = await store.get_events(row.id)
                if not events:
                    continue
                if events[-1].type == EventType.USER_MESSAGE.value and not (
                    events[-1].data or {}
                ).get("synthetic"):
                    await complete_turn(store, row.id, **kwargs)
                    return row.id
            await asyncio.sleep(0.02)
        return None

    return asyncio.create_task(_run())


# ---------------------------------------------------------------------------
# auth + binding
# ---------------------------------------------------------------------------

async def test_models_lists_the_agent(client, api_key):
    r = await client.get("/v1/api/models", headers=auth(api_key.token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "list"
    assert body["data"][0]["id"] == AGENT
    assert body["data"][0]["object"] == "model"


async def test_a_missing_token_is_refused(client):
    r = await client.post("/v1/api/chat/completions", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert r.status_code == 401


async def test_an_interactive_jwt_cannot_use_the_openai_endpoint(
    client, session_factory, org_id,
):
    """The programmatic channel stays cleanly separated from the web one."""
    user_id = await create_user(session_factory, org_id)
    jwt = create_access_token(org_id, user_id, {"sessions:write"})
    r = await client.post(
        "/v1/api/chat/completions",
        headers=auth(jwt),
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403


async def test_a_key_bound_to_another_agent_is_refused(
    client, session_factory, org_id,
):
    """The agent comes from the Host header, so without this a customer's key
    reaches every sibling agent in the operator's org."""
    other = await ServiceAccountStore(session_factory).create(
        org_id=org_id, name="other", agent_id=OTHER_AGENT, kind=KIND_API_KEY,
    )
    r = await client.post(
        "/v1/api/chat/completions",
        headers=auth(other.token),
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403
    assert "different agent" in r.json()["error"]["message"]


async def test_a_revoked_key_stops_working(client, session_factory, org_id):
    store = ServiceAccountStore(session_factory)
    doomed = await store.create(
        org_id=org_id, name="doomed", agent_id=AGENT, kind=KIND_API_KEY,
    )
    assert (await client.get(
        "/v1/api/models", headers=auth(doomed.token),
    )).status_code == 200
    await store.revoke_api_key_for_agent(
        service_account_id=doomed.id, org_id=org_id, agent_id=AGENT,
    )
    assert (await client.get(
        "/v1/api/models", headers=auth(doomed.token),
    )).status_code == 401


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------

async def test_client_declared_tools_are_refused_with_a_readable_error(
    client, api_key,
):
    """Ignoring them would present as a hang, not a refusal."""
    r = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        },
    )
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "tools_not_supported"
    assert "runs its own tools" in error["message"]


async def test_errors_use_the_envelope_sdks_parse(client, api_key):
    """FastAPI's {"detail": ...} reaches the developer as an empty string."""
    r = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": []},
    )
    assert r.status_code == 400
    body = r.json()
    assert set(body) == {"error"}
    assert body["error"]["message"]
    assert body["error"]["type"] == "invalid_request_error"


async def test_an_unsupported_image_type_is_refused(client, api_key):
    r = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/tiff;base64,QUJD"}},
        ]}]},
    )
    assert r.status_code == 400
    assert "Unsupported image type" in r.json()["error"]["message"]


# ---------------------------------------------------------------------------
# a real turn
# ---------------------------------------------------------------------------

async def test_a_completion_runs_a_turn_and_reports_usage(
    client, app, session_factory, api_key,
):
    task = await answer_next_turn(app, session_factory, reasoning="thinking…")
    r = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [{"role": "user", "content": "Capital of Romania?"}]},
    )
    session_id = await task
    assert session_id is not None
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "Bucharest."
    assert body["choices"][0]["message"]["reasoning_content"] == "thinking…"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 120
    assert body["usage"]["completion_tokens"] == 34
    assert body["usage"]["completion_tokens_details"]["reasoning_tokens"] == 9
    assert r.headers["x-surogate-session"] == str(session_id)
    assert r.headers["x-surogate-conversation-action"] == "create"


async def test_the_session_is_an_api_channel_session_owned_by_the_key(
    client, app, session_factory, api_key,
):
    task = await answer_next_turn(app, session_factory)
    await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [{"role": "user", "content": "who owns me?"}]},
    )
    session_id = await task
    store = SessionStore(session_factory)
    session = await store.get_session(session_id)
    assert session.channel == API_CHANNEL
    assert session.user_id is None
    assert str(session.service_account_id) == str(api_key.id)


async def test_a_failed_turn_is_an_error_not_an_empty_answer(
    client, app, session_factory, api_key,
):
    """A 200 with empty content would be recorded as the agent's reply."""
    task = await answer_next_turn(app, session_factory, fail="upstream exploded")
    r = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [{"role": "user", "content": "break please"}]},
    )
    await task
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "agent_turn_failed"


# ---------------------------------------------------------------------------
# conversation continuity
# ---------------------------------------------------------------------------

async def test_a_second_turn_continues_the_same_session(
    client, app, session_factory, api_key,
):
    task = await answer_next_turn(app, session_factory, answer="Bucharest.")
    first = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [{"role": "user", "content": "Capital of Romania?"}]},
    )
    session_one = await task
    assert first.status_code == 200

    task = await answer_next_turn(app, session_factory, answer="1.7 million.")
    second = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [
            {"role": "user", "content": "Capital of Romania?"},
            {"role": "assistant", "content": "Bucharest."},
            {"role": "user", "content": "And its population?"},
        ]},
    )
    session_two = await task
    assert second.status_code == 200, second.text
    assert session_two == session_one, "the conversation must continue in place"
    assert second.headers["x-surogate-conversation-action"] == "append"


async def test_assistant_text_drift_does_not_fork_the_conversation(
    client, app, session_factory, api_key,
):
    """A client that re-renders the agent's answer must not lose continuity."""
    task = await answer_next_turn(app, session_factory, answer="Bucharest.")
    await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [{"role": "user", "content": "Capital?"}]},
    )
    session_one = await task

    task = await answer_next_turn(app, session_factory)
    second = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [
            {"role": "user", "content": "Capital?"},
            {"role": "assistant", "content": "  Bucharest.\n\n"},
            {"role": "user", "content": "population?"},
        ]},
    )
    assert await task == session_one
    assert second.headers["x-surogate-conversation-action"] == "append"


async def test_a_rewritten_history_forks_into_a_fresh_session(
    client, app, session_factory, api_key,
):
    """Appending would leave the stale turn and its answer in the agent's
    context, and it would answer with both still there."""
    task = await answer_next_turn(app, session_factory, answer="Bucharest.")
    await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [{"role": "user", "content": "Capital of Romania?"}]},
    )
    session_one = await task

    task = await answer_next_turn(app, session_factory)
    forked = await client.post(
        "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={"messages": [
            {"role": "user", "content": "Capital of Bulgaria?"},
            {"role": "assistant", "content": "Sofia."},
            {"role": "user", "content": "population?"},
        ]},
    )
    session_two = await task
    assert forked.status_code == 200, forked.text
    assert session_two != session_one
    assert forked.headers["x-surogate-conversation-action"] in {"create", "fork"}


async def test_two_end_users_behind_one_key_get_separate_sessions(
    client, app, session_factory, api_key,
):
    """The collision that scoping the key exists to prevent: without it both
    users share a session and each sees the other's turns."""
    sessions = []
    for who in ("alice", "bob"):
        task = await answer_next_turn(app, session_factory, answer="Hello!")
        await client.post(
            "/v1/api/chat/completions",
            headers=auth(api_key.token),
            json={"messages": [{"role": "user", "content": "Hi"}], "user": who},
        )
        sessions.append(await task)

    followups = []
    for who in ("alice", "bob"):
        task = await answer_next_turn(app, session_factory)
        r = await client.post(
            "/v1/api/chat/completions",
            headers=auth(api_key.token),
            json={
                "messages": [
                    {"role": "user", "content": "Hi"},
                    {"role": "assistant", "content": "Hello!"},
                    {"role": "user", "content": "who am I?"},
                ],
                "user": who,
            },
        )
        assert r.status_code == 200, r.text
        followups.append(await task)

    assert followups[0] != followups[1], "two end users must never share a session"
    assert set(followups) == set(sessions), "each must continue their own"


async def test_an_explicit_conversation_header_pins_the_session(
    client, app, session_factory, api_key,
):
    """The collision-free path: content stops mattering entirely."""
    task = await answer_next_turn(app, session_factory)
    await client.post(
        "/v1/api/chat/completions",
        headers={**auth(api_key.token), "X-Surogate-Conversation": "thread-7"},
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    session_one = await task

    task = await answer_next_turn(app, session_factory)
    second = await client.post(
        "/v1/api/chat/completions",
        headers={**auth(api_key.token), "X-Surogate-Conversation": "thread-7"},
        json={"messages": [{"role": "user", "content": "completely unrelated"}]},
    )
    assert second.status_code == 200, second.text
    assert await task == session_one


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------

async def test_streaming_emits_well_formed_chunks_and_terminates(
    client, app, session_factory, api_key,
):
    task = await answer_next_turn(
        app, session_factory, deltas=("Buch", "arest."), reasoning="hmm",
    )
    async with client.stream(
        "POST", "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={
            "messages": [{"role": "user", "content": "Capital?"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = "".join([chunk async for chunk in response.aiter_text()])
    await task

    lines = [
        line[6:] for line in raw.splitlines()
        if line.startswith("data: ")
    ]
    assert lines[-1] == "[DONE]", "a stream without [DONE] reads as truncated"
    frames = [json.loads(line) for line in lines[:-1]]
    assert all(f["object"] == "chat.completion.chunk" for f in frames)
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}

    text = "".join(
        f["choices"][0]["delta"].get("content", "")
        for f in frames if f["choices"]
    )
    assert text == "Bucharest."
    assert any(
        f["choices"] and f["choices"][0]["delta"].get("reasoning_content") == "hmm"
        for f in frames
    )
    finals = [
        f for f in frames
        if f["choices"] and f["choices"][0]["finish_reason"] is not None
    ]
    assert finals and finals[-1]["choices"][0]["finish_reason"] == "stop"
    usage_frames = [f for f in frames if "usage" in f]
    assert usage_frames and usage_frames[-1]["choices"] == []
    assert usage_frames[-1]["usage"]["prompt_tokens"] == 120


async def test_streaming_without_include_usage_omits_the_usage_frame(
    client, app, session_factory, api_key,
):
    task = await answer_next_turn(app, session_factory)
    async with client.stream(
        "POST", "/v1/api/chat/completions",
        headers=auth(api_key.token),
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as response:
        raw = "".join([chunk async for chunk in response.aiter_text()])
    await task
    frames = [
        json.loads(line[6:]) for line in raw.splitlines()
        if line.startswith("data: ") and line[6:] != "[DONE]"
    ]
    assert not any("usage" in f for f in frames)
    assert raw.rstrip().endswith("[DONE]")
