"""The Programs capability gates the follow-up tools end to end."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from surogates.harness.tool_schemas import channel_tool_flags, drop_unusable_tools
from surogates.orchestrator.worker import _filter_effective_tools
from surogates.runtime.resolver import build_agent_runtime_context
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.followup import FOLLOWUP_TOOL_NAMES


def _tenant():
    return TenantContext(
        org_id=uuid.uuid4(), user_id=uuid.uuid4(), org_config={},
        user_preferences={}, permissions=frozenset(), asset_root="/tmp",
    )


def _session():
    return SimpleNamespace(config={}, channel="whatsapp")


def _effective(programs_enabled: bool) -> set[str]:
    return _filter_effective_tools(
        tools=set(FOLLOWUP_TOOL_NAMES) | {"checkin_outcome"},
        tenant=_tenant(),
        session=_session(),
        use_api_for_harness_tools=False,
        programs_enabled=programs_enabled,
    )


def test_tools_ship_only_with_the_programs_capability():
    assert FOLLOWUP_TOOL_NAMES <= _effective(True)
    assert not FOLLOWUP_TOOL_NAMES & _effective(False)


def test_checkin_outcome_is_not_gated():
    # Unchanged from before the capability existed: an agent mid-check-in
    # must always be able to close it.
    assert "checkin_outcome" in _effective(False)


def test_runtime_config_reads_the_capability_and_defaults_off():
    base = {
        "agent_id": "a1", "org_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()), "enabled": True, "version": 1,
        "storage_key_prefix": "p/",
    }
    assert build_agent_runtime_context(
        {**base, "programs_enabled": True},
    ).programs_enabled is True
    assert build_agent_runtime_context(base).programs_enabled is False


def test_enrolment_tools_need_a_channel_but_escalate_does_not():
    schemas = [{"function": {"name": n}} for n in sorted(FOLLOWUP_TOOL_NAMES)]
    flags = {"has_kbs": False, "makes_routines": False}
    kept = drop_unusable_tools(schemas, **flags, **channel_tool_flags("web"))
    assert [s["function"]["name"] for s in kept] == ["escalate"]
    kept = drop_unusable_tools(schemas, **flags, **channel_tool_flags("whatsapp"))
    assert len(kept) == len(FOLLOWUP_TOOL_NAMES)
