"""Tests for the hard-task classifier and advisor preflight.

Auto-routing to experts was dropped (see
``docs/superpowers/specs/2026-05-23-expert-mechanism-resurrection-design.md``);
``TestDeadHelpersRemoved`` guards against the deleted helpers
creeping back.  The classifier now drives only the hidden advisor
preflight.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from surogates.harness.budget import IterationBudget
from surogates.harness.loop import AgentHarness
from surogates.session.events import EventType
from surogates.session.models import Event, Session
from surogates.tools.registry import ToolRegistry


def _session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        agent_id="default",
        channel="web",
        status="active",
        model="default-model",
        config={"temperature": 0.7},
        created_at=now,
        updated_at=now,
    )


def _harness() -> AgentHarness:
    tenant = SimpleNamespace(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        service_account_id=None,
        org_config={},
        user_preferences={},
        asset_root="/tmp/test",
    )
    return AgentHarness(
        session_store=AsyncMock(),
        tool_registry=ToolRegistry(),
        llm_client=AsyncMock(),
        tenant=tenant,
        worker_id="worker",
        budget=IterationBudget(max_total=10),
        context_compressor=MagicMock(),
        prompt_builder=MagicMock(),
        advisor_client=AsyncMock(),
        advisor_model="advisor-model",
    )


class TestDeadHelpersRemoved:
    """Auto-router helpers must be gone — the design dropped auto-routing.

    See ``docs/superpowers/specs/2026-05-23-expert-mechanism-resurrection-design.md``.
    These assertions prevent the helpers from creeping back via copy-paste.
    """

    def test_select_expert_for_task_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "select_expert_for_task")

    def test_load_skills_for_expert_routing_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "load_skills_for_expert_routing")

    def test_classify_tool_calls_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "classify_tool_calls")

    def test_hard_task_classifier_removed(self):
        """The advisor gate is model-driven; nothing classifies turns."""
        from surogates.harness import expert_routing

        for name in (
            "classify_hard_task",
            "classify_hard_task_async",
            "HardTaskClassification",
            "is_advisor_guidance_message",
        ):
            assert not hasattr(expert_routing, name), name

    def test_trigger_helpers_removed(self):
        from surogates.harness import expert_routing

        assert not hasattr(expert_routing, "_normalise_trigger_text")
        assert not hasattr(expert_routing, "_trigger_match_score")
