"""The Programs capability gates the follow-up tools end to end."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from surogates.db.models import ProgramScheduleRow
from surogates.followup import store as followup_store
from surogates.harness.tool_schemas import channel_tool_flags, drop_unusable_tools
from surogates.runtime.resolver import build_agent_runtime_context
from surogates.tools.builtin.followup import FOLLOWUP_TOOL_NAMES, followup_tool_exclusions

ORG = uuid.uuid4()
AGENT = "agent-1"
SESSION = SimpleNamespace(org_id=ORG, agent_id=AGENT)
NOT_ESCALATE = FOLLOWUP_TOOL_NAMES - {"escalate"}


async def _excluded(sf, programs_enabled: bool) -> frozenset[str]:
    return await followup_tool_exclusions(sf, SESSION, programs_enabled=programs_enabled)


@pytest.mark.asyncio
async def test_nothing_ships_when_nobody_could_receive_an_escalation(sf):
    assert await _excluded(sf, False) == FOLLOWUP_TOOL_NAMES
    assert await _excluded(sf, True) == frozenset({"escalate"})


@pytest.mark.asyncio
async def test_an_operator_inbox_offers_escalate_without_programs(sf):
    await followup_store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, escalation_service_account_id=str(uuid.uuid4()),
    )
    assert await _excluded(sf, False) == NOT_ESCALATE
    assert await _excluded(sf, True) == frozenset()


@pytest.mark.asyncio
async def test_a_team_only_reaches_anyone_with_programs_on(sf):
    await followup_store.update_settings(
        sf, org_id=ORG, agent_id=AGENT, alert_recipient_user_ids=[str(uuid.uuid4())],
    )
    assert await _excluded(sf, True) == frozenset()
    assert await _excluded(sf, False) == FOLLOWUP_TOOL_NAMES


@pytest.mark.asyncio
async def test_a_programs_own_inbox_counts_while_it_is_active(sf):
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT, active=False,
            config={"escalation_service_account_id": str(uuid.uuid4())},
        ))
        await db.commit()
    assert "escalate" in await _excluded(sf, True)
    async with sf() as db:
        db.add(ProgramScheduleRow(
            program_id=uuid.uuid4(), org_id=ORG, agent_id=AGENT, active=True,
            config={"escalation_service_account_id": str(uuid.uuid4())},
        ))
        await db.commit()
    assert await _excluded(sf, True) == frozenset()


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
