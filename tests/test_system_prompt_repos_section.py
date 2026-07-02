"""AgentHarness appends the configured-repos section to the system prompt."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.sandbox.pool import SandboxPool
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry

pytestmark = pytest.mark.asyncio(loop_scope="session")

_REPO = {"url": "https://github.com/invergent-ai/surogate-ops.git", "default_branch": "main"}


def _tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={}, user_preferences={}, permissions=frozenset(),
        asset_root="/tmp/test",
    )


def _session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(), user_id=uuid4(), org_id=uuid4(), agent_id="a1",
        channel="slack", status="active", config={}, created_at=now, updated_at=now,
    )


def _harness(coding_repos) -> AgentHarness:
    pb = MagicMock(spec=PromptBuilder)
    pb.build.return_value = "BASE PROMPT"
    return AgentHarness(
        session_store=AsyncMock(),
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=_tenant(),
        worker_id="test-worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=pb,
        sandbox_pool=MagicMock(spec=SandboxPool),
        coding_repos=coding_repos,
    )


async def test_prompt_appends_repos_section():
    prompt = await _harness((_REPO,))._build_system_prompt(_session())
    assert prompt.startswith("BASE PROMPT")
    assert "invergent-ai/surogate-ops" in prompt
    assert "github" in prompt
    assert "https://github.com" in prompt  # the link instruction


async def test_prompt_unchanged_without_repos():
    prompt = await _harness(())._build_system_prompt(_session())
    assert prompt == "BASE PROMPT"
