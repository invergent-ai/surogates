"""MCP tool-definition scanner for safety analysis and rug-pull detection.

Combines our own pattern-based checks with Microsoft's Agent Governance
Toolkit ``MCPSecurityScanner`` for defense in depth.  Maintains SHA-256
fingerprints to detect definition mutations (*rug-pull* attacks) between
server reconnections.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import warnings
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Import AGT scanner — suppress the sample-rules warning since we layer
# our own patterns on top.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", UserWarning)
    from agent_os import MCPSecurityScanner as AGTScanner

# ---------------------------------------------------------------------------
# Invisible Unicode characters that should never appear in tool descriptions.
# ---------------------------------------------------------------------------
_INVISIBLE_CODEPOINTS: frozenset[str] = frozenset(
    {
        "\u200b",  # zero-width space
        "\u200c",  # zero-width non-joiner
        "\u200d",  # zero-width joiner
        "\ufeff",  # byte-order mark / zero-width no-break space
        "\u202a",  # left-to-right embedding
        "\u202b",  # right-to-left embedding
        "\u202c",  # pop directional formatting
        "\u202d",  # left-to-right override
        "\u202e",  # right-to-left override
    }
)

_INVISIBLE_PATTERN = re.compile(
    "[" + "".join(re.escape(c) for c in sorted(_INVISIBLE_CODEPOINTS)) + "]"
)

# ---------------------------------------------------------------------------
# HTML comment pattern
# ---------------------------------------------------------------------------
_HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)

# ---------------------------------------------------------------------------
# Prompt-injection heuristics (case-insensitive, word-boundary aware).
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bignore\s+(all\s+)?previous\b", re.IGNORECASE),
    re.compile(r"\boverride\s+(all\s+)?(instructions|rules|constraints)\b", re.IGNORECASE),
    re.compile(r"\bactually\s+do\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(all\s+)?(previous|above|prior)\b", re.IGNORECASE),
    re.compile(r"\bforget\s+(all\s+)?(previous|above|prior|your)\b", re.IGNORECASE),
    re.compile(r"\bdo\s+not\s+follow\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\bnew\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\bsystem\s*:\s*", re.IGNORECASE),
]


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Outcome of scanning a single MCP tool definition."""

    safe: bool
    threats: list[str]
    severity: str  # "info", "warning", "critical"


class MCPGovernance:
    """Scans MCP tool definitions for safety and tracks fingerprints.

    Runs two scanning layers:
    1. AGT ``MCPSecurityScanner`` — detects prompt injection, hidden
       instructions, cross-server attacks, rug-pulls.
    2. Our own pattern checks — invisible Unicode, HTML comments,
       injection regex, schema abuse.

    A tool must pass **both** layers to be considered safe.
    """

    def __init__(self) -> None:
        self._fingerprints: dict[str, str] = {}
        self._agt_scanner = AGTScanner()

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def scan_tool(self, tool_def: dict) -> ScanResult:
        """Scan a single tool definition for threats.

        Runs two layers:

        **Layer 1 — AGT MCPSecurityScanner:** Hidden instructions, prompt
        injection, cross-server impersonation (via ``scan_tool``).

        **Layer 2 — Our own checks:**

        1. Invisible Unicode characters in any string field.
        2. HTML comments that could hide instructions.
        3. Prompt-injection patterns in descriptions.
        4. Overly permissive JSON-Schema (``additionalProperties: true``
           at the top level without property constraints).
        """
        threats: list[str] = []
        severity = "info"

        # --- Layer 1: AGT scan ---
        tool_name = tool_def.get("name", "<unnamed>")
        description = tool_def.get("description", "")
        schema = tool_def.get("inputSchema") or tool_def.get("input_schema")
        server_name = tool_def.get("_server_name", "unknown")

        agt_threats = self._agt_scanner.scan_tool(
            tool_name, description, schema, server_name
        )
        for threat in agt_threats:
            threats.append(
                f"[AGT] {threat.threat_type.value}: {threat.message}"
            )
            agt_sev = threat.severity.value if hasattr(threat.severity, "value") else str(threat.severity)
            severity = _escalate(severity, agt_sev)

        # --- Layer 2: Our own checks ---
        text_fields = _extract_text_fields(tool_def)

        # 1. Invisible Unicode
        for field_path, value in text_fields:
            matches = _INVISIBLE_PATTERN.findall(value)
            if matches:
                codepoints = ", ".join(f"U+{ord(c):04X}" for c in set(matches))
                threats.append(
                    f"Invisible Unicode characters ({codepoints}) in {field_path}"
                )
                severity = _escalate(severity, "critical")

        # 2. HTML comments
        for field_path, value in text_fields:
            if _HTML_COMMENT_PATTERN.search(value):
                threats.append(f"HTML comment detected in {field_path}")
                severity = _escalate(severity, "warning")

        # 3. Prompt injection
        for field_path, value in text_fields:
            for pattern in _INJECTION_PATTERNS:
                match = pattern.search(value)
                if match:
                    threats.append(
                        f"Prompt injection pattern ({match.group()!r}) in {field_path}"
                    )
                    severity = _escalate(severity, "critical")
                    break  # one match per field is enough

        # 4. Schema abuse
        input_schema = tool_def.get("inputSchema") or tool_def.get("input_schema") or {}
        if isinstance(input_schema, dict):
            additional = input_schema.get("additionalProperties")
            properties = input_schema.get("properties")
            if additional is True and (not properties or len(properties) == 0):
                threats.append(
                    "Input schema allows arbitrary properties with no declared properties"
                )
                severity = _escalate(severity, "warning")

        safe = len(threats) == 0
        return ScanResult(safe=safe, threats=threats, severity=severity)

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------

    def register_fingerprint(self, tool_name: str, tool_def: dict) -> None:
        """Store the SHA-256 fingerprint of *tool_def* for future comparison.

        Registers with both our own fingerprint store and AGT's scanner.
        """
        fp = _fingerprint(tool_def)
        self._fingerprints[tool_name] = fp

        # Also register with AGT for its rug-pull detection.
        description = tool_def.get("description", "")
        schema = tool_def.get("inputSchema") or tool_def.get("input_schema")
        server_name = tool_def.get("_server_name", "unknown")
        self._agt_scanner.register_tool(tool_name, description, schema, server_name)

    def check_rug_pull(self, tool_name: str, tool_def: dict) -> bool:
        """Check whether *tool_def* still matches the stored fingerprint.

        Returns ``True`` if the fingerprint **matches** (no rug-pull).
        Returns ``False`` if the definition has **changed** or was never
        registered.
        """
        stored = self._fingerprints.get(tool_name)
        if stored is None:
            return False
        return stored == _fingerprint(tool_def)

    def has_fingerprint(self, tool_name: str) -> bool:
        """Return True when *tool_name* has been previously registered."""
        return tool_name in self._fingerprints

    def get_fingerprint(self, tool_name: str) -> str | None:
        """Return the stored SHA-256 fingerprint for *tool_name*, or None."""
        return self._fingerprints.get(tool_name)

    # ------------------------------------------------------------------
    # Bulk operations
    # ------------------------------------------------------------------

    def scan_and_filter(
        self,
        server_name: str,
        tools: list[dict],
    ) -> list[dict]:
        """Scan all tools from *server_name*, returning only safe ones.

        Safe tools have their fingerprints registered.  Unsafe tools are
        logged and excluded.  Tools that were previously registered and
        whose definitions have changed (rug-pull) are also excluded.
        """
        safe_tools: list[dict] = []

        for tool_def in tools:
            tool_name = tool_def.get("name", "<unnamed>")
            qualified_name = f"{server_name}.{tool_name}"

            # Rug-pull check for previously registered tools.
            if qualified_name in self._fingerprints:
                if not self.check_rug_pull(qualified_name, tool_def):
                    logger.warning(
                        "Rug-pull detected for %s from server %s -- "
                        "tool definition changed since last registration",
                        tool_name,
                        server_name,
                    )
                    continue

            result = self.scan_tool(tool_def)

            if not result.safe:
                logger.warning(
                    "Unsafe MCP tool %s from server %s [%s]: %s",
                    tool_name,
                    server_name,
                    result.severity,
                    "; ".join(result.threats),
                )
                continue

            self.register_fingerprint(qualified_name, tool_def)
            safe_tools.append(tool_def)

        logger.info(
            "MCP scan for server %s: %d/%d tools passed",
            server_name,
            len(safe_tools),
            len(tools),
        )
        return safe_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: dict[str, int] = {
    "info": 0,
    "warning": 1,
    "critical": 2,
}


def _escalate(current: str, proposed: str) -> str:
    """Return the more severe of *current* and *proposed*."""
    if _SEVERITY_ORDER.get(proposed, 0) > _SEVERITY_ORDER.get(current, 0):
        return proposed
    return current


def _fingerprint(tool_def: dict) -> str:
    """Compute a deterministic SHA-256 fingerprint for *tool_def*."""
    canonical = json.dumps(tool_def, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _extract_text_fields(
    obj: dict | list | str,
    path: str = "root",
) -> list[tuple[str, str]]:
    """Recursively extract all string values from a nested structure.

    Returns a list of ``(dotted_path, value)`` tuples.
    """
    results: list[tuple[str, str]] = []
    if isinstance(obj, str):
        results.append((path, obj))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            results.extend(_extract_text_fields(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            results.extend(_extract_text_fields(item, f"{path}[{idx}]"))
    return results
