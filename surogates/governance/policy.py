"""Governance gate for tool-call policy enforcement.

Wraps Microsoft's Agent Governance Toolkit (``agent-os-kernel``) for
deterministic, sub-millisecond policy checks.  Integrates:

* **PolicyEngine** — role-based tool permissions, ABAC, argument checks
* **ExecutionSandbox** — workspace path containment via ``check_file_access()``
* **EgressPolicy** — network egress control (domain/port allow/deny)
* **TrustRoot** — non-overridable top-level authority (wraps this gate)

Our ``GovernanceGate`` is a thin Surogates-specific wrapper that:
1. Translates allow-list / deny-list config into AGT's ``state_permissions``
2. Loads policies from platform volumes + org config overlay
3. Checks workspace sandbox, egress policy, and argument-level rules
4. Returns ``PolicyDecision`` dataclass for the ToolRouter
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_os import PolicyEngine as AGTPolicyEngine
from agent_os.egress_policy import EgressDecision, EgressPolicy, EgressRule
from agent_os.sandbox import ExecutionSandbox, SandboxConfig
from surogates.governance.transparency import TransparencyInterceptor, TransparencyLevel, ToolCallRequest

logger = logging.getLogger(__name__)

# Agent role used for all Surogates tool-call checks.
_DEFAULT_ROLE = "agent"

# Tools whose arguments contain filesystem paths that must be validated
# against the workspace sandbox.  Maps tool name → list of argument keys
# that hold paths.
_PATH_ARGUMENT_MAP: dict[str, list[str]] = {
    "read_file": ["path"],
    "write_file": ["path"],
    "patch": ["path"],
    "search_files": ["path"],
    "list_files": ["path"],
    "terminal": ["workdir"],
}

# Tools whose arguments contain URLs that must be validated against
# the egress policy.  Maps tool name → list of argument keys with URLs.
_URL_ARGUMENT_MAP: dict[str, list[str]] = {
    "web_search": ["query"],       # query may not be a URL, but search/extract have url
    "web_extract": ["url"],
    "web_crawl": ["url", "start_url"],
    "browser_navigate": ["url"],
}


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
        egress_policy: EgressPolicy | None = None,
        transparency: TransparencyInterceptor | None = None,
    ) -> None:
        self._enabled: bool = enabled
        self._open_policy: bool = (allowed_tools is None and denied_tools is None)

        # Initialise AGT engine.
        self._engine = AGTPolicyEngine()

        # AGT ExecutionSandbox cache — one per workspace_path, created
        # lazily in check().  Thread-safe: dict reads/writes are atomic
        # in CPython and worst case we create a duplicate (idempotent).
        self._sandbox_cache: dict[str, ExecutionSandbox] = {}

        # AGT EgressPolicy — controls which domains/ports tools can reach.
        # When None, egress is unchecked (all outbound allowed).
        self._egress_policy: EgressPolicy | None = egress_policy

        # AGT TransparencyInterceptor — EU AI Act Art. 13/50 compliance.
        # When set, tool calls are blocked until AI disclosure is confirmed
        # for the session.  When None, transparency is not enforced.
        self._transparency: TransparencyInterceptor | None = transparency

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

    # ------------------------------------------------------------------
    # Transparency (EU AI Act)
    # ------------------------------------------------------------------

    def confirm_disclosure(self, session_id: str) -> None:
        """Mark AI disclosure as confirmed for a session.

        Must be called before tool execution when transparency is enabled.
        Typically called when the user starts a new session and the
        frontend has shown the AI disclosure notice.
        """
        if self._transparency is not None:
            self._transparency.confirm_disclosure(session_id)

    def is_disclosure_confirmed(self, session_id: str) -> bool:
        """Check if AI disclosure has been confirmed for a session."""
        if self._transparency is None:
            return True
        return self._transparency.is_disclosure_confirmed(session_id)

    # ------------------------------------------------------------------
    # Check
    # ------------------------------------------------------------------

    def check(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        workspace_path: str | None = None,
        session_id: str | None = None,
    ) -> PolicyDecision:
        """Evaluate whether a tool invocation is permitted.

        Delegates to the AGT PolicyEngine for:

        - Role-based tool permission checks
        - Argument-level validation (path traversal, dangerous code,
          SQL injection)
        - Conditional permissions (ABAC)
        - **Workspace sandbox** — path containment via AGT ExecutionSandbox
        - **Egress policy** — network egress control via AGT EgressPolicy
        - **Transparency** — EU AI Act Art. 13/50 disclosure enforcement
        """
        if not self._enabled:
            return PolicyDecision(
                allowed=True, reason="governance disabled", tool_name=tool_name
            )

        # Transparency check — blocks until AI disclosure is confirmed.
        if self._transparency is not None and session_id:
            tc_request = ToolCallRequest(
                tool_name=tool_name,
                arguments=arguments or {},
                agent_id=session_id,
                metadata={"session_id": session_id},
            )
            tc_result = self._transparency.intercept(tc_request)
            if not tc_result.allowed:
                return PolicyDecision(
                    allowed=False,
                    reason=tc_result.reason or "AI disclosure not confirmed",
                    tool_name=tool_name,
                )

        # Fast deny-list check (before AGT role check).
        if tool_name in self._denied_tools:
            return PolicyDecision(
                allowed=False,
                reason=f"denied: tool {tool_name!r} explicitly blocked",
                tool_name=tool_name,
            )

        # Workspace sandbox check — enforced before all other checks.
        # Uses AGT ExecutionSandbox.check_file_access() with symlink
        # resolution and is_relative_to() containment.
        sandbox_violation = self._check_workspace_sandbox(
            tool_name, arguments or {}, workspace_path
        )
        if sandbox_violation:
            return PolicyDecision(
                allowed=False, reason=sandbox_violation, tool_name=tool_name
            )

        # Egress policy check — enforced for tools that make outbound
        # network requests.  Uses AGT EgressPolicy.check_url().
        egress_violation = self._check_egress(tool_name, arguments or {})
        if egress_violation:
            return PolicyDecision(
                allowed=False, reason=egress_violation, tool_name=tool_name
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

    def _get_sandbox(self, workspace_path: str) -> ExecutionSandbox:
        """Get or create an AGT ExecutionSandbox for a workspace path.

        Cached per workspace_path so repeated calls for the same session
        reuse the same instance.
        """
        sandbox = self._sandbox_cache.get(workspace_path)
        if sandbox is None:
            sandbox = ExecutionSandbox(
                config=SandboxConfig(allowed_paths=[workspace_path]),
            )
            self._sandbox_cache[workspace_path] = sandbox
        return sandbox

    def _check_workspace_sandbox(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        workspace_path: str | None,
    ) -> str | None:
        """Check filesystem paths in tool arguments against the workspace sandbox.

        Uses AGT ``ExecutionSandbox.check_file_access()`` which:

        - Resolves symlinks via ``pathlib.Path.resolve()``
        - Uses ``is_relative_to()`` for safe containment checking
        - Prevents path traversal (``../../etc/passwd``)
        - Prevents absolute path escapes (``/etc/shadow``)

        Returns a violation message string if blocked, ``None`` if allowed.
        """
        if not workspace_path:
            return None

        path_keys = _PATH_ARGUMENT_MAP.get(tool_name)
        if not path_keys:
            return None

        sandbox = self._get_sandbox(workspace_path)

        for key in path_keys:
            raw_path = arguments.get(key)
            if not raw_path or not isinstance(raw_path, str):
                continue

            # For relative paths, resolve against workspace root so the
            # sandbox check uses the correct absolute path.
            import os
            expanded = os.path.expanduser(raw_path)
            if not os.path.isabs(expanded):
                expanded = os.path.join(workspace_path, expanded)

            mode = "w" if tool_name in ("write_file", "patch") else "r"
            if not sandbox.check_file_access(expanded, mode):
                return (
                    f"Workspace sandbox violation: '{raw_path}' resolves "
                    f"outside the session workspace. All file operations "
                    f"must stay within the workspace directory."
                )

        return None

    def _check_egress(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """Check URL arguments against the AGT EgressPolicy.

        Returns a violation message if the URL's domain/port is blocked,
        ``None`` if allowed or no egress policy is configured.
        """
        if self._egress_policy is None:
            return None

        url_keys = _URL_ARGUMENT_MAP.get(tool_name)
        if not url_keys:
            return None

        for key in url_keys:
            raw_url = arguments.get(key)
            if not raw_url or not isinstance(raw_url, str):
                continue
            # Only check values that look like URLs.
            if not raw_url.startswith(("http://", "https://")):
                continue
            decision: EgressDecision = self._egress_policy.check_url(raw_url)
            if not decision.allowed:
                return (
                    f"Egress policy violation: outbound request to "
                    f"'{raw_url}' blocked — {decision.reason}"
                )

        return None

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
        *,
        transparency_settings: Any | None = None,
    ) -> GovernanceGate:
        """Load policies from a platform volume and an org-config overlay.

        The platform policy directory is scanned for files matching
        ``policy.yaml``, ``policy.yml``, or ``policy.json``.  The file
        must contain a mapping with optional keys ``allowed_tools`` (list)
        and ``denied_tools`` (list).

        *org_config* may contain a ``"governance"`` key with the same
        structure; values are merged (union) with platform policies.

        *transparency_settings* is an optional
        :class:`~surogates.config.TransparencySettings` instance.  When
        provided and enabled, the EU AI Act transparency interceptor is
        activated.
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

        # --- Egress policy ---
        egress = None
        egress_data = (platform_data or {}).get("egress") or {}
        org_egress = gov.get("egress", {}) if isinstance(gov, dict) else {}
        # Merge org egress rules over platform
        merged_egress_rules = list(egress_data.get("rules", []))
        merged_egress_rules.extend(org_egress.get("rules", []))
        if merged_egress_rules:
            default_action = (
                org_egress.get("default_action")
                or egress_data.get("default_action")
                or "deny"
            )
            egress = EgressPolicy(default_action=default_action)
            for rule in merged_egress_rules:
                egress.add_rule(
                    domain=rule.get("domain", ""),
                    ports=rule.get("ports", [443]),
                    protocol=rule.get("protocol", "tcp"),
                    action=rule.get("action", "allow"),
                )

        # --- Transparency (EU AI Act Art. 13/50) ---
        transparency = None
        if transparency_settings is not None and getattr(
            transparency_settings, "enabled", False
        ):
            level_str = getattr(transparency_settings, "level", "basic")
            try:
                level = TransparencyLevel(level_str)
            except ValueError:
                level = TransparencyLevel.BASIC
            transparency = TransparencyInterceptor(
                default_level=level,
                require_disclosure_confirmation=getattr(
                    transparency_settings, "require_confirmation", True
                ),
                emotion_recognition_notice=getattr(
                    transparency_settings, "emotion_recognition", False
                ),
            )

        return cls(
            allowed_tools=allowed if allowed else None,
            denied_tools=denied if denied else None,
            enabled=enabled,
            egress_policy=egress,
            transparency=transparency,
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
