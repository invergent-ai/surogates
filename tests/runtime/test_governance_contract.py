"""Consumer half of the governance wire contract.

The producer half lives in the surogate-ops repo
(``tests/test_governance_end_to_end.py``) and asserts that a policy
saved in Studio projects to exactly the blob below. This file asserts
the same blob actually enforces at runtime, so the two repos cannot
drift apart without one of them failing.
"""

from __future__ import annotations

import pytest

from surogates.runtime.governance import (
    build_governance_gate,
    disclosure_config,
)
from surogates.tools.router import TOOL_LOCATIONS


# Mirrored from the producer test. Any shape change must land in both.
CANONICAL_BLOB = {
    "enabled": True,
    "allowed_tools": [],
    "denied_tools": ["terminal"],
    "egress": {
        "default_action": "deny",
        "rules": [{
            "domain": "api.example.com",
            "ports": [443],
            "protocol": "tcp",
            "action": "allow",
        }],
    },
    "transparency": {"enabled": True, "level": "full"},
}


def test_canonical_blob_denies_the_denied_tool():
    gate = build_governance_gate(CANONICAL_BLOB)
    assert not gate.check("terminal", {"command": "ls"}).allowed
    assert gate.check("read_file", {"path": "notes.md"}).allowed


def test_canonical_blob_enforces_its_egress_rules():
    gate = build_governance_gate(CANONICAL_BLOB)
    assert gate.check(
        "web_extract", {"url": "https://api.example.com/x"},
    ).allowed
    assert not gate.check(
        "web_extract", {"url": "https://elsewhere.example.org/x"},
    ).allowed


def test_canonical_blob_yields_a_disclosure_notice():
    cfg = disclosure_config(CANONICAL_BLOB)
    assert cfg["enabled"] is True
    assert cfg["level"] == "full"
    assert cfg["text"]


def test_allow_list_blob_blocks_everything_unlisted():
    gate = build_governance_gate({
        "enabled": True,
        "allowed_tools": ["read_file", "write_file"],
        "denied_tools": [],
    })
    assert gate.check("read_file", {"path": "a"}).allowed
    assert not gate.check("terminal", {"command": "ls"}).allowed
    assert not gate.check("generate_image", {"prompt": "x"}).allowed
    # MCP tools answer to server attachment + entitlements, not this list.
    assert gate.check("mcp__srv__tool", {}).allowed


def test_master_switch_off_drops_agent_rules_but_keeps_the_floor(tmp_path):
    """``enabled: false`` means "no agent rules", not "no governance"."""
    gate = build_governance_gate({
        "enabled": False, "denied_tools": ["terminal"],
    })
    assert gate.check("terminal", {"command": "ls"}).allowed
    assert not gate.check(
        "read_file", {"path": "/etc/passwd"}, workspace_path=str(tmp_path),
    ).allowed


@pytest.mark.parametrize("tool", sorted(TOOL_LOCATIONS))
def test_every_runtime_tool_can_actually_be_denied(tool: str):
    """Whatever Studio can name, the gate must be able to block.

    Studio's catalog is pinned to this same table on the ops side, so
    together the two tests mean: every offerable deny rule enforces.
    """
    gate = build_governance_gate({"enabled": True, "denied_tools": [tool]})
    assert not gate.check(tool, {}).allowed
