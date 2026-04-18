"""Integration tests for the database-level observability layer.

Covers:
- ``events_populate_tenant`` trigger denormalizing ``org_id`` / ``user_id``
- Each view in ``surogates/db/observability.sql``
- Governance emission from ``execute_single_tool`` (``policy.denied`` /
  ``policy.allowed``) so that a refactor of ``GovernanceGate.check`` or
  of ``_PATH_ARGUMENT_MAP`` does not silently break the decision trail.

External BI and audit tools consume these views directly, so regressions
here break downstream integrations.  Every test sets up a minimal org +
user + session and exercises a single view or trigger behavior.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import text

from surogates.harness.tool_exec import execute_single_tool
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class _StubTenant:
    """Just enough of :class:`TenantContext` to satisfy ``execute_single_tool``."""

    org_id: UUID
    user_id: UUID


# ---------------------------------------------------------------------------
# Trigger: tenant denormalization
# ---------------------------------------------------------------------------


async def test_trigger_populates_org_and_user_from_session(
    session_store, session_factory,
):
    """Inserting an event without org_id/user_id fills them from sessions."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "hello"},
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT org_id, user_id FROM events WHERE session_id = :sid"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["org_id"] == org_id
    assert row["user_id"] == user_id


async def test_trigger_preserves_caller_values(session_factory):
    """An insert with explicit org_id/user_id is not overwritten by the trigger."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    other_org_id = await create_org(session_factory)
    other_user_id = await create_user(session_factory, other_org_id)

    # Create a session owned by (org_id, user_id)
    session_id = uuid.uuid4()
    async with session_factory() as db:
        await db.execute(
            text(
                "INSERT INTO sessions (id, user_id, org_id, agent_id) "
                "VALUES (:sid, :uid, :oid, 'test-agent')"
            ),
            {"sid": session_id, "uid": user_id, "oid": org_id},
        )
        await db.execute(
            text(
                "INSERT INTO session_cursors (session_id, harness_cursor) "
                "VALUES (:sid, 0)"
            ),
            {"sid": session_id},
        )
        # Insert the event with different tenant values — trigger must not clobber.
        await db.execute(
            text(
                "INSERT INTO events (session_id, org_id, user_id, type, data) "
                "VALUES (:sid, :oid, :uid, 'user.message', '{}'::jsonb)"
            ),
            {"sid": session_id, "oid": other_org_id, "uid": other_user_id},
        )
        await db.commit()

        row = (
            await db.execute(
                text(
                    "SELECT org_id, user_id FROM events WHERE session_id = :sid"
                ),
                {"sid": session_id},
            )
        ).mappings().one()

    assert row["org_id"] == other_org_id
    assert row["user_id"] == other_user_id


# ---------------------------------------------------------------------------
# v_session_tree
# ---------------------------------------------------------------------------


async def test_session_tree_root(session_store, session_factory):
    """A root session appears with depth 0 and itself as root."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT session_id, root_session_id, depth "
                    "FROM v_session_tree WHERE session_id = :sid"
                ),
                {"sid": root.id},
            )
        ).mappings().one()

    assert row["session_id"] == root.id
    assert row["root_session_id"] == root.id
    assert row["depth"] == 0


async def test_session_tree_nested_delegation(session_store, session_factory):
    """Expert-delegation sub-sessions walk up to a shared root with increasing depth."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    root = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )
    child = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent", parent_id=root.id,
    )
    grandchild = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent", parent_id=child.id,
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT session_id, root_session_id, depth, ancestor_path "
                    "FROM v_session_tree "
                    "WHERE session_id IN (:r, :c, :g) "
                    "ORDER BY depth"
                ),
                {"r": root.id, "c": child.id, "g": grandchild.id},
            )
        ).mappings().all()

    by_depth = {r["depth"]: r for r in rows}
    assert by_depth[0]["session_id"] == root.id
    assert by_depth[1]["session_id"] == child.id
    assert by_depth[2]["session_id"] == grandchild.id
    # All three share the same root.
    assert all(r["root_session_id"] == root.id for r in rows)
    # Path accumulates.
    assert list(by_depth[2]["ancestor_path"]) == [root.id, child.id, grandchild.id]


# ---------------------------------------------------------------------------
# v_tool_invocations
# ---------------------------------------------------------------------------


async def test_tool_invocations_joins_call_to_result(
    session_store, session_factory,
):
    """A tool.call + matching tool.result pair produces one row with both ids."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    call_id = await session_store.emit_event(
        session.id, EventType.TOOL_CALL,
        {"tool_call_id": "tc-1", "name": "terminal", "arguments": {"cmd": "ls"}},
    )
    result_id = await session_store.emit_event(
        session.id, EventType.TOOL_RESULT,
        {"tool_call_id": "tc-1", "name": "terminal",
         "content": "file.txt", "elapsed_ms": 42},
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT call_event_id, result_event_id, tool_name, "
                    "tool_call_id, elapsed_ms, result_content "
                    "FROM v_tool_invocations WHERE session_id = :sid"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["call_event_id"] == call_id
    assert row["result_event_id"] == result_id
    assert row["tool_name"] == "terminal"
    assert row["tool_call_id"] == "tc-1"
    assert row["elapsed_ms"] == 42
    assert row["result_content"] == "file.txt"


async def test_tool_invocations_result_null_when_missing(
    session_store, session_factory,
):
    """A tool.call with no matching result still appears, with result_event_id NULL."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await session_store.emit_event(
        session.id, EventType.TOOL_CALL,
        {"tool_call_id": "tc-orphan", "name": "terminal", "arguments": {}},
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT result_event_id, completed_at "
                    "FROM v_tool_invocations WHERE session_id = :sid"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["result_event_id"] is None
    assert row["completed_at"] is None


# ---------------------------------------------------------------------------
# v_tool_usage_daily
# ---------------------------------------------------------------------------


async def test_tool_usage_daily_aggregates(session_store, session_factory):
    """Multiple tool.call events roll up into a single day/user/tool row."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    for i in range(3):
        await session_store.emit_event(
            session.id, EventType.TOOL_CALL,
            {"tool_call_id": f"tc-{i}", "name": "terminal", "arguments": {}},
        )
    await session_store.emit_event(
        session.id, EventType.TOOL_CALL,
        {"tool_call_id": "tc-read", "name": "read_file", "arguments": {}},
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT tool_name, call_count FROM v_tool_usage_daily "
                    "WHERE org_id = :oid AND user_id = :uid "
                    "ORDER BY call_count DESC"
                ),
                {"oid": org_id, "uid": user_id},
            )
        ).mappings().all()

    counts = {r["tool_name"]: r["call_count"] for r in rows}
    assert counts == {"terminal": 3, "read_file": 1}


# ---------------------------------------------------------------------------
# v_policy_denials
# ---------------------------------------------------------------------------


async def test_policy_denials_exposes_tool_and_reason(
    session_store, session_factory,
):
    """A policy.denied event shows up with tool_name and reason projected."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await session_store.emit_event(
        session.id, EventType.POLICY_DENIED,
        {"tool": "shell_exec", "reason": "not in allow-list", "timestamp": 1.0},
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT tool_name, reason, agent_id "
                    "FROM v_policy_denials WHERE session_id = :sid"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["tool_name"] == "shell_exec"
    assert row["reason"] == "not in allow-list"
    assert row["agent_id"] == "test-agent"


# ---------------------------------------------------------------------------
# v_expert_outcomes
# ---------------------------------------------------------------------------


async def test_expert_outcomes_joins_delegation_result_and_feedback(
    session_store, session_factory,
):
    """A delegation → result → override chain collapses to one view row."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await session_store.emit_event(
        session.id, EventType.EXPERT_DELEGATION,
        {"expert": "sql_writer", "task": "find active users",
         "tools": ["terminal"], "max_iterations": 5},
    )
    result_id = await session_store.emit_event(
        session.id, EventType.EXPERT_RESULT,
        {"expert": "sql_writer", "success": True, "iterations_used": 3},
    )
    await session_store.emit_event(
        session.id, EventType.EXPERT_OVERRIDE,
        {
            "expert": "sql_writer",
            "target_event_id": result_id,
            "rating": "down",
            "rated_by_user_id": str(user_id),
            "reason": "wrong schema",
        },
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT expert_name, outcome_type, success, "
                    "iterations_used, feedback_type, feedback_rating, "
                    "feedback_reason "
                    "FROM v_expert_outcomes WHERE session_id = :sid"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["expert_name"] == "sql_writer"
    assert row["outcome_type"] == "expert.result"
    assert row["success"] is True
    assert row["iterations_used"] == 3
    assert row["feedback_type"] == "expert.override"
    assert row["feedback_rating"] == "down"
    assert row["feedback_reason"] == "wrong schema"


async def test_expert_outcomes_without_feedback(session_store, session_factory):
    """Delegation with result but no user feedback leaves feedback_* NULL."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await session_store.emit_event(
        session.id, EventType.EXPERT_DELEGATION,
        {"expert": "test_fixer", "task": "fix tests",
         "tools": [], "max_iterations": 3},
    )
    await session_store.emit_event(
        session.id, EventType.EXPERT_FAILURE,
        {"expert": "test_fixer", "success": False,
         "iterations_used": 3, "error": "budget exhausted"},
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT outcome_type, success, error, feedback_event_id "
                    "FROM v_expert_outcomes WHERE session_id = :sid"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["outcome_type"] == "expert.failure"
    assert row["success"] is False
    assert row["error"] == "budget exhausted"
    assert row["feedback_event_id"] is None


# ---------------------------------------------------------------------------
# v_training_candidates
# ---------------------------------------------------------------------------


async def test_training_candidates_flags_quality_signals(
    session_store, session_factory,
):
    """Quality flags light up when the corresponding events are present."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    clean = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )
    tainted = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    # Clean session: only a user message.
    await session_store.emit_event(
        clean.id, EventType.USER_MESSAGE, {"content": "hi"},
    )

    # Tainted session: policy denial + expert override + crash.
    await session_store.emit_event(
        tainted.id, EventType.POLICY_DENIED,
        {"tool": "x", "reason": "y", "timestamp": 1.0},
    )
    await session_store.emit_event(
        tainted.id, EventType.EXPERT_DELEGATION,
        {"expert": "e", "task": "t", "tools": [], "max_iterations": 1},
    )
    result_id = await session_store.emit_event(
        tainted.id, EventType.EXPERT_RESULT,
        {"expert": "e", "success": True, "iterations_used": 1},
    )
    await session_store.emit_event(
        tainted.id, EventType.EXPERT_OVERRIDE,
        {
            "expert": "e",
            "target_event_id": result_id,
            "rating": "down",
            "rated_by_user_id": str(user_id),
        },
    )
    await session_store.emit_event(
        tainted.id, EventType.HARNESS_CRASH, {"error": "boom"},
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT session_id, had_policy_denial, "
                    "had_expert_override, had_crash "
                    "FROM v_training_candidates "
                    "WHERE session_id IN (:c, :t)"
                ),
                {"c": clean.id, "t": tainted.id},
            )
        ).mappings().all()

    by_id = {r["session_id"]: r for r in rows}
    assert by_id[clean.id]["had_policy_denial"] is False
    assert by_id[clean.id]["had_expert_override"] is False
    assert by_id[clean.id]["had_crash"] is False
    assert by_id[tainted.id]["had_policy_denial"] is True
    assert by_id[tainted.id]["had_expert_override"] is True
    assert by_id[tainted.id]["had_crash"] is True


# ---------------------------------------------------------------------------
# v_session_messages
# ---------------------------------------------------------------------------


async def test_session_messages_includes_conversation_events(
    session_store, session_factory,
):
    """Message-shaped events surface; context.compact and harness.wake do not."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "/arxiv cuda"},
    )
    await session_store.emit_event(
        session.id, EventType.SKILL_INVOKED,
        {"skill": "arxiv", "raw_message": "/arxiv cuda", "staged_at": None},
    )
    await session_store.emit_event(
        session.id, EventType.LLM_RESPONSE,
        {"message": {"role": "assistant", "content": "hello"},
         "model": "gpt-4o", "input_tokens": 1, "output_tokens": 1},
    )
    await session_store.emit_event(
        session.id, EventType.HARNESS_WAKE, {},
    )
    await session_store.emit_event(
        session.id, EventType.CONTEXT_COMPACT,
        {"compacted_messages": [], "strategy": "summary"},
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT type FROM v_session_messages "
                    "WHERE session_id = :sid ORDER BY event_id"
                ),
                {"sid": session.id},
            )
        ).mappings().all()

    types = [r["type"] for r in rows]
    assert types == ["user.message", "skill.invoked", "llm.response"]


# ---------------------------------------------------------------------------
# v_response_feedback
# ---------------------------------------------------------------------------


async def test_response_feedback_joins_response_to_feedback(
    session_store, session_factory,
):
    """An llm.response with a matching user.feedback surfaces the rating."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent", model="gpt-4o",
    )

    response_id = await session_store.emit_event(
        session.id, EventType.LLM_RESPONSE,
        {
            "message": {"role": "assistant", "content": "42"},
            "model": "gpt-4o",
            "input_tokens": 1,
            "output_tokens": 1,
        },
    )
    await session_store.emit_event(
        session.id, EventType.USER_FEEDBACK,
        {
            "target_event_id": response_id,
            "rating": "up",
            "rated_by_user_id": str(user_id),
            "reason": "concise",
        },
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT response_event_id, response_content, model, "
                    "feedback_rating, feedback_reason "
                    "FROM v_response_feedback WHERE session_id = :sid"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["response_event_id"] == response_id
    assert row["response_content"] == "42"
    assert row["model"] == "gpt-4o"
    assert row["feedback_rating"] == "up"
    assert row["feedback_reason"] == "concise"


async def test_response_feedback_null_when_unrated(
    session_store, session_factory,
):
    """An llm.response without user.feedback still appears, with rating NULL."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    await session_store.emit_event(
        session.id, EventType.LLM_RESPONSE,
        {"message": {"role": "assistant", "content": "x"},
         "model": "gpt-4o", "input_tokens": 1, "output_tokens": 1},
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT feedback_event_id, feedback_rating "
                    "FROM v_response_feedback WHERE session_id = :sid"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["feedback_event_id"] is None
    assert row["feedback_rating"] is None


# ---------------------------------------------------------------------------
# execute_single_tool — POLICY_DENIED / POLICY_ALLOWED emission
#
# These tests drive the governance check site in ``tool_exec.py`` through
# the public entry point to catch silent regressions from refactors of
# ``GovernanceGate.check`` or ``_PATH_ARGUMENT_MAP``.  They do not
# exercise the downstream tool dispatch — the write_file handler is
# absent and produces an error-shaped tool_result, which is fine.  The
# assertion is on the governance event, not on the tool outcome.
# ---------------------------------------------------------------------------


async def _setup_governance_fixture(
    session_store, session_factory, workspace_path: str,
):
    """Create org + user + session + lease for governance emission tests.

    Returns ``(session, lease, org_id, user_id)`` with the lease already
    acquired so ``execute_single_tool``'s ``advance_harness_cursor``
    call succeeds.
    """
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
        config={"workspace_path": workspace_path},
    )
    lease = await session_store.try_acquire_lease(
        session.id, "worker-policy-test", ttl_seconds=60,
    )
    assert lease is not None
    return session, lease, org_id, user_id


async def test_policy_denied_event_on_path_escape(
    session_store, session_factory,
):
    """A write_file with a path outside workspace emits policy.denied."""
    session, lease, org_id, user_id = await _setup_governance_fixture(
        session_store, session_factory, "/workspace",
    )

    tc = {
        "id": "tc-escape-1",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({
                "path": "/etc/passwd",
                "content": "pwned",
            }),
        },
    }

    await execute_single_tool(
        tc,
        session=session,
        lease=lease,
        store=session_store,
        tools=ToolRegistry(),
        tenant=_StubTenant(org_id=org_id, user_id=user_id),
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT data FROM events "
                    "WHERE session_id = :sid AND type = 'policy.denied'"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["data"]["tool"] == "write_file"
    assert row["data"]["reason"]  # non-empty human-readable reason


async def test_policy_allowed_not_emitted_by_default(
    session_store, session_factory,
):
    """A safe write_file with ``log_policy_allowed=False`` emits no allow event."""
    session, lease, org_id, user_id = await _setup_governance_fixture(
        session_store, session_factory, "/tmp",
    )

    tc = {
        "id": "tc-allow-default",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({
                "path": "/tmp/safe.txt",
                "content": "ok",
            }),
        },
    }

    await execute_single_tool(
        tc,
        session=session,
        lease=lease,
        store=session_store,
        tools=ToolRegistry(),
        tenant=_StubTenant(org_id=org_id, user_id=user_id),
    )

    async with session_factory() as db:
        allowed = (
            await db.execute(
                text(
                    "SELECT COUNT(*) AS n FROM events "
                    "WHERE session_id = :sid AND type = 'policy.allowed'"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert allowed["n"] == 0


async def test_policy_allowed_emitted_when_log_allowed_true(
    session_store, session_factory,
):
    """A safe write_file with ``log_policy_allowed=True`` emits policy.allowed."""
    session, lease, org_id, user_id = await _setup_governance_fixture(
        session_store, session_factory, "/tmp",
    )

    tc = {
        "id": "tc-allow-on",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({
                "path": "/tmp/safe.txt",
                "content": "ok",
            }),
        },
    }

    await execute_single_tool(
        tc,
        session=session,
        lease=lease,
        store=session_store,
        tools=ToolRegistry(),
        tenant=_StubTenant(org_id=org_id, user_id=user_id),
        log_policy_allowed=True,
    )

    async with session_factory() as db:
        row = (
            await db.execute(
                text(
                    "SELECT data FROM events "
                    "WHERE session_id = :sid AND type = 'policy.allowed'"
                ),
                {"sid": session.id},
            )
        ).mappings().one()

    assert row["data"]["tool"] == "write_file"
    assert row["data"]["check"] == "workspace_sandbox"


async def test_training_candidates_thumbs_signals(
    session_store, session_factory,
):
    """had_response_thumbs_up/down flip when user.feedback events land."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    up_sess = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )
    down_sess = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    for sess, rating in [(up_sess, "up"), (down_sess, "down")]:
        resp_id = await session_store.emit_event(
            sess.id, EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": "x"},
             "model": "gpt-4o", "input_tokens": 1, "output_tokens": 1},
        )
        await session_store.emit_event(
            sess.id, EventType.USER_FEEDBACK,
            {"target_event_id": resp_id, "rating": rating,
             "rated_by_user_id": str(user_id)},
        )

    async with session_factory() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT session_id, had_response_thumbs_up, "
                    "had_response_thumbs_down "
                    "FROM v_training_candidates "
                    "WHERE session_id IN (:u, :d)"
                ),
                {"u": up_sess.id, "d": down_sess.id},
            )
        ).mappings().all()

    by_id = {r["session_id"]: r for r in rows}
    assert by_id[up_sess.id]["had_response_thumbs_up"] is True
    assert by_id[up_sess.id]["had_response_thumbs_down"] is False
    assert by_id[down_sess.id]["had_response_thumbs_up"] is False
    assert by_id[down_sess.id]["had_response_thumbs_down"] is True
