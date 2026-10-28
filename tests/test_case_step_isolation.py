"""A case's sessions are marked by the server alone.

Keel starts an agent's step session with a contract (``config.case_step``), or
its session on a work item with the item's (``config.case_item``), a tool and
knowledge scope (``config.case_scope``) and the case's ``case:`` boundaries. A
client naming any of them would make itself a step or an item, widen its
knowledge, or read and write a case's memory and workspace, so only the Keel
link stamps them. Every session of the case, children included, keeps the
case's memory, while only the root session is the step or the item.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Response

from surogates.api.routes import sessions as sessions_route
from surogates.channels.constants import session_queue_priority
from surogates.channels.memory_boundary import (
    CASE_AGENT_KEY,
    CASE_RERUN_KEY,
    is_case_agent_session,
    rerun_kind_of,
    session_memory_boundary,
)
from surogates.config import Settings
from surogates.keel.items import is_case_item
from surogates.keel.steps import (
    CASE_ITEM_KEY,
    CASE_SCOPE_KEY,
    CASE_STEP_KEY,
    CaseScope,
    case_scope_of,
    is_case_session,
    is_case_step,
    is_rerun,
    step_contract_of,
)
from surogates.runtime.memory_protocol import memory_object_key
from surogates.session.models import Session
from surogates.session.provisioning import create_agent_session, create_child_session
from tests.test_eval_session_seeding import _RecordingStore, _route_fixtures

CONTRACT = {
    "case_id": str(uuid4()),
    "case_number": "LN-000001",
    "case_type": "loan",
    "type_version": 1,
    "token": "t2",
    "node": "check",
    "step": "Check completeness",
    "mode": "complete",
    "inputs": {"applicant": "Ioana", "amount": "182000.00"},
    "form": {
        "type": "object",
        "properties": {"docs_complete": {"type": "boolean"}},
        "additionalProperties": False,
    },
    "files": {"offer": "one"},
    "outcomes": {
        "complete": {"required": ["docs_complete", "income"]},
        "missing": {"required": []},
    },
    "checks": ["income must be positive"],
    "tools": None,
    "mcp_servers": None,
    "knowledge": ["kb-uuid"],
}
ITEM_CONTRACT = {
    **{key: CONTRACT[key] for key in ("case_id", "case_number", "case_type", "type_version", "token", "node", "step")},
    "item_id": "item-1",
    "kind": "raised_hand",
    "question": "Which payslip is the latest?",
}
CASE_AGENT = {"case_id": CONTRACT["case_id"], "case_number": "LN-000001", "person_id": str(uuid4())}
SCOPE = {"tools": ["write_file"], "knowledge": ["kb-uuid"]}


class _SessionStore(_RecordingStore):
    """Hands back whole session rows, so a child can be made from one."""

    async def create_session(self, **kwargs):
        self.created.append(dict(kwargs))
        now = datetime.now(UTC)
        session_id = kwargs.pop("session_id") or uuid4()
        return Session(id=session_id, status="active", created_at=now, updated_at=now, **kwargs)


async def _step_session(store, request, case: str, *, case_rerun: dict | None = None):
    """A step's root session, made as the Keel link makes one; a rerun's when *case_rerun* is given."""
    return await create_agent_session(
        store=store,
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=uuid4(),
        user_id=None,
        agent_id="support-bot",
        channel="api",
        config={"memory_boundary": f"case:{case}", "workspace_boundary": f"case:{case}"},
        service_account_id=uuid4(),
        case_step=CONTRACT,
        case_scope=SCOPE,
        case_rerun=case_rerun,
    )


async def _create_through(route: str, config: dict):
    if route == "api":
        _, request, tenant, agent_runtime = _route_fixtures("/v1/api/sessions", service_account=True)
        return await sessions_route.create_api_session(
            sessions_route.CreateSessionRequest(config=config), request, tenant, agent_runtime,
        )
    _, request, tenant, agent_runtime = _route_fixtures("/v1/sessions", service_account=False)
    return await sessions_route.create_session(
        sessions_route.CreateSessionRequest(config=config), request, Response(), tenant, agent_runtime,
    )


@pytest.mark.parametrize("route", ["api", "web"])
async def test_a_client_cannot_make_a_case_session(route):
    session = await _create_through(route, {
        CASE_STEP_KEY: CONTRACT,
        CASE_ITEM_KEY: ITEM_CONTRACT,
        CASE_AGENT_KEY: CASE_AGENT,
        CASE_SCOPE_KEY: {"tools": None, "knowledge": ["kb-of-another-project"]},
        CASE_RERUN_KEY: {"kind": "rerun"},
        "memory_boundary": "case:x",
        "workspace_boundary": "case:x",
    })

    kept = {
        CASE_STEP_KEY, CASE_ITEM_KEY, CASE_AGENT_KEY, CASE_SCOPE_KEY, CASE_RERUN_KEY,
        "memory_boundary", "workspace_boundary",
    } & set(session.config)
    assert kept == set()
    assert rerun_kind_of(session) is None
    assert not is_case_session(session)
    assert not is_case_step(session)
    assert not is_case_item(session)
    assert not is_case_agent_session(session)
    assert step_contract_of(session) is None
    assert case_scope_of(session) is None


async def test_a_case_sessions_memory_is_per_agent_and_case():
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    s = await _step_session(_SessionStore(), request, case="C1")

    assert memory_object_key(
        storage_key_prefix="p/a", user_id=None, boundary=session_memory_boundary(s), target="memory",
    ) == "p/a/boundaries/case:C1/memory.json"


async def test_a_child_inherits_the_case_and_its_scope_but_is_no_step():
    store = _SessionStore()
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    step = await _step_session(store, request, case="C1")
    assert is_case_step(step)
    assert step_contract_of(step).node == "check"

    child = await create_child_session(
        store=store,
        parent=step,
        channel="delegation",
        # What the code making the child names is never its scope or a contract.
        config={
            CASE_STEP_KEY: CONTRACT,
            CASE_ITEM_KEY: ITEM_CONTRACT,
            CASE_AGENT_KEY: CASE_AGENT,
            CASE_SCOPE_KEY: {"tools": None, "knowledge": ["kb-of-another-project"]},
        },
    )

    assert child.parent_id == step.id
    assert is_case_session(child)
    assert session_memory_boundary(child) == "case:C1"
    assert case_scope_of(child) == CaseScope(tools=["write_file"], knowledge=["kb-uuid"])
    assert not is_case_step(child)
    assert CASE_ITEM_KEY not in child.config
    assert CASE_AGENT_KEY not in child.config
    assert step_contract_of(child) is None


async def test_a_reruns_helper_is_a_rerun_too():
    store = _SessionStore()
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    root = await _step_session(store, request, case="C1", case_rerun={"kind": "rerun", "memory_prefix": "p/a"})
    plain = await _step_session(store, request, case="C1")

    # What the code making the child names is never its kind: the parent's is kept.
    child = await create_child_session(
        store=store, parent=root, channel="delegation", config={CASE_RERUN_KEY: {"kind": "trial"}},
    )
    plain_child = await create_child_session(
        store=store, parent=plain, channel="delegation", config={CASE_RERUN_KEY: {"kind": "rerun"}},
    )

    assert rerun_kind_of(root) == rerun_kind_of(child) == "rerun"
    assert child.config[CASE_RERUN_KEY] == {"kind": "rerun", "memory_prefix": "p/a"}
    assert is_rerun(root)
    assert not is_rerun(child)
    assert rerun_kind_of(plain) is None
    assert rerun_kind_of(plain_child) is None
    assert CASE_RERUN_KEY not in plain_child.config


async def test_reruns_wait_behind_case_work_and_trials_do_not():
    store = _SessionStore()
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    rerun = await _step_session(store, request, case="C1", case_rerun={"kind": "rerun"})
    trial = await _step_session(store, request, case="C1", case_rerun={"kind": "trial"})
    plain = await _step_session(store, request, case="C1")

    children = {
        kind: await create_child_session(store=store, parent=parent, channel="delegation")
        for kind, parent in (("rerun", rerun), ("trial", trial), ("plain", plain))
    }

    assert session_queue_priority(rerun) == session_queue_priority(children["rerun"]) == 2.0
    assert session_queue_priority(trial) == session_queue_priority(children["trial"]) == 0.0
    assert session_queue_priority(plain) == session_queue_priority(children["plain"]) == 1.0


def test_the_keys_never_show_in_settings(monkeypatch):
    monkeypatch.setenv("SUROGATES_KEEL_DOOR_KEY", "door-key-value-1234")
    monkeypatch.setenv("SUROGATES_KEEL_LINK_KEY", "link-key-value-5678")

    settings = Settings()

    assert (settings.keel.door_key, settings.keel.link_key) == ("door-key-value-1234", "link-key-value-5678")
    shown = repr(settings)
    assert "door-key-value-1234" not in shown
    assert "link-key-value-5678" not in shown
