"""Ground-truth probes for the OpenAI-compatible API channel, against a real DB.

Not feature tests -- there is no feature yet. These pin the behaviour of the
EXISTING primitives an OpenAI facade would have to build on, so the design is
decided by measurement rather than by reading. Every assertion here is a fact
about production code today.

Run: .venv/bin/python -m pytest tests/integration/test_openai_channel_probe.py -q -s
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from uuid import UUID

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text

from surogates.channels.constants import (
    API_CHANNEL,
    END_USER_CHANNELS,
    SERVICE_ACCOUNT_CHANNELS,
)
from surogates.db.models import ServiceAccount
from surogates.db.models import Session as SessionRow
from surogates.session.events import EventType
from surogates.tenant.auth.service_account import ServiceAccountStore
from surogates.tenant.context import TenantContext

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def conversation_key(messages: list[dict]) -> str:
    """Recommended derivation: user turns only, whitespace-normalised.

    Hashes the conversation state being REPLIED TO (everything before the
    final user message), so it is stable against assistant-text drift --
    see the pure probe P6/P7.
    """
    users = [
        " ".join(str(m.get("content") or "").split())
        for m in messages
        if m.get("role") == "user"
    ]
    blob = json.dumps(users[:-1], separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


async def make_api_session(
    session_store,
    org_id: UUID,
    agent_id: str,
    *,
    service_account_id: UUID,
    idempotency_key: str | None = None,
    config: dict | None = None,
):
    return await session_store.create_session(
        session_id=uuid.uuid4(),
        user_id=None,
        org_id=org_id,
        agent_id=agent_id,
        channel=API_CHANNEL,
        model=None,
        config=config or {},
        service_account_id=service_account_id,
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# D1 — service accounts: how many can an agent actually have?
# ---------------------------------------------------------------------------

async def test_d1_partial_unique_index_caps_agent_at_one_service_account(
    session_factory,
):
    """The blocker for 'create tokens, delete tokens' on the runtime store."""
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    agent_id = "agent-openai-probe"

    first = await store.create(org_id=org_id, name="key-one", agent_id=agent_id)
    assert first.agent_id == agent_id

    with pytest.raises(Exception) as exc:
        await store.create(org_id=org_id, name="key-two", agent_id=agent_id)
    msg = str(exc.value)

    # ...but org-scoped accounts (agent_id NULL) are unlimited.
    a = await store.create(org_id=org_id, name="org-key-a")
    b = await store.create(org_id=org_id, name="org-key-b")
    assert a.id != b.id

    print(
        "\n[D1] GREEN (was RED) the one-per-agent cap now applies only to the "
        f"PRINCIPAL: {msg.splitlines()[0][:100]}\n"
        "     `create()` still infers kind='agent_principal' from a bare "
        "agent_id, so this historical call keeps its meaning; API keys pass "
        "kind='api_key' and are unbounded per agent "
        "(see test_agent_api_keys.py)."
    )


# ---------------------------------------------------------------------------
# D2 — is a token bound to the agent it was minted for?
# ---------------------------------------------------------------------------

async def test_d2_token_binding_is_now_carried_to_the_auth_layer(session_factory):
    """Originally RED: the auth view dropped ``agent_id`` entirely, so a key
    minted for agent A authenticated against agent B. Now GREEN — the binding
    survives resolution and ``enforce_agent_binding`` enforces it."""
    from surogates.tenant.auth.middleware import enforce_agent_binding
    from surogates.tenant.auth.service_account import KIND_API_KEY

    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)

    issued = await store.create(
        org_id=org_id, name="for-agent-a", agent_id="agent-a", kind=KIND_API_KEY,
    )
    resolved = await store.get_by_token(issued.token)
    assert resolved is not None
    assert resolved.agent_id == "agent-a"

    ctx = TenantContext(
        org_id=org_id, user_id=None, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/a",
        service_account_id=resolved.id,
        service_account_agent_id=resolved.agent_id,
    )
    from starlette.requests import Request

    def _req(target: str):
        return Request({
            "type": "http", "method": "GET", "path": "/v1/api/skills",
            "headers": [(b"host", b"localhost")],
            "query_string": f"agent_id={target}".encode(),
        })

    await enforce_agent_binding(_req("agent-a"), ctx)
    with pytest.raises(HTTPException) as exc:
        await enforce_agent_binding(_req("agent-b"), ctx)
    assert exc.value.status_code == 403

    org_scoped = await store.get_by_token(
        (await store.create(org_id=org_id, name="ops-chat")).token
    )
    assert org_scoped is not None and org_scoped.agent_id is None

    print(
        "\n[D2] GREEN ResolvedServiceAccount now carries agent_id; an "
        "agent-bound key is refused (403) against another agent, while "
        "org-scoped control-plane identities keep their org-wide reach."
    )


# ---------------------------------------------------------------------------
# D3 — revocation latency (matters for a 'Delete token' button)
# ---------------------------------------------------------------------------

async def test_d3_revocation_is_immediate_in_process(session_factory):
    org_id = await create_org(session_factory)
    store = ServiceAccountStore(session_factory)
    issued = await store.create(org_id=org_id, name="revoke-me")

    assert await store.get_by_token(issued.token) is not None
    assert await store.revoke(service_account_id=issued.id, org_id=org_id) is True
    assert await store.get_by_token(issued.token) is None

    # Second revoke is a no-op -> the UI must treat it as already-gone.
    assert await store.revoke(service_account_id=issued.id, org_id=org_id) is False
    print(
        "\n[D3] GREEN revoke() invalidates both caches in-process and the token "
        "stops resolving immediately. Peer replicas converge within the 60s "
        "TTL (_CACHE_TTL_SECONDS) -- so 'Delete' means 'dead here now, dead "
        "everywhere within a minute'. That must be said in the UI."
    )


# ---------------------------------------------------------------------------
# D4 — idempotency_key as the conversation key: indexed, unique, O(1)
# ---------------------------------------------------------------------------

async def test_d4_idempotency_key_works_as_a_conversation_key(
    session_factory, session_store,
):
    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(session_factory, org_id, name="openai")
    agent_id = "agent-conv"

    key = f"openai:{agent_id}:{conversation_key([{'role': 'user', 'content': 'q1'}])}"
    created = await make_api_session(
        session_store, org_id, agent_id,
        service_account_id=sa.id, idempotency_key=key,
    )

    found = await session_store.get_session_by_idempotency_key(org_id, key)
    assert found is not None and found.id == created.id

    # The partial unique index makes a duplicate impossible, which is exactly
    # the concurrency guard a conversation key needs.
    with pytest.raises(Exception):
        await make_api_session(
            session_store, org_id, agent_id,
            service_account_id=sa.id, idempotency_key=key,
        )

    miss = await session_store.get_session_by_idempotency_key(org_id, key + "-nope")
    assert miss is None

    print(
        "\n[D4] GREEN sessions.idempotency_key gives an indexed, unique, "
        "O(1) conversation lookup with a built-in race guard "
        "(uq_sessions_idempotency). No new column, no new index needed. "
        "Caveat: it is ORG-scoped, so the agent_id must be inside the key."
    )


# ---------------------------------------------------------------------------
# D5 — the alternative: config['channel_session_key'] has no index
# ---------------------------------------------------------------------------

async def test_d5_channel_session_key_lookup_is_unindexed(
    session_factory, session_store,
):
    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(session_factory, org_id, name="openai2")
    agent_id = "agent-scan"

    for i in range(25):
        await make_api_session(
            session_store, org_id, agent_id,
            service_account_id=sa.id,
            config={"channel_session_key": f"conv-{i}"},
        )

    async with session_factory() as db:
        plan = (await db.execute(text(
            "EXPLAIN SELECT * FROM sessions WHERE org_id = :o AND agent_id = :a "
            "AND channel = :c AND config->>'channel_session_key' = :k "
            "ORDER BY created_at DESC LIMIT 1"
        ), {"o": org_id, "a": agent_id, "c": API_CHANNEL, "k": "conv-7"})).fetchall()
    plan_txt = " | ".join(r[0].strip() for r in plan)

    print(
        f"\n[D5] YELLOW channel_session_key plan: {plan_txt[:180]}\n"
        "     Works, and it is what slack/telegram use, but the JSON path has "
        "no index -- fine at channel volume, wrong for API traffic. D4 wins."
    )


# ---------------------------------------------------------------------------
# D6 — stateful reuse: does resuming a completed session actually work?
# ---------------------------------------------------------------------------

async def test_d6_completed_session_can_be_resumed_and_keeps_history(
    session_factory, session_store,
):
    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(session_factory, org_id, name="openai3")
    s = await make_api_session(
        session_store, org_id, "agent-resume", service_account_id=sa.id,
    )

    await session_store.emit_event(s.id, EventType.USER_MESSAGE, {"content": "q1"})
    await session_store.emit_event(
        s.id, EventType.LLM_RESPONSE,
        {"message": {"role": "assistant", "content": "a1"}},
    )
    await session_store.emit_event(s.id, EventType.SESSION_COMPLETE, {
        "reason": "stop",
        "cost_summary": {
            "total_input_tokens": 900, "total_output_tokens": 120,
            "total_cache_read_tokens": 400, "total_reasoning_tokens": 40,
            "total_cost_usd": 0.003, "call_count": 1,
        },
    })
    await session_store.update_session_status(s.id, "completed")

    reloaded = await session_store.get_session(s.id)
    assert reloaded.status == "completed"

    from surogates.session.models import REUSABLE_SESSION_STATUSES
    assert "completed" in REUSABLE_SESSION_STATUSES

    await session_store.update_session_status(s.id, "active")
    await session_store.emit_event(s.id, EventType.SESSION_RESUME, {})
    await session_store.emit_event(s.id, EventType.USER_MESSAGE, {"content": "q2"})

    events = await session_store.get_events(s.id)
    kinds = [e.type for e in events]
    assert kinds.count(EventType.USER_MESSAGE.value) == 2

    complete = [e for e in events if e.type == EventType.SESSION_COMPLETE.value][0]
    usage = complete.data["cost_summary"]

    print(
        f"\n[D6] GREEN a completed api session resumes cleanly and keeps its "
        f"full log ({len(events)} events, {kinds.count('user.message')} user "
        f"turns). Per-turn usage is readable straight off session.complete: "
        f"prompt={usage['total_input_tokens']} completion="
        f"{usage['total_output_tokens']} reasoning="
        f"{usage['total_reasoning_tokens']}. "
        "This is the whole stateful mode, already built."
    )


# ---------------------------------------------------------------------------
# D7 — concurrency: two OpenAI requests on one conversation
# ---------------------------------------------------------------------------

async def test_d7_concurrent_requests_on_one_conversation(
    session_factory, session_store,
):
    org_id = await create_org(session_factory)
    sa = await issue_service_account_token(session_factory, org_id, name="openai4")
    agent_id = "agent-concurrent"
    key = f"openai:{agent_id}:race"

    async def attempt():
        try:
            s = await make_api_session(
                session_store, org_id, agent_id,
                service_account_id=sa.id, idempotency_key=key,
            )
            return ("created", s.id)
        except Exception as exc:
            return ("conflict", type(exc).__name__)

    results = await asyncio.gather(*(attempt() for _ in range(5)))
    created = [r for r in results if r[0] == "created"]
    conflicts = [r for r in results if r[0] == "conflict"]

    assert len(created) == 1, f"expected exactly one winner, got {results}"

    async with session_factory() as db:
        count = (await db.execute(
            select(func.count()).select_from(SessionRow).where(
                SessionRow.org_id == org_id, SessionRow.idempotency_key == key,
            )
        )).scalar_one()
    assert count == 1

    print(
        f"\n[D7] GREEN 5 concurrent creations on one conversation key -> "
        f"{len(created)} session, {len(conflicts)} conflicts "
        f"({conflicts[0][1] if conflicts else '-'}). The DB is the lock; the "
        "losers must re-read and reuse (prompts.py::_submit_one already does "
        "exactly this). No application-level locking needed for CREATION.\n"
        "     Still open: two concurrent MESSAGES into one live session -- "
        "the harness serialises on the session lease, so the second turn "
        "queues rather than interleaving."
    )


# ---------------------------------------------------------------------------
# D8 — billing: what an api session is missing vs a web session
# ---------------------------------------------------------------------------

async def test_d8_api_sessions_are_invisible_to_the_billing_and_users_planes(
    session_factory, session_store,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    sa = await issue_service_account_token(session_factory, org_id, name="openai5")
    agent_id = "agent-billing"

    api_s = await make_api_session(
        session_store, org_id, agent_id, service_account_id=sa.id,
    )
    web_s = await session_store.create_session(
        session_id=uuid.uuid4(), user_id=user_id, org_id=org_id,
        agent_id=agent_id, channel="web", model=None, config={},
    )

    assert api_s.user_id is None and api_s.service_account_id == sa.id
    assert web_s.user_id == user_id and web_s.service_account_id is None

    assert API_CHANNEL not in END_USER_CHANNELS
    assert "web" in END_USER_CHANNELS
    assert API_CHANNEL in SERVICE_ACCOUNT_CHANNELS

    from surogates.db.agent_users import BACKFILL_SQL
    assert "s.user_id IS NOT NULL" in BACKFILL_SQL
    assert "'api'" not in BACKFILL_SQL

    print(
        "\n[D8] RED  an api session has user_id=NULL, so it can NEVER enroll in "
        "agent_users (the enrollment SQL requires user_id IS NOT NULL) and "
        "'api' is not in END_USER_CHANNELS. Consequences: invisible on the "
        "Users page, no per-user memory scope, no per-user allowance "
        "authorize/settle, no commerce gate.\n"
        "     The fix is NOT to add 'api' to END_USER_CHANNELS -- see D9."
    )


# ---------------------------------------------------------------------------
# D9 — the billing fix already exists, and it is the website-embed pattern
# ---------------------------------------------------------------------------

async def test_d9_allowance_gate_is_channel_agnostic_and_takes_any_end_user_id(
    session_factory, session_store,
):
    import inspect

    from surogates.api.routes._commerce_turn import (
        authorize_allowance_turn,
        reserve_allowance,
    )

    sig = inspect.signature(reserve_allowance)
    assert "end_user_id" in sig.parameters
    assert "channel" in sig.parameters
    # `from __future__ import annotations` in the target module stringifies these.
    assert sig.parameters["end_user_id"].annotation == "str"
    assert sig.parameters["channel"].annotation == "str | None"

    # The HTTP wrapper the OpenAI route will actually call takes the same two,
    # and is what maps the domain outcomes onto 402 / 503.
    http_sig = inspect.signature(authorize_allowance_turn)
    assert "end_user_id" in http_sig.parameters
    assert "channel" in http_sig.parameters

    doc = inspect.getdoc(reserve_allowance) or ""
    assert "channel-agnostic" in doc.lower()

    # The website embed pins a buyer id onto session.config and bills THAT
    # party for anonymous visitor turns. Identical shape for an API token.
    org_id = await create_org(session_factory)
    buyer_id = await create_user(session_factory, org_id)
    sa = await issue_service_account_token(session_factory, org_id, name="openai6")
    s = await make_api_session(
        session_store, org_id, "agent-embedlike",
        service_account_id=sa.id,
        config={"embed_end_user_id": str(buyer_id)},
    )
    reloaded = await session_store.get_session(s.id)
    assert reloaded.config["embed_end_user_id"] == str(buyer_id)

    print(
        "\n[D9] GREEN reserve_allowance is explicitly channel-agnostic, takes "
        "end_user_id:str + channel:str, and is already shared by web / slack / "
        "telegram / website. An api turn only has to CALL it.\n"
        "     The identity model is already solved by the website embed: "
        "user_id stays NULL, and config['embed_end_user_id'] names the party "
        "who is billed. For an API key that party is the key's owner. "
        "=> 'billed like everything else' needs NO new billing machinery."
    )
