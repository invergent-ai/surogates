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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Matches POSIX shell variable references like ``$HOME`` or ``${HOME}``.
# Bare ``$`` (no name following) and Windows ``%VAR%`` are intentionally
# excluded — the former is harmless in a path, the latter is foreign to
# our Linux sandboxes and would create false positives.
_SHELL_VAR_RE: re.Pattern[str] = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")

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
    overridable: bool = False
    policy_id: str | None = None


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

        # Path-arg hygiene check — reject shell-variable patterns like
        # ``$HOME`` or ``${HOME}`` in path-typed arguments before they can
        # be taken literally by file tools and create directories named
        # ``$HOME`` on disk.  Runs before the workspace sandbox check so
        # the error message points at the real cause instead of an
        # opaque "outside workspace" rejection.
        hygiene_violation = self._check_path_arg_hygiene(
            tool_name, arguments or {}
        )
        if hygiene_violation:
            return PolicyDecision(
                allowed=False, reason=hygiene_violation, tool_name=tool_name
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

    def _check_path_arg_hygiene(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """Reject path arguments containing shell-variable references.

        Tool path arguments are passed verbatim to filesystem APIs — they
        are never expanded by a shell.  When the model writes
        ``$HOME/foo.py`` expecting expansion, the underlying ``open()``
        call creates a directory literally named ``$HOME``.  Catching the
        pattern here returns a clear, actionable error so the model
        switches to a relative path on the next turn instead of cascading
        through a dozen broken tool calls.

        Returns a violation message if a shell-variable pattern is
        present in any path-typed argument; ``None`` otherwise.
        """
        path_keys = _PATH_ARGUMENT_MAP.get(tool_name)
        if not path_keys:
            return None

        for key in path_keys:
            raw_path = arguments.get(key)
            if not isinstance(raw_path, str) or not raw_path:
                continue
            match = _SHELL_VAR_RE.search(raw_path)
            if match:
                return (
                    f"Path argument {key!r} contains shell variable "
                    f"{match.group(0)!r} — paths are taken literally and "
                    f"never expanded by a shell.  Use a relative path "
                    f"(working directory is the workspace) or omit the "
                    f"variable."
                )

        return None

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

    def with_profile(self, profile: dict[str, Any]) -> GovernanceGate:
        """Return a new :class:`GovernanceGate` narrowed by *profile*.

        Composition semantics (principle of least privilege):

        * ``allowed_tools``: intersected with the base allowlist when the
          base is set, otherwise taken as the new allowlist.
        * ``denied_tools``: unioned with the base denylist.
        * ``egress``: the profile's rules are appended to the base rules;
          ``default_action`` falls back to the base when the profile does
          not specify one.
        * ``enabled``: inherited from the base; the profile cannot
          re-enable a disabled base gate.

        The returned gate is **frozen** -- policy profiles are per-session
        overlays and must not be mutated mid-request.  This method does
        not mutate ``self``.
        """
        base_allowed = self._engine.state_permissions.get(
            _DEFAULT_ROLE, set(),
        ) if self._has_allow_list else None

        profile_allowed = profile.get("allowed_tools") or None
        profile_denied = profile.get("denied_tools") or []

        # Allowed: intersect when the base has an allowlist.
        if base_allowed is not None and profile_allowed is not None:
            new_allowed = set(base_allowed) & set(profile_allowed)
        elif profile_allowed is not None:
            new_allowed = set(profile_allowed)
        elif base_allowed is not None:
            new_allowed = set(base_allowed)
        else:
            new_allowed = None

        # Denied: union.
        new_denied = set(self._denied_tools)
        new_denied.update(profile_denied)

        # Egress: narrow semantics -- the composed default is the
        # stricter of base-vs-profile (``deny`` always beats ``allow``)
        # and profile rules are appended to the base so neither side can
        # loosen the posture unilaterally.
        new_egress: EgressPolicy | None = None
        profile_egress = profile.get("egress") or {}
        profile_rules = profile_egress.get("rules") or []
        profile_default = profile_egress.get("default_action")

        if self._egress_policy is not None or profile_rules:
            base_default = (
                getattr(self._egress_policy, "default_action", None)
                if self._egress_policy is not None else None
            )
            composed_default = _strictest_egress_default(
                base_default, profile_default,
            )

            new_egress = EgressPolicy(default_action=composed_default)
            base_rules = (
                getattr(self._egress_policy, "rules", None) or []
                if self._egress_policy is not None else []
            )
            for rule in base_rules:
                # AGT EgressRule exposes domain/ports/protocol/action as
                # public attrs.  Copy ports by value so the composed
                # policy cannot leak mutations back into the base.
                new_egress.add_rule(
                    domain=getattr(rule, "domain", ""),
                    ports=list(getattr(rule, "ports", [443])),
                    protocol=getattr(rule, "protocol", "tcp"),
                    action=getattr(rule, "action", "allow"),
                )
            for rule in profile_rules:
                new_egress.add_rule(
                    domain=rule.get("domain", ""),
                    ports=list(rule.get("ports", [443])),
                    protocol=rule.get("protocol", "tcp"),
                    action=rule.get("action", "allow"),
                )

        composed = GovernanceGate(
            allowed_tools=new_allowed if new_allowed is not None else None,
            denied_tools=new_denied if new_denied else None,
            enabled=self._enabled,
            egress_policy=new_egress,
            transparency=self._transparency,
        )
        composed.freeze()
        return composed


# ------------------------------------------------------------------
# File loading helpers
# ------------------------------------------------------------------


def _strictest_egress_default(base: str | None, profile: str | None) -> str:
    """Return the stricter of two egress default actions.

    ``deny`` is stricter than ``allow``; unknown / missing values fall
    back to ``deny`` so a misspelled profile cannot accidentally loosen
    the base's posture.  Used by :meth:`GovernanceGate.with_profile`
    to compose a new default-action under narrowing semantics.
    """
    candidates = [v for v in (base, profile) if v]
    if not candidates:
        return "deny"
    if any(v == "deny" for v in candidates):
        return "deny"
    # Every candidate must be "allow" for the composed default to be allow.
    return "allow" if all(v == "allow" for v in candidates) else "deny"


