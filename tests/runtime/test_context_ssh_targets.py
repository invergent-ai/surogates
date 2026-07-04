"""ssh_targets projection onto AgentRuntimeContext."""

from __future__ import annotations

from surogates.runtime.resolver import build_agent_runtime_context

_BASE = {
    "agent_id": "a1",
    "org_id": "o1",
    "project_id": "o1",
    "enabled": True,
    "version": 1,
    "storage_key_prefix": "o1/a1",
}


def test_ssh_targets_projected():
    payload = {
        **_BASE,
        "ssh_targets": [
            {"alias": "deploy", "host": "h", "port": 22, "key_name": "k"},
        ],
    }
    ctx = build_agent_runtime_context(payload)
    assert ctx.ssh_targets == (
        {"alias": "deploy", "host": "h", "port": 22, "key_name": "k"},
    )


def test_ssh_targets_default_empty():
    assert build_agent_runtime_context(dict(_BASE)).ssh_targets == ()


def test_ssh_targets_are_copied():
    src = [{"alias": "deploy", "host": "h", "port": 22, "key_name": "k"}]
    ctx = build_agent_runtime_context({**_BASE, "ssh_targets": src})
    src[0]["host"] = "mutated"
    assert ctx.ssh_targets[0]["host"] == "h"
