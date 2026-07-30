"""Integration tests for the session_search builtin."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from surogates.session.events import EventType
from surogates.tools.builtin.session_search import _session_search_handler

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _seed_student_session(
    session_store, session_factory, org_id, agent_id, content,
):
    """Create a user + a session owned by that user with one message."""
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=agent_id,
        channel="web",
    )
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": content},
    )
    return user_id, session


async def test_session_search_accepts_service_account_principal(
    session_store, session_factory,
):
    """API sessions should search prior sessions for the same service account."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id)

    session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-a",
        channel="api",
    )
    await session_store.emit_event(
        session.id,
        EventType.USER_MESSAGE,
        {"content": "session 2 was about deployment notes"},
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "deployment notes"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id,
                user_id=None,
                service_account_id=issued.id,
            ),
            agent_id="agent-a",
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(session.id)


async def test_owner_scoped_search_sees_end_user_sessions(
    session_store, session_factory,
):
    """An ops owner-console session (SA principal + ops config stamp)
    searches across every principal's sessions for the same org + agent."""
    org_id = await create_org(session_factory)
    other_org = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-owner",
    )

    _, student_session = await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "the magnesium electron configuration is 2 8 2",
    )
    # Same org, different agent -- must stay invisible.
    await _seed_student_session(
        session_store, session_factory, org_id, "agent-b",
        "the magnesium electron configuration is 2 8 2",
    )
    # Different org entirely -- must stay invisible.
    await _seed_student_session(
        session_store, session_factory, other_org, "agent-a",
        "the magnesium electron configuration is 2 8 2",
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="agent-a",
            session_config={"ops": {"user_id": "11111111-1111-1111-1111-111111111111"}},
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(student_session.id)


async def test_forged_ops_config_on_user_principal_stays_scoped(
    session_store, session_factory,
):
    """A user-principal session cannot widen its scope by stamping an
    ``ops`` block into its own config -- the gate requires a
    service-account principal, which end-user surfaces never get."""
    org_id = await create_org(session_factory)

    attacker_id, attacker_session = await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "attacker asking about magnesium",
    )
    _, victim_session = await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "victim discussing magnesium secrets",
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=attacker_id, service_account_id=None,
            ),
            agent_id="agent-a",
            session_config={"ops": {"user_id": str(attacker_id)}},
        )
    )

    assert result["success"] is True
    session_ids = {r["session_id"] for r in result["results"]}
    assert str(victim_session.id) not in session_ids
    assert session_ids == {str(attacker_session.id)}


async def test_service_account_without_ops_stamp_stays_scoped(
    session_store, session_factory,
):
    """A service-account principal without the ops config stamp keeps
    the existing behaviour: only its own sessions."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(session_factory, org_id)

    own_session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-a",
        channel="api",
    )
    await session_store.emit_event(
        own_session.id, EventType.USER_MESSAGE,
        {"content": "service account notes about magnesium"},
    )
    await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "student session about magnesium",
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="agent-a",
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(own_session.id)


async def test_owner_scoped_recent_mode_lists_end_user_sessions(
    session_store, session_factory,
):
    """Empty-query (recent) mode under owner scope lists end-user
    sessions and carries each session's user_id so the owner can tell
    principals apart."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-owner",
    )

    student_id, student_session = await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "hello I want to learn chemistry",
    )

    result = json.loads(
        await _session_search_handler(
            {},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="agent-a",
            session_config={"ops": {"user_id": "11111111-1111-1111-1111-111111111111"}},
        )
    )

    assert result["success"] is True
    assert result["mode"] == "recent"
    by_id = {r["session_id"]: r for r in result["results"]}
    assert str(student_session.id) in by_id
    assert by_id[str(student_session.id)]["user_id"] == str(student_id)


async def test_forged_ops_config_on_plain_service_account_stays_scoped(
    session_store, session_factory,
):
    """An org API service account (not an ops live-chat account) cannot
    widen its scope by stamping a forged ``ops`` block into its own
    session config -- the gate also requires the ops-chat account-name
    prefix that only surogate-ops's forwarder uses."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="partner-integration",
    )

    own_session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-a",
        channel="api",
    )
    await session_store.emit_event(
        own_session.id, EventType.USER_MESSAGE,
        {"content": "integration notes about magnesium"},
    )
    await _seed_student_session(
        session_store, session_factory, org_id, "agent-a",
        "student secrets about magnesium",
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="agent-a",
            session_config={"ops": {"user_id": "forged"}},
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(own_session.id)


async def test_system_agent_session_stays_scoped(
    session_store, session_factory,
):
    """System-agent console sessions (config.agent_type stamped by
    surogate-ops) stay private per user -- owner scope must not apply,
    matching the ops Sessions Explorer's forced scope='mine'."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-member",
    )

    own_session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="copilot",
        channel="web",
    )
    await session_store.emit_event(
        own_session.id, EventType.USER_MESSAGE,
        {"content": "my private copilot chat about magnesium"},
    )
    other = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-other",
    )
    other_session = await session_store.create_session(
        user_id=None,
        service_account_id=other.id,
        org_id=org_id,
        agent_id="copilot",
        channel="web",
    )
    await session_store.emit_event(
        other_session.id, EventType.USER_MESSAGE,
        {"content": "someone else's private copilot chat about magnesium"},
    )

    result = json.loads(
        await _session_search_handler(
            {"query": "magnesium"},
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=issued.id,
            ),
            agent_id="copilot",
            session_config={
                "ops": {"user_id": "11111111-1111-1111-1111-111111111111"},
                "agent_type": "surogate-platform-copilot",
            },
        )
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(own_session.id)


# ---------------------------------------------------------------------------
# What the search can actually reach
#
# Each test below pins one defect that made the shipped search silently
# incomplete: assistant text unreachable, recaps unsearched, a dead result
# key, role_filter ignored, streamed deltas ranked as matches, and a
# documented query syntax the implementation discarded.
# ---------------------------------------------------------------------------


async def _search(session_store, org_id, agent_id, principal_id, **args):
    return json.loads(
        await _session_search_handler(
            args,
            session_store=session_store,
            tenant=SimpleNamespace(
                org_id=org_id, user_id=None, service_account_id=principal_id,
            ),
            agent_id=agent_id,
        )
    )


async def _own_session(session_store, session_factory, org_id, agent_id):
    issued = await issue_service_account_token(session_factory, org_id)
    session = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id=agent_id,
        channel="api",
    )
    return issued, session


async def test_assistant_replies_are_searchable(session_store, session_factory):
    """``llm.response`` nests text under ``message``.

    The previous flat ``data->>'content'`` read made every assistant turn
    invisible, so a query whose words only appear in the agent's own reply
    returned nothing.
    """
    org_id = await create_org(session_factory)
    issued, session = await _own_session(
        session_store, session_factory, org_id, "agent-assistant",
    )
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "and then?"},
    )
    await session_store.emit_event(
        session.id,
        EventType.LLM_RESPONSE,
        {"message": {"role": "assistant", "content": "titration reaches equivalence"}},
    )

    result = await _search(
        session_store, org_id, "agent-assistant", issued.id, query="equivalence",
    )

    assert result["success"] is True
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(session.id)


async def test_turn_recaps_are_searchable(session_store, session_factory):
    """Recaps are the densest narrative stored and were never searched."""
    org_id = await create_org(session_factory)
    issued, session = await _own_session(
        session_store, session_factory, org_id, "agent-recap",
    )
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "ok"},
    )
    await session_store.emit_event(
        session.id,
        EventType.TURN_SUMMARY,
        {"turn_id": "t1", "recap": "worked through stoichiometry limiting reagents"},
        )

    result = await _search(
        session_store, org_id, "agent-recap", issued.id, query="reagents",
    )

    assert result["count"] == 1
    assert result["results"][0]["session_id"] == str(session.id)


async def test_tool_results_are_searchable_via_content(
    session_store, session_factory,
):
    """Result payloads carry ``content``; the old ``result`` key never existed."""
    org_id = await create_org(session_factory)
    issued, session = await _own_session(
        session_store, session_factory, org_id, "agent-tool",
    )
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "check it"},
    )
    await session_store.emit_event(
        session.id,
        EventType.TOOL_RESULT,
        {"tool_call_id": "c1", "name": "web_search", "content": "perchlorate solubility table"},
    )

    result = await _search(
        session_store, org_id, "agent-tool", issued.id, query="perchlorate",
    )

    assert result["count"] == 1


async def test_streamed_deltas_never_match(session_store, session_factory):
    """``llm.delta`` is one row per token chunk — it must stay out of scope."""
    org_id = await create_org(session_factory)
    issued, session = await _own_session(
        session_store, session_factory, org_id, "agent-delta",
    )
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "unrelated prompt"},
    )
    await session_store.emit_event(
        session.id, EventType.LLM_DELTA, {"content": "electronegativity"},
    )

    result = await _search(
        session_store, org_id, "agent-delta", issued.id, query="electronegativity",
    )

    assert result["success"] is True
    assert result["count"] == 0


async def test_role_filter_restricts_the_searched_events(
    session_store, session_factory,
):
    """``role_filter`` was parsed and then never applied."""
    org_id = await create_org(session_factory)
    issued, user_hit = await _own_session(
        session_store, session_factory, org_id, "agent-roles",
    )
    await session_store.emit_event(
        user_hit.id,
        EventType.USER_MESSAGE,
        {"content": "buffer capacity question"},
    )

    assistant_hit = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-roles",
        channel="api",
    )
    await session_store.emit_event(
        assistant_hit.id, EventType.USER_MESSAGE, {"content": "go on"},
    )
    await session_store.emit_event(
        assistant_hit.id,
        EventType.LLM_RESPONSE,
        {"message": {"role": "assistant", "content": "buffer capacity peaks at the pKa"}},
    )

    only_user = await _search(
        session_store, org_id, "agent-roles", issued.id,
        query="buffer capacity", role_filter="user",
    )
    only_assistant = await _search(
        session_store, org_id, "agent-roles", issued.id,
        query="buffer capacity", role_filter="assistant",
    )

    assert {r["session_id"] for r in only_user["results"]} == {str(user_hit.id)}
    assert {r["session_id"] for r in only_assistant["results"]} == {
        str(assistant_hit.id)
    }


async def test_unknown_role_filter_is_rejected(session_store, session_factory):
    org_id = await create_org(session_factory)
    issued, _ = await _own_session(
        session_store, session_factory, org_id, "agent-badrole",
    )

    result = await _search(
        session_store, org_id, "agent-badrole", issued.id,
        query="anything", role_filter="user,wizard",
    )

    assert result["success"] is False
    assert "wizard" in result["error"]


async def test_or_syntax_matches_partial_terms(session_store, session_factory):
    """The tool documents OR; ``plainto_tsquery`` used to AND everything."""
    org_id = await create_org(session_factory)
    issued, session = await _own_session(
        session_store, session_factory, org_id, "agent-or",
    )
    await session_store.emit_event(
        session.id,
        EventType.USER_MESSAGE,
        {"content": "we settled on baseten for inference"},
    )

    result = await _search(
        session_store, org_id, "agent-or", issued.id,
        query="elevenlabs OR baseten OR funding",
    )

    assert result["count"] == 1


async def test_quoted_phrase_requires_the_sequence(
    session_store, session_factory,
):
    org_id = await create_org(session_factory)
    issued, session = await _own_session(
        session_store, session_factory, org_id, "agent-phrase",
    )
    await session_store.emit_event(
        session.id,
        EventType.USER_MESSAGE,
        {"content": "networking inside docker was the blocker"},
    )

    exact = await _search(
        session_store, org_id, "agent-phrase", issued.id,
        query='"docker networking"',
    )
    loose = await _search(
        session_store, org_id, "agent-phrase", issued.id,
        query="docker networking",
    )

    assert exact["count"] == 0
    assert loose["count"] == 1


async def test_negation_excludes_a_term(session_store, session_factory):
    org_id = await create_org(session_factory)
    issued, python_only = await _own_session(
        session_store, session_factory, org_id, "agent-neg",
    )
    await session_store.emit_event(
        python_only.id, EventType.USER_MESSAGE, {"content": "ported the python parser"},
    )
    both = await session_store.create_session(
        user_id=None,
        service_account_id=issued.id,
        org_id=org_id,
        agent_id="agent-neg",
        channel="api",
    )
    await session_store.emit_event(
        both.id, EventType.USER_MESSAGE, {"content": "python and java interop notes"},
    )

    result = await _search(
        session_store, org_id, "agent-neg", issued.id, query="python -java",
    )

    assert {r["session_id"] for r in result["results"]} == {str(python_only.id)}
