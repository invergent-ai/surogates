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
    DISCLOSURE_TEXTS,
    TransparencyLevel,
)

logger = logging.getLogger(__name__)

# The platform floor: open policy (workspace-sandbox path containment,
# path-argument hygiene and AGT argument checks still run).  Shared
# across wakes and used as the tool-path fallback so the per-workspace
# ExecutionSandbox cache is actually effective — the gate documents its
# sandbox cache as thread-safe, and the floor carries no mutable policy
# state (``add_allowed``/``add_denied`` have no production callers).
_FLOOR_GATE = GovernanceGate()


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

    allowed = governance.get("allowed_tools")
    if isinstance(allowed, list):
        allowed_names = [t for t in allowed if isinstance(t, str) and t]
        if allowed_names:
            profile["allowed_tools"] = allowed_names
    elif allowed not in (None, []):
        logger.warning(
            "governance.allowed_tools has unexpected type %s; ignoring",
            type(allowed).__name__,
        )

    denied = governance.get("denied_tools")
    if isinstance(denied, list):
        denied_names = [t for t in denied if isinstance(t, str) and t]
        if denied_names:
            profile["denied_tools"] = denied_names
    elif denied not in (None, []):
        logger.warning(
            "governance.denied_tools has unexpected type %s; ignoring",
            type(denied).__name__,
        )

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


_LEVEL_VALUES = {level.value for level in TransparencyLevel}


def disclosure_text(level: str) -> str:
    """The disclosure copy for a level, empty for ``none``.

    Unknown levels degrade to the ``basic`` text (never to silence) for
    the same reason ``disclosure_config`` clamps upward.
    """
    normalized = str(level or "").strip().lower()
    if normalized == "none":
        return ""
    try:
        return DISCLOSURE_TEXTS[TransparencyLevel(normalized)]
    except (KeyError, ValueError):
        return DISCLOSURE_TEXTS[TransparencyLevel.BASIC]
