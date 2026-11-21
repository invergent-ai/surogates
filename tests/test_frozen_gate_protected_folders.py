"""A profiled (frozen) gate refuses the protected system folders as the platform's gate does (issue #702).

``with_profile`` returns a frozen gate whose argument checks run through ``_check_arguments_direct``, which had no
protected-folder rule: /etc/x, /etc/shadow and /proc/1/environ were refused by the platform's gate and allowed by any
agent with a governance profile (allow-list, deny-list or egress).
"""

from __future__ import annotations

import pytest

from surogates.governance.policy import GovernanceGate


@pytest.fixture
def gates():
    base = GovernanceGate()
    profiled = base.with_profile({"denied_tools": ["web_search"]})
    return base, profiled


@pytest.mark.parametrize("path", ["/etc/x", "/etc/shadow", "/proc/1/environ", "/sys/kernel/x", "/dev/sda",
                                  "/tmp/../etc/passwd"])
def test_a_profiled_gate_refuses_what_the_platforms_gate_refuses(gates, path):
    base, profiled = gates
    for gate in (base, profiled):
        for tool in ("read_file", "write_file"):
            decision = gate.check(tool, {"path": path, "content": "x"})
            assert not decision.allowed, (gate, tool, path)


@pytest.mark.parametrize("path", ["notes/today.md", "/workspace/report.md", "etc/x", "/tmp/etc/x"])
def test_ordinary_paths_stay_allowed_on_both(gates, path):
    base, profiled = gates
    for gate in (base, profiled):
        assert gate.check("read_file", {"path": path}).allowed, (gate, path)


def test_the_platforms_prefix_rule_is_mirrored_as_it_is(gates):
    # The AGT engine matches the folder as a prefix of the normalised path, so /etcetera/x is refused as /etc/; the
    # profiled gate gives the same answer rather than a different one.
    base, profiled = gates
    assert not base.check("read_file", {"path": "/etcetera/x"}).allowed
    assert not profiled.check("read_file", {"path": "/etcetera/x"}).allowed


def test_the_profiled_gate_is_frozen():
    profiled = GovernanceGate().with_profile({"denied_tools": ["web_search"]})
    assert profiled._engine.is_frozen
