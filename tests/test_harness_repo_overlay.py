"""AgentHarness overlays runtime-projected repos onto the wake-local session.

``wake`` re-fetches the session from the store, so the repos passed to the
harness must be applied there — never written back to the persisted row — so
``/code`` and the coding tool can read ``session.config['repos']``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.sandbox.pool import SandboxPool
from surogates.session.models import Session
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry

_REPOS = ({"url": "https://github.com/acme/api", "default_branch": "main"},)


def _tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={}, user_preferences={}, permissions=frozenset(),
        asset_root="/tmp/test",
    )


def _session(config=None) -> Session:
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    return Session(
        id=uuid4(), org_id=uuid4(), agent_id="a1", channel="slack",
        status="active", config=config or {}, created_at=now, updated_at=now,
    )


def _harness(coding_repos=()) -> AgentHarness:
    return AgentHarness(
        session_store=AsyncMock(),
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=_tenant(),
        worker_id="test-worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        sandbox_pool=MagicMock(spec=SandboxPool),
        coding_repos=coding_repos,
    )


def test_overlay_sets_repos_on_a_copy():
    original = _session({"multi_party": True})
    out = _harness(_REPOS)._overlay_repos(original)
    assert out is not original
    assert out.config["repos"] == [
        {"url": "https://github.com/acme/api", "default_branch": "main"},
    ]
    assert out.config["multi_party"] is True  # existing keys preserved
    assert "repos" not in original.config  # persisted row untouched


def test_overlay_deep_copies_repo_dicts():
    repos = [{"url": "https://github.com/acme/api", "default_branch": "main"}]
    out = _harness(tuple(repos))._overlay_repos(_session())
    repos[0]["url"] = "mutated"
    assert out.config["repos"][0]["url"] == "https://github.com/acme/api"


def test_overlay_noop_when_no_repos():
    original = _session({"multi_party": True})
    out = _harness(())._overlay_repos(original)
    assert out is original  # no repos → no needless copy
