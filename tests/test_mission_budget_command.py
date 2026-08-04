"""Setting a mission's token allowance without reaching for psql.

`budget_tokens` shipped with no way to set it: a column the store accepted
and nothing surfaced, so the only route was direct SQL. That is not a
feature anyone can use.

Two entry points, because a budget matters at both moments — you set one
when you know the objective is large, and you set one when a running
mission turns out to be burning more than you expected.
"""
from __future__ import annotations

import pytest

from surogates.missions.commands import (
    MissionCommandParseError,
    parse_auto_research_command,
    parse_mission_command,
)


# -- the value itself -------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("20000000", 20_000_000),
    ("20M", 20_000_000),
    ("20m", 20_000_000),
    ("1.5M", 1_500_000),
    ("500k", 500_000),
    ("500K", 500_000),
    ("2_000_000", 2_000_000),
])
def test_human_token_counts_are_accepted(text, expected):
    """Nobody should have to type 20000000 correctly under pressure."""
    cmd = parse_mission_command(f"ship it\nBudget: {text}\nRubric: works")
    assert cmd.budget_tokens == expected


@pytest.mark.parametrize("bad", ["lots", "-5", "0", "20MB", "", "M"])
def test_an_unusable_budget_is_rejected_not_ignored(bad):
    """Silently treating a typo as 'unbounded' is the fail-open this
    codebase has been removing all week."""
    with pytest.raises(MissionCommandParseError):
        parse_mission_command(f"ship it\nBudget: {bad}\nRubric: works")


# -- at creation ------------------------------------------------------------

def test_no_budget_block_means_unbounded():
    cmd = parse_mission_command("ship it\nRubric: works")
    assert cmd.budget_tokens is None


def test_the_budget_line_is_kept_out_of_the_description():
    cmd = parse_mission_command("ship it\nBudget: 5M\nRubric: works")
    assert cmd.description == "ship it"
    assert "Budget" not in (cmd.description or "")


def test_the_budget_line_is_kept_out_of_the_rubric():
    """It may sit after the rubric too; either position must parse the same."""
    cmd = parse_mission_command("ship it\nRubric: works\nBudget: 5M")
    assert cmd.budget_tokens == 5_000_000
    assert "Budget" not in (cmd.rubric or "")
    assert cmd.rubric == "works"


def test_a_description_mentioning_budget_prose_is_untouched():
    """Only a line that IS a Budget: directive counts, not prose about one."""
    cmd = parse_mission_command(
        "reconcile the marketing Budget: Q3 overspend\nRubric: reconciled",
    )
    assert cmd.budget_tokens is None
    assert "marketing Budget" in (cmd.description or "")


# -- on a running mission ---------------------------------------------------

def test_budget_is_a_control_verb():
    cmd = parse_mission_command("budget 20M")
    assert cmd.action == "budget"
    assert cmd.budget_tokens == 20_000_000


def test_the_budget_verb_rejects_a_bad_value():
    with pytest.raises(MissionCommandParseError):
        parse_mission_command("budget nonsense")


def test_the_budget_verb_requires_a_value():
    with pytest.raises(MissionCommandParseError):
        parse_mission_command("budget")


def test_budget_none_clears_the_ceiling():
    """An operator must be able to take a ceiling off again."""
    cmd = parse_mission_command("budget none")
    assert cmd.action == "budget"
    assert cmd.budget_tokens is None
    assert cmd.clear_budget is True


# -- on a research run ------------------------------------------------------

def test_a_research_run_keeps_the_budget_its_parse_found():
    """/auto-research delegates to the /mission parser and then rebuilds the
    command; a field dropped in that rebuild is a ceiling the operator typed
    and never got."""
    cmd = parse_auto_research_command(
        "repo=/workspace/r Improve F1\nBudget: 3M\n\nRubric:\n- improves",
    )
    assert cmd.budget_tokens == 3_000_000


def test_a_bad_research_budget_says_so():
    """/auto-research reframes parse errors into 'you need a repo and a
    Rubric'; on a control verb that hides the only useful part."""
    with pytest.raises(MissionCommandParseError, match="unusable budget"):
        parse_auto_research_command("budget 20MB")


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
