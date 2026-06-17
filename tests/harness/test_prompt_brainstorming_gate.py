"""The brainstorming-gate guidance (force a design pass before creative
work) is injected with the skills guidance when ``skill_view`` is loaded —
unless the agent has ``brainstorming_gate`` turned off."""

from __future__ import annotations

from uuid import UUID

from surogates.harness.prompt import PromptBuilder, default_library
from surogates.tenant import TenantContext


def _tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )


class _Session:
    model = "gpt-4o"
    config: dict = {}


def _section(*, brainstorming_gate: bool) -> str:
    pb = PromptBuilder(
        _tenant(),
        available_tools={"skill_view"},
        session=_Session(),
        brainstorming_gate=brainstorming_gate,
    )
    return pb._tool_guidance_section()


def test_brainstorming_gate_present_by_default():
    frag = default_library().get("guidance/brainstorming_gate")
    assert frag in _section(brainstorming_gate=True)


def test_brainstorming_gate_hidden_when_disabled():
    frag = default_library().get("guidance/brainstorming_gate")
    out = _section(brainstorming_gate=False)
    assert frag not in out
    # the broader skills guidance still loads — only the gate is dropped.
    assert default_library().get("guidance/skills") in out
