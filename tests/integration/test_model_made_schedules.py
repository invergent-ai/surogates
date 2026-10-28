"""A schedule a chat's model made is its user's like any ``/loop``: ``/loop list`` shows it, ``/loop cancel`` ends it."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.runtime import SlashCommandConfig
from surogates.sandbox.pool import SandboxPool
from surogates.scheduled.store import ScheduledSessionStore
from surogates.session.acting_principal import ActingPrincipal
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.cron import _cron_create_handler
from surogates.tools.registry import ToolRegistry
from tests.test_cron_tools import DIGEST, QUESTION, YES, FakeSessions, _preview
from tests.test_wake_slash_command_gate import _llm_responses, _session, _stub_store

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_loop_list_and_cancel_reach_a_schedule_the_model_made(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = _session()
    principal = ActingPrincipal(user_id=user_id, service_account_id=None)

    async def create(events):
        return json.loads(await _cron_create_handler(
            DIGEST,
            tenant=SimpleNamespace(org_id=org_id),
            agent_id=session.agent_id,
            session_id=str(session.id),
            session_store=FakeSessions(principal, events),
            session_factory=session_factory,
        ))

    shown = await create([])
    made = await create([_preview("cron_create", shown["would"]), QUESTION, YES])
    schedule_id = made["schedule"]["id"]

    events = _stub_store(session, [])
    harness = AgentHarness(
        session_store=events,
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=TenantContext(
            org_id=org_id, user_id=user_id, org_config={}, user_preferences={},
            permissions=frozenset(), asset_root="/tmp/test",
        ),
        worker_id="test-worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool),
        slash_commands=SlashCommandConfig(),
        session_factory=session_factory,
    )
    lease = SimpleNamespace(lease_token="lease-tok")
    await harness._handle_loop_command(session, "/loop list", lease)
    await harness._handle_loop_command(session, f"/loop cancel {schedule_id}", lease)

    listed, cancelled = _llm_responses(events)
    assert f"`{schedule_id}` " in listed and "Europe/Bucharest; until cancelled)" in listed
    assert cancelled == f"Loop {schedule_id} cancelled."
    assert await ScheduledSessionStore(session_factory).list_for_user(
        org_id=org_id, user_id=user_id, agent_id=session.agent_id,
    ) == []
