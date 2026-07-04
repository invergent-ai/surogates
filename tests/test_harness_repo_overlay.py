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


_SSH_TARGETS = (
    {"alias": "deploy", "host": "deploy.example.com", "port": 22,
     "user": "ubuntu", "key_name": "prod", "host_key": "deploy.example.com ssh-ed25519 AAAA"},
)


def _harness(coding_repos=(), ssh_targets=(), agent_service_account_id=None) -> AgentHarness:
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
        ssh_targets=ssh_targets,
        agent_service_account_id=agent_service_account_id,
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


def test_overlay_sets_ssh_targets_on_a_copy():
    original = _session({"multi_party": True})
    out = _harness(ssh_targets=_SSH_TARGETS)._overlay_repos(original)
    assert out is not original
    assert out.config["ssh_targets"] == [dict(_SSH_TARGETS[0])]
    assert out.config["multi_party"] is True
    assert "ssh_targets" not in original.config


def test_overlay_deep_copies_ssh_target_dicts():
    targets = [dict(_SSH_TARGETS[0])]
    out = _harness(ssh_targets=tuple(targets))._overlay_repos(_session())
    targets[0]["host"] = "mutated"
    assert out.config["ssh_targets"][0]["host"] == "deploy.example.com"


def test_overlay_noop_when_no_repos_or_targets():
    original = _session({"multi_party": True})
    out = _harness()._overlay_repos(original)
    assert out is original


def test_overlay_sets_both_repos_and_ssh_targets():
    out = _harness(_REPOS, _SSH_TARGETS)._overlay_repos(_session())
    assert out.config["repos"] and out.config["ssh_targets"]


def test_overlay_carries_agent_sa_with_ssh_targets():
    out = _harness(
        ssh_targets=_SSH_TARGETS, agent_service_account_id="sa-123",
    )._overlay_repos(_session())
    assert out.config["agent_service_account_id"] == "sa-123"


def test_overlay_omits_agent_sa_without_ssh_targets():
    # The agent SA id is only carried for SSH; a repos-only overlay omits it.
    out = _harness(
        _REPOS, agent_service_account_id="sa-123",
    )._overlay_repos(_session())
    assert "agent_service_account_id" not in out.config
