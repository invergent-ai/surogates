"""A declared ``policy_profile`` must not pass silently unenforced.

Four writers stamp ``session.config["policy_profile"]``
(``harness/agent_resolver.py``, ``tools/builtin/coordinator.py``,
``tools/builtin/delegate.py``, ``tasks/spawn.py``), the API accepts it, the
UI shows it, and the shipped ``build-sub-agent`` skill documents it as
narrowing the tenant base policy ("intersects allowed, unions denied;
profiles never widen") with ``policy_profile: read_only`` as its example.

Nothing resolves the name. The only gate construction is
``build_governance_gate(ctx.governance)`` from the per-agent runtime config;
``session.config`` never reaches it, and no registry maps ``read_only`` (or
any other name) to a profile. A sub-agent declared read-only runs with the
parent's authority.

Enforcing it means defining what each name permits — a security decision, not
a bug fix. Until that exists, the platform must at least say so out loud
instead of accepting the declaration and dropping it.
"""

from __future__ import annotations

import logging

from surogates.runtime.governance import warn_if_profile_unenforced


def test_declared_profile_is_reported(caplog):
    with caplog.at_level(logging.WARNING, logger="surogates.runtime.governance"):
        warn_if_profile_unenforced({"policy_profile": "read_only"}, agent_id="ag-1")
    assert any("read_only" in r.getMessage() for r in caplog.records), (
        "an operator must be told the declared restriction is not in force"
    )


def test_no_profile_is_silent(caplog):
    with caplog.at_level(logging.WARNING, logger="surogates.runtime.governance"):
        warn_if_profile_unenforced({}, agent_id="ag-1")
        warn_if_profile_unenforced(None, agent_id="ag-1")
        warn_if_profile_unenforced({"policy_profile": None}, agent_id="ag-1")
        warn_if_profile_unenforced({"policy_profile": ""}, agent_id="ag-1")
    assert caplog.records == []


def test_a_malformed_config_does_not_raise(caplog):
    """This runs on the wake path; it must never be the thing that fails."""
    with caplog.at_level(logging.WARNING, logger="surogates.runtime.governance"):
        warn_if_profile_unenforced({"policy_profile": 123}, agent_id="ag-1")
        warn_if_profile_unenforced("not-a-dict", agent_id="ag-1")  # type: ignore[arg-type]
