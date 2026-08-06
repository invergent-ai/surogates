"""Per-agent governance profile: runtime-config projection -> gate.

The management plane projects ``Agent.policy`` into the runtime-config
``governance`` blob (``AgentRuntimeContext.governance``).  This module
turns that raw dict into the frozen per-wake :class:`GovernanceGate`
the tool-execution path enforces, and exposes the AI-disclosure view
the API and channel adapters serve.

Normalization mirrors the ops-side projection
(``agents_shared.governance_blob_from_policy``): entries that are not
the documented shape are dropped rather than raising, because the
worker must keep serving sessions even when a legacy or hand-edited
blob arrives.  Dropping an entry never *widens* the policy — a
malformed ``allowed_tools`` falls back to "no allow-list" while a
malformed ``denied_tools`` entry is simply not matched, and the
platform floor (workspace sandbox + argument hygiene) applies
regardless of profile content.
"""

from __future__ import annotations

import logging
from typing import Any

from surogates.governance.policy import GovernanceGate
from surogates.governance.transparency import (
    TransparencyLevel,
    disclosure_text_for,
)

logger = logging.getLogger(__name__)

# The platform floor: open policy — workspace-sandbox path containment
# and path-argument hygiene still run.  (The AGT engine's dangerous-code
# screening keys off tool names this harness does not use, so treat the
# floor as path containment only, not command screening.)  Shared
# across wakes and used as the tool-path fallback so the per-workspace
# ExecutionSandbox cache is actually effective — the gate documents its
# sandbox cache as thread-safe, and the floor carries no mutable policy
# state (``add_allowed``/``add_denied`` have no production callers).
_FLOOR_GATE = GovernanceGate()

# Execution-context self-tools: how a session reports its own state and
# how a stuck one gets unstuck, as opposed to work tools.
#
# These are the single declaration for both layers that care.
# ``worker._filter_effective_tools`` strips them from sessions that
# cannot use them and force-adds them for sessions that can, even under
# a restrictive AgentDef allowlist; the governance gate refuses to let
# an agent policy deny them. Keeping one set means adding a fourth
# group cannot leave the two layers disagreeing.
WORKER_SELF_TOOLS: frozenset[str] = frozenset({
    "worker_block", "worker_complete", "worker_context",
})
BOARD_SELF_TOOLS: frozenset[str] = frozenset({
    "share_note", "read_board", "expand_note",
})
# The coordinator's only levers to resume or abandon a blocked task: a
# child that called ``worker_block`` stays blocked forever without them,
# so a policy that denied them would strand work with a single
# ``policy.denied`` event as the trace.
TASK_LIFECYCLE_TOOLS: frozenset[str] = frozenset({
    "unblock_task", "cancel_task",
})
# The research coordinator's deterministic spine. Declared here for the
# same reason as the sets above -- both the prompt surface
# (``worker._filter_effective_tools``) and the model-visible schemas
# (``AgentHarness._apply_execution_context_gates``) gate on it, and they
# must not drift. Deliberately NOT in ``PROTECTED_TOOLS``: unlike the
# self-tools these are never force-added, they ride the registry's full
# set on the coordinator and are stripped everywhere else so executors
# stay tree-blind.
RESEARCH_SPINE_TOOLS: frozenset[str] = frozenset({
    "idea_tree", "dispatch_experiments", "merge_experiment",
})

# Tools no agent policy may take away. The ops catalog omits these names
# so Studio cannot offer them and the policy validator rejects them with
# a clear 422; this set is the runtime's own belt-and-braces for
# hand-written or legacy blobs.
PROTECTED_TOOLS: frozenset[str] = (
    WORKER_SELF_TOOLS | BOARD_SELF_TOOLS | TASK_LIFECYCLE_TOOLS
)

# The harness disclosure-level vocabulary, straight off the enum.
_LEVEL_VALUES = {level.value for level in TransparencyLevel}


def floor_gate() -> GovernanceGate:
    """The shared platform-floor gate (no agent-configured rules)."""
    return _FLOOR_GATE


def governance_profile(governance: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize the runtime-config governance blob into a gate profile.

    Returns the ``GovernanceGate.with_profile`` input shape
    (``allowed_tools`` / ``denied_tools`` / ``egress``), or ``None``
    when the agent has no enforceable profile: no blob, an explicitly
    disabled policy (``enabled: false`` means the operator switched the
    agent-configured rules off), or a blob with no restrictions in it.
    """
    if not isinstance(governance, dict) or not governance:
        return None
    if not governance.get("enabled", True):
        return None

    profile: dict[str, Any] = {}

    allowed = _tool_names(governance, "allowed_tools")
    if allowed:
        # An allow-list must still admit the self-tools, or a task worker
        # cannot report its own state.
        profile["allowed_tools"] = sorted(allowed | PROTECTED_TOOLS)

    denied = _tool_names(governance, "denied_tools")
    protected = denied & PROTECTED_TOOLS
    if protected:
        logger.warning(
            "governance.denied_tools names protected self-tools %s; "
            "ignoring them (denying these strands blocked work)",
            sorted(protected),
        )
    if denied - PROTECTED_TOOLS:
        profile["denied_tools"] = sorted(denied - PROTECTED_TOOLS)

    approval = _tool_names(governance, "require_approval")
    if approval:
        profile["require_approval"] = sorted(approval)

    egress = governance.get("egress")
    if isinstance(egress, dict):
        rules = [r for r in (egress.get("rules") or []) if isinstance(r, dict)]
        default_action = egress.get("default_action")
        # Restriction detection only — ``with_profile`` owns the actual
        # egress semantics (including materialising a policy for a bare
        # deny default); this mirrors its activation condition so a
        # restriction-free blob composes no gate at all.
        if rules or default_action == "deny":
            profile["egress"] = {
                "default_action": (
                    default_action if default_action in ("allow", "deny")
                    else "deny"
                ),
                "rules": rules,
            }

    return profile or None


def _tool_names(governance: dict[str, Any], key: str) -> set[str]:
    """Non-empty string tool names under *key*, or an empty set.

    A malformed value never widens the policy: an unusable
    ``allowed_tools`` becomes "no allow-list" and an unusable
    ``denied_tools`` simply matches nothing.
    """
    value = governance.get(key)
    if isinstance(value, list):
        # Strip: a padded entry that silently matches nothing fails OPEN for
        # ``denied_tools`` and ``require_approval``, which is the wrong
        # direction for both.
        return {
            t.strip() for t in value if isinstance(t, str) and t.strip()
        }
    if value not in (None, []):
        logger.warning(
            "governance.%s has unexpected type %s; ignoring",
            key, type(value).__name__,
        )
    return set()


def build_governance_gate(
    governance: dict[str, Any] | None,
) -> GovernanceGate:
    """Build the per-wake gate from an agent's governance blob.

    The base gate is the shared platform floor.  When the agent carries
    an enforceable profile the floor is narrowed via ``with_profile``
    (allow-lists intersect, deny-lists union, strictest egress default)
    and the result is frozen for the wake; no agent configuration can
    relax the floor.  Agents without a profile share the floor gate
    directly — cheap, and it keeps the workspace sandbox cache warm
    across wakes.
    """
    profile = governance_profile(governance)
    if profile is None:
        return _FLOOR_GATE
    return _FLOOR_GATE.with_profile(profile)


def disclosure_config(
    governance: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The agent's explicit AI-disclosure config, or ``None``.

    ``None`` means "nothing explicitly configured" — callers fall back
    to the deployment-wide setting.  That covers a missing/malformed
    transparency block AND a policy whose master switch is off
    (``enabled: false``): Studio presents one master toggle over the
    whole governance section, so a switched-off policy must not keep
    disclosing behind the operator's back — it falls back to the
    deployment default like an unconfigured agent.

    Otherwise returns ``{"enabled", "level", "text"}`` with the level
    clamped to the harness vocabulary.  Unknown levels degrade to
    ``basic`` (disclosure still shown) rather than ``none`` so a
    vocabulary drift upstream can never silently switch disclosure off.
    An explicitly disabled block returns
    ``{"enabled": False, "level": "none", "text": ""}`` — the agent
    config beats the deployment fallback in both directions.
    """
    if not isinstance(governance, dict):
        return None
    if not governance.get("enabled", True):
        return None
    raw = governance.get("transparency")
    if not isinstance(raw, dict):
        return None
    if not raw.get("enabled", False):
        return {"enabled": False, "level": "none", "text": ""}
    level = str(raw.get("level", "basic")).strip().lower()
    return {
        "enabled": True,
        "level": level if level in _LEVEL_VALUES else "basic",
        "text": disclosure_text(level),
    }


def disclosure_text(level: str) -> str:
    """The disclosure copy for a level string, empty for ``none``.

    Adds string normalisation and the ``none`` case on top of
    :func:`~surogates.governance.transparency.disclosure_text_for`;
    unknown levels degrade to the ``basic`` text (never to silence) for
    the same reason ``disclosure_config`` clamps upward.
    """
    normalized = str(level or "").strip().lower()
    if normalized == "none":
        return ""
    try:
        return disclosure_text_for(TransparencyLevel(normalized))
    except ValueError:
        return disclosure_text_for(TransparencyLevel.BASIC)
