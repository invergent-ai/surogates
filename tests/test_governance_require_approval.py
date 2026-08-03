"""Tools that need a human's say-so before they run.

`PolicyDecision.overridable` existed but was never set `True`, so the whole
"ask a human, then proceed" path was dead. This is the trigger that makes it
reachable.

Placement is the load-bearing detail. `check()` returns early for
`self._open_policy or tool_name.startswith("mcp__")`, and open policy is the
production default -- so a check placed after that line is as unreachable as
`overridable` was. MCP/Composio tools are the highest-risk external-side-effect
surface and take that early return unconditionally.
"""

from __future__ import annotations

import pytest

from surogates.governance.policy import GovernanceGate


def test_a_tool_needing_approval_is_denied_overridably():
    gate = GovernanceGate(require_approval={"terminal"})
    decision = gate.check("terminal", {"command": "rm -rf /"})

    assert decision.allowed is False
    assert decision.overridable is True, (
        "a denial nobody can override is a refusal, not an approval gate"
    )
    assert "approval" in decision.reason.lower()


def test_other_tools_are_unaffected():
    gate = GovernanceGate(require_approval={"terminal"})
    assert gate.check("file_read", {"path": "a.txt"}).allowed is True


def test_an_mcp_tool_under_open_policy_still_hits_the_gate():
    """The regression that matters.

    Open policy is the production default and `mcp__*` names bypass the role
    check unconditionally, so an approval rule placed below that early return
    would never fire for exactly the tools most worth gating.
    """
    gate = GovernanceGate(require_approval={"mcp__tool_router__send_email"})
    decision = gate.check("mcp__tool_router__send_email", {"to": "a@b.c"})

    assert decision.allowed is False
    assert decision.overridable is True


def test_an_ungated_mcp_tool_still_passes():
    gate = GovernanceGate(require_approval={"mcp__other"})
    assert gate.check("mcp__tool_router__search", {}).allowed is True


def test_no_approval_list_changes_nothing():
    gate = GovernanceGate()
    assert gate.check("terminal", {"command": "ls"}).allowed is True


def test_a_denied_tool_beats_an_approval_rule():
    """An explicit denial is not negotiable -- approval must not soften it."""
    gate = GovernanceGate(
        denied_tools={"terminal"}, require_approval={"terminal"},
    )
    decision = gate.check("terminal", {"command": "ls"})

    assert decision.allowed is False
    assert decision.overridable is False, (
        "an explicitly denied tool must not become approvable"
    )


def test_with_profile_unions_the_approval_list():
    """Profiles narrow, never widen: more tools needing approval is stricter."""
    base = GovernanceGate(require_approval={"terminal"})
    composed = base.with_profile({"require_approval": ["write_file"]})

    assert composed.check("terminal", {}).overridable is True
    assert composed.check("write_file", {"path": "a"}).overridable is True


def test_with_profile_cannot_drop_a_base_approval_rule():
    base = GovernanceGate(require_approval={"terminal"})
    composed = base.with_profile({"allowed_tools": ["terminal"]})

    assert composed.check("terminal", {}).allowed is False


@pytest.mark.parametrize("blob,expected", [
    ({"require_approval": ["terminal"]}, {"terminal"}),
    ({"require_approval": []}, None),
    ({}, None),
    ({"require_approval": "terminal"}, None),
    ({"require_approval": [1, None, "  terminal  "]}, {"terminal"}),
])
def test_the_profile_builder_reads_the_list(blob, expected):
    from surogates.runtime.governance import governance_profile

    profile = governance_profile(blob) or {}
    got = profile.get("require_approval")
    assert (set(got) if got else None) == expected
