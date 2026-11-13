"""A coordinator works the way Claude Code's coordinator mode does: it sequences its agents and reads their reports."""

from __future__ import annotations

from surogates.harness.prompt_library import default_library


def test_a_coordinator_is_told_to_sequence_its_agents():
    text = default_library().get("guidance/coordinator").lower()
    for rule in (
        "in parallel",          # independent read-only work runs side by side
        "one agent at a time",  # one writer per set of files
        "fresh agent",          # verification by a fresh agent
        "based on your findings",  # never written in a brief
        "notification",         # waits for the reports it needs
        "send_message",         # continues an agent to fix its own failure
    ):
        assert rule in text, rule
    for old in ("spawn_worker", "send_worker_message", "stop_worker", "worker"):
        assert old not in text, old
