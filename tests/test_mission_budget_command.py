"""Research mission creation persists the requested token budget."""
from __future__ import annotations


from surogates.missions.commands import (
    parse_auto_research_command,
)


async def test_a_research_budget_reaches_the_mission_row(monkeypatch):
    """A research mission IS a mission, so its ceiling belongs on the same
    column — which only happens if the research create path forwards it."""
    from uuid import uuid4

    from surogates.missions import commands

    captured: dict = {}

    async def spy(**kwargs):
        captured.update(kwargs)
        # Bail before the research sidecar so this stays DB-free.
        return commands.MissionHandlerResult(ok=False, error="stop")

    monkeypatch.setattr(commands, "handle_mission_create", spy)

    await commands.handle_research_mission_create(
        cmd=parse_auto_research_command(
            "repo=/workspace/r Improve F1\nBudget: 3M\n\nRubric:\n- improves",
        ),
        session_id=uuid4(), org_id=uuid4(), agent_id="agent-a",
        session_store=None, session_factory=None, mission_store=None,
        user_id=uuid4(),
    )
    assert captured["budget_tokens"] == 3_000_000
