"""Per-agent governance profile: runtime-config projection -> gate.

The management plane projects ``Agent.policy`` into the runtime-config
``governance`` blob (``AgentRuntimeContext.governance``).  This module
turns that raw dict into the frozen per-wake :class:`GovernanceGate`
the tool-execution path enforces, and exposes the transparency
(AI-disclosure) view the API and channel adapters serve.

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

logger = logging.getLogger(__name__)

# The harness disclosure-level vocabulary.  Mirrors
# :class:`surogates.governance.transparency.TransparencyLevel`; kept as
# a plain set here so profile parsing does not import the interceptor
# machinery.
_VALID_LEVELS = {"none", "basic", "enhanced", "full"}


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

    The base gate is the platform floor — open policy, which still
    enforces workspace-sandbox path containment, path-argument hygiene
    and AGT argument checks.  When the agent carries an enforceable
    profile the floor is narrowed via ``with_profile`` (allow-lists
    intersect, deny-lists union, strictest egress default) and the
    result is frozen for the wake; no agent configuration can relax
    the floor.
    """
    from surogates.tools.router import TOOL_LOCATIONS

    floor = GovernanceGate(
        allow_list_scope=frozenset(TOOL_LOCATIONS),
    )
    profile = governance_profile(governance)
    if profile is None:
        return floor
    return floor.with_profile(profile)


def transparency_config(
    governance: dict[str, Any] | None,
) -> dict[str, Any]:
    """Normalize the per-agent AI-disclosure block.

    Returns ``{"enabled": bool, "level": str}`` with the level clamped
    to the harness vocabulary.  Unknown levels degrade to ``basic``
    (disclosure still shown) rather than ``none`` so a vocabulary drift
    upstream can never silently switch disclosure off.
    """
    raw = (
        governance.get("transparency")
        if isinstance(governance, dict) else None
    )
    if not isinstance(raw, dict):
        return {"enabled": False, "level": "none"}
    enabled = bool(raw.get("enabled", False))
    level = str(raw.get("level", "basic")).strip().lower()
    if level not in _VALID_LEVELS:
        level = "basic"
    if not enabled:
        return {"enabled": False, "level": "none"}
    return {"enabled": True, "level": level}


def has_transparency_config(governance: dict[str, Any] | None) -> bool:
    """True when the agent carries an explicit transparency block.

    Distinguishes "the operator configured disclosure (possibly off)"
    from "nothing configured" — the latter falls back to the
    deployment-wide default, the former always wins.
    """
    return isinstance(governance, dict) and isinstance(
        governance.get("transparency"), dict,
    )


def disclosure_text(level: str) -> str:
    """The disclosure copy for a level, empty for ``none``/unknown-off.

    Unknown levels degrade to the ``basic`` text (never to silence) for
    the same reason ``transparency_config`` clamps upward.
    """
    from surogates.governance.transparency import (
        DISCLOSURE_TEXTS,
        TransparencyLevel,
    )

    normalized = str(level or "").strip().lower()
    if normalized == "none":
        return ""
    try:
        return DISCLOSURE_TEXTS[TransparencyLevel(normalized)]
    except (KeyError, ValueError):
        return DISCLOSURE_TEXTS[TransparencyLevel.BASIC]
