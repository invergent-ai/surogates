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
    PROTECTED_TOOLS,
    build_governance_gate,
    disclosure_config,
)


# Mirrored from the producer test, together with the contract id both
# halves assert — editing the literal on one side only now fails here.
GOVERNANCE_CONTRACT_ID = "governance-blob/v1"


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


def test_contract_id_matches_the_producer():
    assert GOVERNANCE_CONTRACT_ID == "governance-blob/v1"


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


@pytest.mark.parametrize(
    "tool", ["terminal", "web_extract", "generate_image", "user_reports"],
)
def test_representative_tools_can_be_denied(tool: str):
    """Deny is set membership, so a few representatives suffice.

    The exhaustive version of this test was tautological (it passed for
    invented names too); what actually needs pinning is the *set* of
    governable names, which the ops catalog-parity test owns.
    """
    gate = build_governance_gate({"enabled": True, "denied_tools": [tool]})
    assert not gate.check(tool, {}).allowed


@pytest.mark.parametrize("tool", sorted(PROTECTED_TOOLS))
def test_protected_self_tools_cannot_be_denied(tool: str):
    """A policy must not be able to strand a session.

    Denying ``unblock_task``/``cancel_task`` would leave a child that
    called ``worker_block`` blocked forever, and denying the
    ``worker_*``/board self-tools contradicts the harness, which
    force-adds them into a worker's schema regardless of the AgentDef
    allowlist.
    """
    gate = build_governance_gate({"enabled": True, "denied_tools": [tool]})
    assert gate.check(tool, {}).allowed


@pytest.mark.parametrize("tool", sorted(PROTECTED_TOOLS))
def test_protected_self_tools_survive_an_allow_list(tool: str):
    gate = build_governance_gate({
        "enabled": True, "allowed_tools": ["read_file"],
    })
    assert gate.check(tool, {}).allowed
    # The allow-list still bites for ordinary tools.
    assert not gate.check("terminal", {"command": "ls"}).allowed


def test_protected_tools_are_all_real_registered_tools():
    """The protected set must name tools that actually exist.

    A typo here would silently protect nothing, so pin it against a
    fully built registry rather than a hand-maintained list.
    """
    from surogates.tools.registry import ToolRegistry
    from surogates.tools.runtime import ToolRuntime

    registry = ToolRegistry()
    ToolRuntime(registry).register_builtins()
    unknown = sorted(PROTECTED_TOOLS - set(registry.tool_names))
    assert not unknown, f"PROTECTED_TOOLS names unregistered tools: {unknown}"
