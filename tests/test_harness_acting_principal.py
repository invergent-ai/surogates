"""The harness carries the acting (sender) principal distinctly from the tenant.

On a managed channel the tenant is the agent's own service account (it acts as
itself), but automation the sender creates (/loop, /mission, /auto-research) must
be owned by the sender — the acting principal — not the agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from surogates.harness.budget import IterationBudget
from surogates.harness.context import ContextCompressor
from surogates.harness.loop import AgentHarness
from surogates.harness.prompt import PromptBuilder
from surogates.session.acting_principal import ActingPrincipal
from surogates.tenant.context import TenantContext
from surogates.tools.registry import ToolRegistry


def _tenant(*, user_id=None, service_account_id=None):
    return TenantContext(
        org_id=uuid4(), user_id=user_id, org_config={}, user_preferences={},
        permissions=frozenset(), asset_root="/tmp/test",
        service_account_id=service_account_id,
    )


def _harness(tenant, acting_principal=None):
    return AgentHarness(
        session_store=AsyncMock(),
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=tenant,
        worker_id="w",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(spec=ContextCompressor),
        prompt_builder=MagicMock(spec=PromptBuilder),
        acting_principal=acting_principal,
    )


def test_acting_principal_defaults_to_tenant_when_unset():
    uid = uuid4()
    h = _harness(_tenant(user_id=uid))
    assert h._acting_principal == ActingPrincipal(user_id=uid, service_account_id=None)


def test_acting_principal_overrides_tenant_on_managed_channel():
    agent_sa = uuid4()
    human = uuid4()
    h = _harness(
        _tenant(service_account_id=agent_sa),
        acting_principal=ActingPrincipal(user_id=human, service_account_id=None),
    )
    # The agent still acts as itself (tenant = its service account) ...
    assert h._tenant.service_account_id == agent_sa
    assert h._tenant.user_id is None
    # ... but the sender (owner of any automation) is the human.
    assert h._acting_principal == ActingPrincipal(user_id=human, service_account_id=None)
