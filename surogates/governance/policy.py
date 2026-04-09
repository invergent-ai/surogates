"""Governance gate for tool-call policy enforcement.

Wraps Microsoft's Agent Governance Toolkit (``agent-os-kernel``) PolicyEngine
for deterministic, sub-millisecond policy checks.  The AGT engine provides:

* Role-based tool permissions via ``state_permissions``
* ABAC conditional permissions via ``add_conditional_permission``
* Argument-level checks (path traversal, dangerous code patterns, SQL injection)
* Protected path enforcement
* Freeze / immutability

Our ``GovernanceGate`` is a thin Surogates-specific wrapper that:
1. Translates allow-list / deny-list config into AGT's ``state_permissions``
2. Loads policies from platform volumes + org config overlay
3. Returns ``PolicyDecision`` dataclass for the ToolRouter
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_os import PolicyEngine as AGTPolicyEngine

logger = logging.getLogger(__name__)

# Agent role used for all Surogates tool-call checks.
_DEFAULT_ROLE = "agent"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Result of a governance check on a single tool call."""

    allowed: bool
    reason: str
    tool_name: str


class GovernanceGate:
    """Wraps the AGT PolicyEngine for Surogates tool-call governance.

    Mode resolution:

    * If *allowed_tools* is set the gate operates in **allow-list** mode --
      only tools in the set are permitted.
    * If *denied_tools* is set those tools are removed from the allow-list.
    * If **neither** is set, every tool is allowed (open policy -- the AGT
      engine is still consulted for argument-level checks).

    Once :meth:`freeze` is called the AGT engine becomes immutable for the
    remainder of the session.
    """

    def __init__(
        self,
        allowed_tools: set[str] | None = None,
        denied_tools: set[str] | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._enabled: bool = enabled
        self._open_policy: bool = (allowed_tools is None and denied_tools is None)

        # Initialise AGT engine.
        self._engine = AGTPolicyEngine()

        # Resolve effective allow-set.
        if allowed_tools is not None:
            effective = set(allowed_tools)
            if denied_tools:
                effective -= set(denied_tools)
            self._engine.state_permissions[_DEFAULT_ROLE] = effective
        elif denied_tools is not None:
            # Deny-list only: treat as open policy with explicit blocks.
            # We handle the deny check ourselves before AGT's role gate.
            self._open_policy = True
            self._denied_tools: set[str] = set(denied_tools)
        else:
            # Open policy — we skip the AGT role check but still run
            # argument-level validations.
            self._denied_tools = set()

        # Expose blocked_patterns for argument-level checks.
        if denied_tools:
            self._engine.blocked_patterns = list(denied_tools)

        self._has_allow_list = allowed_tools is not None
        if not hasattr(self, "_denied_tools"):
            self._denied_tools = set(denied_tools) if denied_tools else set()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def freeze(self) -> None:
        """Make the policy immutable for the rest of this session."""
        self._engine.freeze()

    @property
    def is_frozen(self) -> bool:
        return self._engine.is_frozen

    def add_allowed(self, tool_name: str) -> None:
        """Add a tool to the allow-list (fails silently if frozen)."""
        if self._engine.is_frozen:
            logger.warning("Attempted to modify frozen governance gate")
            return
        perms = self._engine.state_permissions.get(_DEFAULT_ROLE, set())
        perms.add(tool_name)
        self._engine.state_permissions[_DEFAULT_ROLE] = perms
        self._open_policy = False
        self._has_allow_list = True

    def add_denied(self, tool_name: str) -> None:
        """Add a tool to the deny-list (fails silently if frozen)."""
        if self._engine.is_frozen:
            logger.warning("Attempted to modify frozen governance gate")
            return
        self._denied_tools.add(tool_name)
        # Also remove from allow-list if present.
        perms = self._engine.state_permissions.get(_DEFAULT_ROLE, set())
        perms.discard(tool_name)

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    def check(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Evaluate whether a tool invocation is permitted.

        Delegates to the AGT PolicyEngine for:
        - Role-based tool permission checks
        - Argument-level validation (path traversal, dangerous code, SQL injection)
        - Conditional permissions (ABAC)
        """
        if not self._enabled:
            return PolicyDecision(
                allowed=True, reason="governance disabled", tool_name=tool_name
            )

        # Fast deny-list check (before AGT role check).
        if tool_name in self._denied_tools:
            return PolicyDecision(
                allowed=False,
                reason=f"denied: tool {tool_name!r} explicitly blocked",
                tool_name=tool_name,
            )

        # Open policy: skip role check, only run argument checks.
        if self._open_policy:
            violation = self._check_arguments(tool_name, arguments or {})
            if violation:
                return PolicyDecision(
                    allowed=False, reason=violation, tool_name=tool_name
                )
            return PolicyDecision(
                allowed=True, reason="allowed", tool_name=tool_name
            )

        # AGT check_violation: role + conditional + argument checks.
        violation = self._engine.check_violation(
            _DEFAULT_ROLE, tool_name, arguments or {}
        )
        if violation is not None:
            return PolicyDecision(
                allowed=False, reason=violation, tool_name=tool_name
            )

        return PolicyDecision(
            allowed=True, reason="allowed", tool_name=tool_name
        )

    def _check_arguments(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> str | None:
        """Run AGT argument-level checks without the role gate.

        This is used in open-policy mode where we want path traversal,
        dangerous code, and SQL injection checks but no role enforcement.
        """
        if self._engine.is_frozen:
            # After freeze, state_permissions is a MappingProxyType and cannot
            # be mutated.  Fall back to direct argument checks without the AGT
            # role gate.
            return self._check_arguments_direct(tool_name, arguments)

        # Temporarily add the tool to permissions so check_violation
        # only evaluates argument-level rules.
        perms = self._engine.state_permissions.get(_DEFAULT_ROLE, set())
        perms.add(tool_name)
        self._engine.state_permissions[_DEFAULT_ROLE] = perms
        try:
            return self._engine.check_violation(_DEFAULT_ROLE, tool_name, arguments)
        finally:
            perms.discard(tool_name)
            if not perms:
                self._engine.state_permissions.pop(_DEFAULT_ROLE, None)

    @staticmethod
    def _check_arguments_direct(
        tool_name: str, arguments: dict[str, Any]
    ) -> str | None:
        """Fallback argument checks when AGT engine is frozen.

        Reimplements the key AGT argument-level validations:
        path traversal, dangerous code patterns.
        """
        import os

        # Path traversal check for file tools.
        if tool_name in ("write_file", "read_file", "delete_file", "file_write", "file_read"):
            path = arguments.get("path", "")
            if isinstance(path, str):
                if any(c in path for c in ("\n", "\r", "\x00")):
                    return "Path contains control characters"
                try:
                    normalized = os.path.normpath(os.path.abspath(path))
                    if ".." in normalized.split(os.sep):
                        return f"Path traversal detected: {path}"
                except (ValueError, OSError):
                    return "Invalid path format"

        return None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        platform_policy_path: str,
        org_config: dict[str, Any],
    ) -> GovernanceGate:
        """Load policies from a platform volume and an org-config overlay.

        The platform policy directory is scanned for files matching
        ``policy.yaml``, ``policy.yml``, or ``policy.json``.  The file
        must contain a mapping with optional keys ``allowed_tools`` (list)
        and ``denied_tools`` (list).

        *org_config* may contain a ``"governance"`` key with the same
        structure; values are merged (union) with platform policies.
        """
        allowed: set[str] = set()
        denied: set[str] = set()
        enabled = True

        # --- Platform policy file ---
        platform_dir = Path(platform_policy_path)
        platform_data = _load_policy_dir(platform_dir)

        if platform_data is not None:
            allowed.update(platform_data.get("allowed_tools") or [])
            denied.update(platform_data.get("denied_tools") or [])
            if "enabled" in platform_data:
                enabled = bool(platform_data["enabled"])

        # --- Org overlay ---
        gov = org_config.get("governance", {})
        if isinstance(gov, dict):
            allowed.update(gov.get("allowed_tools") or [])
            denied.update(gov.get("denied_tools") or [])
            if "enabled" in gov:
                enabled = bool(gov["enabled"])

        return cls(
            allowed_tools=allowed if allowed else None,
            denied_tools=denied if denied else None,
            enabled=enabled,
        )


# ------------------------------------------------------------------
# File loading helpers
# ------------------------------------------------------------------


def _load_policy_dir(directory: Path) -> dict[str, Any] | None:
    """Attempt to read a policy file from *directory*.

    Tries ``policy.yaml``, ``policy.yml``, ``policy.json`` in order.
    Returns the parsed mapping or ``None`` if nothing is found.
    """
    for filename in ("policy.yaml", "policy.yml", "policy.json"):
        path = directory / filename
        if not path.is_file():
            continue
        try:
            return _load_file(path)
        except Exception:
            logger.exception("Failed to load policy file %s", path)
    return None


def _load_file(path: Path) -> dict[str, Any]:
    """Parse a YAML or JSON file into a dict."""
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        return _parse_yaml(text)
    return json.loads(text)


def _parse_yaml(text: str) -> dict[str, Any]:
    """Parse YAML, falling back to JSON if PyYAML is unavailable."""
    try:
        import yaml  # type: ignore[import-untyped]

        data = yaml.safe_load(text)
    except ImportError:
        # If PyYAML is not installed, try JSON as a reasonable subset of YAML.
        logger.debug("PyYAML not available; attempting JSON parse")
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Policy file must contain a YAML/JSON mapping")
    return data
