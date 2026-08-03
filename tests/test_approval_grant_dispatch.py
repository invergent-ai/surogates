"""A prior human approval lets the same call through on retry.

Before this, approving a blocked tool call emitted a chat message and nothing
else: `PolicyDecision.overridable` was never even set `True`, and the gate had
no way to learn a human had said yes. The call was blocked again identically.

The grant lookup lives in the async caller, not in `GovernanceGate.check`.
The gate is a frozen, synchronous, per-wake object shared across sessions with
no DB handle and no session context — putting a query in it is not possible,
and keeping it pure is what lets it stay shared.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from surogates.governance.policy import GovernanceGate
from surogates.harness.tool_exec import execute_single_tool
from surogates.session.events import EventType


def _store(granted: bool, reasons: list[str] | None = None) -> AsyncMock:
    store = AsyncMock()
    store.emit_event = AsyncMock(side_effect=range(1, 500))
    store.advance_harness_cursor = AsyncMock()
    store.consume_approval_grant = AsyncMock(
        return_value=(granted, reasons or []),
    )
    return store


async def _run(store, gate, tools=None):
    session = SimpleNamespace(id=uuid4(), config={}, agent_id="a", org_id=uuid4())
    return await execute_single_tool(
        {
            "id": "call_1",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": "deploy prod"}),
            },
        },
        session=session,
        lease=SimpleNamespace(lease_token=uuid4()),
        store=store,
        tools=tools if tools is not None else SimpleNamespace(
            get=lambda _name: None,  # no schema -> args pass through uncoerced
            execute=AsyncMock(return_value="ran"),
        ),
        tenant=SimpleNamespace(org_id=session.org_id),
        governance_gate=gate,
    )


@pytest.mark.asyncio
async def test_an_approved_call_is_let_through():
    store = _store(granted=True)
    gate = GovernanceGate(require_approval={"terminal"})

    await _run(store, gate)

    store.consume_approval_grant.assert_awaited_once()
    types = [c.args[1] for c in store.emit_event.await_args_list]
    assert EventType.POLICY_ALLOWED in types
    assert EventType.POLICY_DENIED not in types


@pytest.mark.asyncio
async def test_an_unapproved_call_is_still_blocked():
    store = _store(granted=False, reasons=["no_grant"])
    gate = GovernanceGate(require_approval={"terminal"})

    await _run(store, gate)

    types = [c.args[1] for c in store.emit_event.await_args_list]
    assert EventType.POLICY_DENIED in types
    assert EventType.INBOX_GOVERNANCE_GATE in types


@pytest.mark.asyncio
async def test_a_spent_grant_is_reported_as_a_near_miss():
    """"You already used that approval" is a different message to the
    operator than "you never had one"."""
    store = _store(granted=False, reasons=["exhausted"])
    gate = GovernanceGate(require_approval={"terminal"})

    await _run(store, gate)

    denied = [
        c.args[2] for c in store.emit_event.await_args_list
        if c.args[1] == EventType.POLICY_DENIED
    ]
    assert denied and "exhausted" in json.dumps(denied[0])


@pytest.mark.asyncio
async def test_a_grant_lookup_failure_fails_closed():
    """An unreadable grant is not an approval."""
    store = _store(granted=False)
    store.consume_approval_grant = AsyncMock(side_effect=RuntimeError("db blip"))
    gate = GovernanceGate(require_approval={"terminal"})

    await _run(store, gate)

    types = [c.args[1] for c in store.emit_event.await_args_list]
    assert EventType.POLICY_DENIED in types


@pytest.mark.asyncio
async def test_a_hard_denial_never_consults_a_grant():
    """Only an overridable denial is approvable; a deny-list hit is not."""
    store = _store(granted=True)
    gate = GovernanceGate(denied_tools={"terminal"})

    await _run(store, gate)

    store.consume_approval_grant.assert_not_awaited()
    types = [c.args[1] for c in store.emit_event.await_args_list]
    assert EventType.POLICY_DENIED in types


@pytest.mark.asyncio
async def test_an_allowed_call_never_consults_a_grant():
    """The happy path must not pay for a query it does not need."""
    store = _store(granted=True)

    await _run(store, GovernanceGate())

    store.consume_approval_grant.assert_not_awaited()
