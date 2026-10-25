"""A person's case agent: one person's agent on one case, which reads the case only through Keel.

Keel starts it through the internal link, stamped with ``config.case_agent``
and the case's ``case:`` memory boundary.  Unlike a step's session it works in
a scratch workspace of its own, never the case's shared one, and its memory of
the case keeps no facts about people.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from surogates.channels.memory_boundary import session_memory_boundary
from surogates.memory.manager import MemoryManager
from surogates.memory.store import MemoryStore
from surogates.runtime.memory_protocol import memory_object_key
from surogates.session.provisioning import create_agent_session
from surogates.storage.tenant import boundary_workspace_prefix, storage_key_prefix
from surogates.tools.registry import ToolRegistry
from surogates.tools.runtime import ToolRuntime
from tests.test_case_step_isolation import CONTRACT, SCOPE, _SessionStore
from tests.test_eval_session_seeding import _route_fixtures

CASE = str(uuid4())
REF = {"case_id": CASE, "case_number": "LD-000001", "person_id": str(uuid4())}
REFUSED = {
    "success": False,
    "error": "This session keeps no facts about people: save to the 'memory' target.",
}


def _request():
    _, request, _, _ = _route_fixtures("/v1/api/sessions", service_account=True)
    request.app.state.settings.storage.key_prefix = "p/a"
    return request


async def _case_agent_session(request):
    """A case agent's session, made as the Keel link makes one."""
    return await create_agent_session(
        store=_SessionStore(),
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=uuid4(),
        user_id=None,
        agent_id="support-bot",
        channel="api",
        config={"memory_boundary": f"case:{CASE}"},
        service_account_id=uuid4(),
        case_agent=REF,
    )


async def _step_session(request):
    """A step's session on the same case, made as the Keel link makes one."""
    return await create_agent_session(
        store=_SessionStore(),
        storage=request.app.state.storage,
        settings=request.app.state.settings,
        org_id=uuid4(),
        user_id=None,
        agent_id="support-bot",
        channel="api",
        config={"memory_boundary": f"case:{CASE}", "workspace_boundary": f"case:{CASE}"},
        service_account_id=uuid4(),
        case_step={**CONTRACT, "case_id": CASE},
        case_scope=SCOPE,
    )


async def test_a_case_agent_works_in_a_scratch_workspace():
    request = _request()
    agent = await _case_agent_session(request)
    step = await _step_session(request)

    assert boundary_workspace_prefix(agent.config, agent, agent.id) == f"p/a/{agent.id}/"
    assert boundary_workspace_prefix(step.config, step, step.id) == f"p/a/boundaries/case:{CASE}/workspace/"
    assert memory_object_key(
        storage_key_prefix=storage_key_prefix(agent.config),
        user_id=None,
        boundary=session_memory_boundary(agent),
        target="memory",
    ) == f"p/a/boundaries/case:{CASE}/memory.json"


def _memory(tmp_path: Path) -> tuple[MemoryStore, MemoryManager]:
    store = MemoryStore(memory_dir=tmp_path / "mem")
    manager = MemoryManager(store)
    manager.initialize_all()
    return store, manager


async def _remember(arguments: dict, manager: MemoryManager, session_config: dict) -> dict:
    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    return json.loads(
        await registry.dispatch("memory", arguments, memory_manager=manager, session_config=session_config),
    )


async def test_a_case_agent_keeps_no_facts_about_people(tmp_path):
    request = _request()
    agent = await _case_agent_session(request)
    store, manager = _memory(tmp_path / "agent")
    about_a_person = {"action": "add", "target": "user", "content": "Ana prefers mornings"}

    refused = await _remember(about_a_person, manager, agent.config)
    noted = await _remember({**about_a_person, "target": "memory"}, manager, agent.config)

    assert refused == REFUSED
    assert store.user_entries == []
    assert noted["success"] is True
    assert store.memory_entries == ["Ana prefers mornings"]

    # Outside a case agent's session, the user target is saved.
    step = await _step_session(request)
    for config in (step.config, {"channel": "api"}):
        other_store, other_manager = _memory(tmp_path / str(uuid4()))
        saved = await _remember(about_a_person, other_manager, config)
        assert saved["success"] is True
        assert other_store.user_entries == ["Ana prefers mornings"]
