"""Keel starts a person's case agent session through ``keel-link``, gives it the person's messages,
reads its status and its events, and stops its turn.

A case agent is one person's agent on one case.  Its session is owned by the
org's ``keel-case-agent`` account, never by the person, carries the case's
``case:`` memory boundary, and works in a scratch workspace of its own.  A
person waits on it, so its wakes queue beside people's, ahead of case work.
"""

from __future__ import annotations

import asyncio
import uuid
from uuid import UUID

import pytest
from sqlalchemy import select

from surogates.channels.constants import API_CHANNEL
from surogates.config import INTERRUPT_CHANNEL_PREFIX, SHARED_WORK_QUEUE_KEY, encode_queue_member
from surogates.db.models import ServiceAccount
from surogates.session.provisioning import create_agent_session, create_child_session
from surogates.session.store import SessionNotFoundError

from .conftest import create_org
from .test_events_stream import _read_sse_events
from .test_keel_case_files import person
from .test_keel_step_sessions import (  # noqa: F401  (link, link_app and org are fixtures)
    AGENT,
    LINK_KEY,
    code_of,
    completed_step,
    interrupt_listener,
    interrupts_heard,
    link,
    link_app,
    link_headers,
    org,
    queue_score,
    timeline,
    user_messages,
)
from .test_keel_step_sessions import body as step_body
from .test_keel_step_sessions import post as post_step
from .test_keel_watch import REQUEST, RESPONSE, delta, short_streams, written

pytestmark = pytest.mark.asyncio(loop_scope="session")

ROUTE = "/v1/keel/case-agent-sessions"
KEY = "keel:c:1:agent:1"
CASE_ID = "3a0c5e2f-7d41-4b8e-a9c6-1f2e3d4c5b6a"
QUESTION = "What income did the applicant declare?"
MESSAGE_KEY = "keel:c:1:agent:1:message:1"


def agent_body(org_id: UUID, person_id: UUID, key: str = KEY, *, session_id: UUID | None = None, **overrides) -> dict:
    """Keel's request to start one person's case agent on a loan case."""
    return {
        "org_id": str(org_id),
        "case_id": CASE_ID,
        "case_number": "LD-000001",
        "person_id": str(person_id),
        "idempotency_key": key,
        "session_id": str(session_id or uuid.uuid4()),
        **overrides,
    }


async def post(client, payload, *, agent: str = AGENT, auth: str | None = LINK_KEY):
    return await client.post(ROUTE, params={"agent_id": agent}, json=payload, headers=link_headers(auth))


def agent_route(session_id, action: str = "") -> str:
    return f"{ROUTE}/{session_id}" + (f"/{action}" if action else "")


async def send(client, session_id, *, auth: str | None = LINK_KEY, **overrides):
    message = {"text": QUESTION, "idempotency_key": MESSAGE_KEY, **overrides}
    return await client.post(agent_route(session_id, "messages"), json=message, headers=link_headers(auth))


async def nothing_made(session_store, org_id: UUID, payload: dict) -> bool:
    if await session_store.get_session_by_idempotency_key(org_id, payload["idempotency_key"]) is not None:
        return False
    try:
        await session_store.get_session(UUID(payload["session_id"]))
    except SessionNotFoundError:
        return True
    return False


async def case_agent(link, org: UUID, session_factory, session_store):
    """A person's case agent session, just made: it has no events and is not queued."""
    person_id, _ = await person(session_factory, org)
    created = await post(link, agent_body(org, person_id))
    assert created.status_code == 201, created.text
    return await session_store.get_session(UUID(created.json()["session_id"]))


async def unqueued(redis_client, session) -> None:
    await redis_client.zrem(
        SHARED_WORK_QUEUE_KEY,
        encode_queue_member(org_id=str(session.org_id), agent_id=session.agent_id, session_id=str(session.id)),
    )


# ---------------------------------------------------------------------------
# Starting it
# ---------------------------------------------------------------------------


async def test_keel_creates_a_case_agent_session_for_one_person(
    link, org, session_store, session_factory, redis_client,
):
    person_id, _ = await person(session_factory, org)
    request = agent_body(org, person_id)

    created = await post(link, request)

    assert created.status_code == 201, created.text
    assert created.json() == {"session_id": request["session_id"]}
    session = await session_store.get_session(UUID(request["session_id"]))
    assert session.channel == API_CHANNEL
    assert session.user_id is None
    async with session_factory() as db:
        owner = (await db.execute(
            select(ServiceAccount.org_id, ServiceAccount.name, ServiceAccount.kind)
            .where(ServiceAccount.id == session.service_account_id)
        )).one()
    assert tuple(owner) == (org, "keel-case-agent", "case_type")
    assert session.config["memory_boundary"] == f"case:{CASE_ID}"
    assert "workspace_boundary" not in session.config
    assert session.config["case_agent"] == {
        "case_id": CASE_ID, "case_number": "LD-000001", "person_id": str(person_id),
    }
    assert {"case_step", "case_item", "case_scope"} & set(session.config) == set()
    # Nothing is written or queued: the person's first message wakes it.
    assert await session_store.get_events(session.id) == []
    assert await queue_score(redis_client, session) is None


async def test_a_retry_finds_the_case_agent_session(link, org, session_store, session_factory):
    person_id, _ = await person(session_factory, org)
    other_person, _ = await person(session_factory, org)
    request = agent_body(org, person_id)

    first = await post(link, request)
    again = await post(link, request)

    assert first.status_code == 201, first.text
    assert (again.status_code, again.json()) == (200, {"session_id": request["session_id"]})
    assert (await session_store.get_session_by_idempotency_key(org, KEY)).id == UUID(request["session_id"])

    step = await post_step(link, step_body(org, "keel:c:1:0"))
    assert step.status_code == 201, step.text
    another_person = await post(link, {**request, "person_id": str(other_person)})
    a_steps_key = await post(
        link, agent_body(org, person_id, "keel:c:1:0", session_id=UUID(step.json()["session_id"])),
    )

    for refused in (another_person, a_steps_key):
        assert (refused.status_code, code_of(refused)) == (409, "key-in-use"), refused.text
    held = await session_store.get_session(UUID(request["session_id"]))
    assert held.config["case_agent"]["person_id"] == str(person_id)
    assert await session_store.get_events(held.id) == []


async def test_a_person_outside_the_org_is_not_a_member(link, org, session_store, session_factory):
    other_org = await create_org(session_factory)
    outsider, _ = await person(session_factory, other_org)
    payloads = [agent_body(org, outsider, "keel:c:1:agent:2"), agent_body(org, uuid.uuid4(), "keel:c:1:agent:3")]

    answers = [await post(link, payload) for payload in payloads]

    assert [(a.status_code, code_of(a)) for a in answers] == [(409, "not-a-member")] * 2
    for payload in payloads:
        assert await nothing_made(session_store, org, payload), payload["idempotency_key"]


async def test_unknown_and_stopped_agents_are_refused(link, org, session_store, session_factory):
    person_id, _ = await person(session_factory, org)
    request = agent_body(org, person_id)

    answers = [await post(link, request, agent=agent) for agent in ("nobody", "stopped-agent")]
    no_agent = await link.post(ROUTE, json=request, headers=link_headers(LINK_KEY))

    assert [(a.status_code, code_of(a)) for a in answers] == [(404, "unknown-agent"), (409, "agent-stopped")]
    assert (no_agent.status_code, code_of(no_agent)) == (422, "invalid-request")
    assert await nothing_made(session_store, org, request)


# ---------------------------------------------------------------------------
# The person's messages
# ---------------------------------------------------------------------------


async def test_a_persons_message_wakes_the_case_agent_ahead_of_case_work(
    link, org, session_store, session_factory, redis_client,
):
    agent = await case_agent(link, org, session_factory, session_store)

    sent = await send(link, agent.id)

    assert (sent.status_code, sent.json()) == (202, {"status": "queued"})
    [message] = await user_messages(session_store, agent.id)
    assert message.data == {"content": QUESTION, "synthetic": "keel_person_message", "keel_key": MESSAGE_KEY}

    await session_store.update_session_status(agent.id, "completed")
    await unqueued(redis_client, agent)

    second = await send(link, agent.id, text="And the guarantor's?", idempotency_key=f"{MESSAGE_KEY}:2")

    assert (second.status_code, second.json()) == (202, {"status": "queued"})
    assert await timeline(session_store, agent.id) == [
        ("user.message", "keel_person_message"),
        ("session.resume", None),
        ("user.message", "keel_person_message"),
    ]
    assert (await session_store.get_session(agent.id)).status == "active"
    # A person waits on it: its wakes queue beside people's, ahead of a step's.
    step = await completed_step(link, org, session_store, redis_client)
    answered = await link.post(
        f"/v1/keel/step-sessions/{step.id}/messages",
        json={"kind": "answer", "text": "The June payslip.", "note": None, "idempotency_key": "keel:c:1:0:answer:1"},
        headers=link_headers(LINK_KEY),
    )
    assert answered.status_code == 202, answered.text
    assert await queue_score(redis_client, agent) == 0.0
    assert await queue_score(redis_client, step) == 1.0


async def test_a_persons_message_is_delivered_once_per_key(link, org, session_store, session_factory, redis_client):
    agent = await case_agent(link, org, session_factory, session_store)
    first = await send(link, agent.id)
    assert first.status_code == 202, first.text
    await session_store.update_session_status(agent.id, "completed")
    await unqueued(redis_client, agent)
    before = await timeline(session_store, agent.id)

    again = await send(link, agent.id)

    assert (again.status_code, again.json()) == (202, {"status": "queued"})
    assert await timeline(session_store, agent.id) == before == [("user.message", "keel_person_message")]
    assert (await session_store.get_session(agent.id)).status == "completed"
    assert await queue_score(redis_client, agent) is None


async def test_cancel_stops_the_turn_and_the_next_message_wakes_it(
    link, org, session_store, session_factory, redis_client,
):
    agent = await case_agent(link, org, session_factory, session_store)
    sent = await send(link, agent.id)
    assert sent.status_code == 202, sent.text
    await unqueued(redis_client, agent)

    listener = await interrupt_listener(redis_client)
    try:
        cancelled = await link.post(agent_route(agent.id, "cancel"), headers=link_headers(LINK_KEY))
        heard = await interrupts_heard(listener)
    finally:
        await listener.aclose()

    assert (cancelled.status_code, cancelled.json()) == (200, {"status": "paused"})
    assert heard == [(f"{INTERRUPT_CHANNEL_PREFIX}:{agent.id}", {"reason": "case_step_cancelled"})]
    assert (await session_store.get_session(agent.id)).status == "paused"

    woken = await send(link, agent.id, text="Go on, please.", idempotency_key=f"{MESSAGE_KEY}:2")

    assert (woken.status_code, woken.json()) == (202, {"status": "queued"})
    assert await timeline(session_store, agent.id) == [
        ("user.message", "keel_person_message"),
        ("session.pause", None),
        ("session.resume", None),
        ("user.message", "keel_person_message"),
    ]
    assert (await session_store.get_session(agent.id)).status == "active"
    assert await queue_score(redis_client, agent) == 0.0


async def test_status_events_and_stream_of_a_case_agent_session(
    link, org, session_store, session_factory, redis_client, monkeypatch,
):
    short_streams(monkeypatch)
    agent = await case_agent(link, org, session_factory, session_store)
    sent = await send(link, agent.id)
    assert sent.status_code == 202, sent.text
    await written(session_store, agent.id, REQUEST, delta("The applicant"), delta(" declared 4,200."), RESPONSE)

    status = await link.get(agent_route(agent.id), headers=link_headers(LINK_KEY))
    page = await link.get(agent_route(agent.id, "events"), params={"after": 0}, headers=link_headers(LINK_KEY))

    assert (status.status_code, status.json()) == (200, {"session_id": str(agent.id), "status": "active"})
    assert page.status_code == 200, page.text
    events = page.json()["events"]
    assert [event["type"] for event in events] == ["user.message", "llm.request", "llm.response"]
    assert events[0]["data"]["content"] == QUESTION
    assert page.json()["has_more"] is False

    async def streamed() -> list[tuple[str, str]]:
        async with link.stream(
            "GET", agent_route(agent.id, "events/stream"), params={"after": 0}, headers=link_headers(LINK_KEY),
        ) as response:
            assert response.status_code == 200
            return await _read_sse_events(response, until_types={"stream.live"}, deadline_s=8.0)

    replayed = await asyncio.wait_for(streamed(), 10)

    assert [event_type for event_type, _data in replayed] == [
        "stream.start", "user.message", "llm.request", "llm.response", "stream.live",
    ]


# ---------------------------------------------------------------------------
# What the routes refuse
# ---------------------------------------------------------------------------


async def agent_routes_answer(link, session_id, *, auth: str | None = LINK_KEY) -> list:
    """What each route on one case agent session answers."""
    headers = link_headers(auth)
    return [
        await link.get(agent_route(session_id), headers=headers),
        await link.post(agent_route(session_id, "cancel"), headers=headers),
        await send(link, session_id, auth=auth),
        await link.get(agent_route(session_id, "events"), headers=headers),
        await link.get(agent_route(session_id, "events/stream"), headers=headers),
    ]


async def test_a_session_that_is_not_a_case_agents_is_unknown(
    link, link_app, org, session_store, session_factory, redis_client,
):
    agent = await case_agent(link, org, session_factory, session_store)
    step = await completed_step(link, org, session_store, redis_client)
    # A helper of the case agent's session is no case agent.
    helper = await create_child_session(store=session_store, parent=agent, channel=API_CHANNEL)

    async def made(**config) -> object:
        return await create_agent_session(
            store=session_store,
            storage=link_app.state.storage,
            settings=link_app.state.settings,
            org_id=org,
            user_id=None,
            agent_id=AGENT,
            channel=API_CHANNEL,
            **config,
        )

    chat = await made()
    # A case agent's mark outside its case's boundary is no case agent either.
    astray = await made(config={"memory_boundary": f"case:{uuid.uuid4()}"}, case_agent=agent.config["case_agent"])

    for session_id in (step.id, helper.id, chat.id, astray.id, uuid.uuid4(), "not-a-session-id"):
        answers = await agent_routes_answer(link, session_id)
        assert [(a.status_code, code_of(a)) for a in answers] == [(404, "unknown-session")] * 5, session_id

    assert await timeline(session_store, step.id) == [("user.message", "keel_step")]
    for session in (step, helper, chat, astray):
        assert (await session_store.get_session(session.id)).status == session.status
    for session in (helper, chat, astray):
        assert await timeline(session_store, session.id) == []


async def test_every_case_agent_route_needs_the_link_key(link, org, session_store, session_factory):
    agent = await case_agent(link, org, session_factory, session_store)
    person_id, _ = await person(session_factory, org)

    for key in (None, "not-the-link-key"):
        created = await post(link, agent_body(org, person_id, "keel:c:1:agent:9"), auth=key)
        answers = [created, *await agent_routes_answer(link, agent.id, auth=key)]
        assert [(a.status_code, code_of(a)) for a in answers] == [(401, "bad-link-key")] * 6

    assert await session_store.get_session_by_idempotency_key(org, "keel:c:1:agent:9") is None
    assert (await session_store.get_session(agent.id)).status == agent.status
    assert await session_store.get_events(agent.id) == []
