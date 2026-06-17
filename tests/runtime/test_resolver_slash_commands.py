"""Tests for the ``slash_commands`` projection in
``surogates.runtime.resolver.build_agent_runtime_context``.

The management plane sends a grouped ``slash_commands`` object on the
runtime-config payload (``{enabled, commands}`` with snake_case command
keys); the resolver turns it into the frozen ``SlashCommandConfig`` the
harness consults.  Absence must default to fully permissive so older
payloads keep every command.
"""

from __future__ import annotations

from surogates.runtime import SlashCommandConfig
from surogates.runtime.resolver import build_agent_runtime_context


def _payload(**extra):
    base = {
        "agent_id": "a-1",
        "org_id": "o-1",
        "project_id": "p-1",
        "enabled": True,
        "version": 1,
        "storage_key_prefix": "p-1/a-1",
    }
    base.update(extra)
    return base


def test_absent_slash_commands_defaults_permissive():
    ctx = build_agent_runtime_context(_payload())
    assert ctx.slash_commands == SlashCommandConfig()


def test_all_commands_off_keeps_only_clear():
    ctx = build_agent_runtime_context(
        _payload(slash_commands={"commands": {}})
    )
    # Every flagged command is off, but ``clear`` has no flag and is
    # always present.
    assert ctx.slash_commands.commands == frozenset({"clear"})


def test_wire_keys_map_to_hyphenated_ids():
    ctx = build_agent_runtime_context(
        _payload(
            slash_commands={
                "commands": {
                    "deep_research": True,
                    "auto_research": False,
                    "loop": True,
                    "compress": False,
                },
            }
        )
    )
    cmds = ctx.slash_commands.commands
    assert "deep-research" in cmds
    assert "auto-research" not in cmds
    assert "loop" in cmds
    assert "compress" not in cmds
    assert "clear" in cmds
