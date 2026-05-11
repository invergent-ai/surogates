"""Inbox hook tests for user-overridable governance denials."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select

from surogates.db.models import InboxItem
from surogates.governance.policy import PolicyDecision
from surogates.harness.tool_exec import execute_single_tool
from surogates.tools.registry import ToolRegistry

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


@dataclass
class _StubTenant:
    org_id: UUID
    user_id: UUID


async def _setup_tool_exec_session(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
        config={"workspace_path": "/workspace"},
    )
    lease = await session_store.try_acquire_lease(
        session.id,
        "worker-governance-test",
        ttl_seconds=60,
    )
    assert lease is not None
    return session, lease, org_id, user_id


async def test_overridable_denial_emits_inbox_governance_gate(
    session_store,
    session_factory,
    monkeypatch,
):
    def fake_check(self, tool_name, arguments=None, **kwargs):
        return PolicyDecision(
            allowed=False,
            reason="External recipient requires explicit approval.",
            tool_name=tool_name,
            overridable=True,
            policy_id="external-comms-v1",
        )

    monkeypatch.setattr(
        "surogates.governance.policy.GovernanceGate.check",
        fake_check,
    )
    session, lease, org_id, user_id = await _setup_tool_exec_session(
        session_store,
        session_factory,
    )

    result = await execute_single_tool(
        {
            "id": "tc-gov-1",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "path": "/outside-workspace.txt",
                    "content": "hello",
                }),
            },
        },
        session=session,
        lease=lease,
        store=session_store,
        tools=ToolRegistry(),
        tenant=_StubTenant(org_id=org_id, user_id=user_id),
    )

    assert result["tool_call_id"] == "tc-gov-1"

    async with session_factory() as db:
        row = (
            await db.execute(
                select(InboxItem).where(
                    InboxItem.session_id == session.id,
                    InboxItem.kind == "governance_gate",
                )
            )
        ).scalar_one()

    assert row.payload["tool_name"] == "write_file"
    assert row.payload["tool_call_id"] == "tc-gov-1"
    assert row.payload["policy_id"] == "external-comms-v1"
    assert "outside-workspace" in row.payload["arguments_excerpt"]
    assert row.action_ref["choices"] == ["approve", "reject"]


async def test_non_overridable_denial_does_not_emit_inbox(
    session_store,
    session_factory,
    monkeypatch,
):
    def fake_check(self, tool_name, arguments=None, **kwargs):
        return PolicyDecision(
            allowed=False,
            reason="Hard safety denial.",
            tool_name=tool_name,
            overridable=False,
        )

    monkeypatch.setattr(
        "surogates.governance.policy.GovernanceGate.check",
        fake_check,
    )
    session, lease, org_id, user_id = await _setup_tool_exec_session(
        session_store,
        session_factory,
    )

    await execute_single_tool(
        {
            "id": "tc-gov-2",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "path": "/outside-workspace.txt",
                    "content": "hello",
                }),
            },
        },
        session=session,
        lease=lease,
        store=session_store,
        tools=ToolRegistry(),
        tenant=_StubTenant(org_id=org_id, user_id=user_id),
    )

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(InboxItem).where(InboxItem.session_id == session.id)
            )
        ).scalars().all()

    assert rows == []
