"""The ambient prompt hears of the agents its source session started that ended."""

from types import SimpleNamespace

from surogates.ambient.agents_probe import summarize_agents
from surogates.ambient.prompt import build_ambient_prompt


def _row(status: str, description: str | None) -> SimpleNamespace:
    dispatch = {"mode": "background", **({"description": description} if description else {})}
    return SimpleNamespace(status=status, config={"agent_dispatch": dispatch})


def test_each_ended_agent_reads_as_one_line():
    rows = [_row("completed", "Count rows"), _row("failed", "Check totals"), _row("paused", None)]
    assert summarize_agents(rows) == [
        "Agent 'Count rows' finished", "Agent 'Check totals' failed", "Agent 'agent' was stopped",
    ]


def test_the_prompt_lists_them():
    prompt = build_ambient_prompt(channel_label="#c", agent_changes=["Agent 'Count rows' finished"])
    assert "Agents you started that ended recently:\n- Agent 'Count rows' finished" in prompt
    assert "Agents you started" not in build_ambient_prompt(channel_label="#c", agent_changes=[])
