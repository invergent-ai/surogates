"""End-to-end tests for user-overridable governance inbox decisions."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import select

from surogates.db.models import Event
from surogates.governance.policy import PolicyDecision
from surogates.harness.tool_exec import execute_single_tool
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry

from .inbox_e2e_helpers import StubTenant
from .inbox_e2e_helpers import app as app
from .inbox_e2e_helpers import client as client
from .inbox_e2e_helpers import create_user_token_session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_governance_approval_wakes_session(
    client,
    session_factory,
    session_store,
    monkeypatch,
):
    user_session = await create_user_token_session(
        session_factory,
        session_store,
        config={"workspace_path": "/workspace"},
    )
    lease = await session_store.try_acquire_lease(
        user_session.session.id,
        "worker-governance-e2e",
        ttl_seconds=60,
    )
    assert lease is not None

    def fake_check(self, tool_name, arguments=None, **kwargs):
        return PolicyDecision(
            allowed=False,
            reason="External write requires explicit approval.",
            tool_name=tool_name,
            overridable=True,
            policy_id="external-comms",
        )

    monkeypatch.setattr(
        "surogates.governance.policy.GovernanceGate.check",
        fake_check,
    )
    woken: list[UUID] = []

    async def fake_wake(request, session_id):
        woken.append(session_id)

    monkeypatch.setattr(
        "surogates.api.routes.inbox._wake_session_from_request",
        fake_wake,
    )

    result = await execute_single_tool(
        {
            "id": "tc-e2e-gov",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "path": "/outside-workspace.txt",
                    "content": "hello",
                }),
            },
        },
        session=user_session.session,
        lease=lease,
        store=session_store,
        tools=ToolRegistry(),
        tenant=StubTenant(
            org_id=user_session.org_id,
            user_id=user_session.user_id,
        ),
    )

    result_content = json.loads(result["content"])
    assert result_content["error"] == "policy_blocked_overridable"

    list_response = await client.get(
        (
            "/v1/inbox?kind=governance_gate"
            f"&session_id={user_session.session.id}"
        ),
        headers=user_session.auth_headers,
    )
    assert list_response.status_code == 200, list_response.text
    items = list_response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["status"] == "pending"
    assert item["payload"]["tool_name"] == "write_file"
    assert item["payload"]["tool_call_id"] == "tc-e2e-gov"

    response = await client.post(
        f"/v1/inbox/{item['id']}/respond",
        json={"decision": "approve"},
        headers=user_session.auth_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "responded"
    assert body["responded_at"] is not None
    assert woken == [user_session.session.id]

    async with session_store._sf() as db:
        user_message = (
            await db.execute(
                select(Event).where(
                    Event.session_id == user_session.session.id,
                    Event.type == EventType.USER_MESSAGE.value,
                )
            )
        ).scalar_one()

    assert user_message.data["source"] == "inbox_governance_decision"
    assert user_message.data["decision"] == "approve"
    assert user_message.data["tool_name"] == "write_file"
    assert user_message.data["tool_call_id"] == "tc-e2e-gov"
    assert "APPROVE" in user_message.data["content"]
