"""MCP tool-definition scanner for safety analysis and rug-pull detection.

Combines our own pattern-based checks with Microsoft's Agent Governance
Toolkit ``MCPSecurityScanner`` for defense in depth.  Maintains SHA-256
fingerprints to detect definition mutations (*rug-pull* attacks) between
server reconnections.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import warnings
from dataclasses import dataclass, field

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

# ---------------------------------------------------------------------------
# AGT wording heuristics that ordinary tool documentation trips.
#
# AGT's scanner flags any URL ("Data exfiltration pattern: https?://"), the
# verbs of every messaging tool ("send email", "send to", "post to"), any
# second-person rule ("you must", "you are", "respond with" ... = "Role
# override") and any 40-character run of base64 letters ("Encoded payload"),
# which long ids and URL paths are.  Dropping on these silently removed real
# tools: Google Calendar's ``add_calendar_to_list`` ("you must set
# colorRgbFormat"), the Teams webinar tools (a doc URL), Gmail's
# ``gmail_send_message`` ("send email").  One kind alone is an advisory: the
# tool is kept, and the hit is audited and logged.  Two kinds together (a rule
# aimed at the model and a destination, or a send / post verb and a URL) are
# the shape of an exfiltration instruction and still drop the tool, as does an
# encoded run that decodes to readable text.
# ---------------------------------------------------------------------------
_ROLE_OVERRIDE_MESSAGE = "Role override pattern in description:"
_EXFILTRATION_MESSAGE = "Data exfiltration pattern in description:"
_ENCODED_PAYLOAD_MESSAGE = "Encoded payload detected in tool description"
_URL_PATTERN = r"https?://"
_SEND_PATTERNS = frozenset({r"\bsend\s+email\b", r"\bsend\s+to\b", r"\bpost\s+to\b"})
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_HEX_ESCAPES = re.compile(r"(?:\\x[0-9a-fA-F]{2}){4,}")


# ---------------------------------------------------------------------------
# The shapes of published tool-poisoning attacks (Invariant Labs' ``add`` /
# shadowing / WhatsApp examples), which the wording heuristics never caught: a
# tag block addressed to the model, an instruction to keep something from the
# user, and secret files a tool description has no reason to name.
# ---------------------------------------------------------------------------
_SECRET_FILE = re.compile(
    r"(\.ssh/|\bid_(rsa|dsa|ecdsa|ed25519)\b|\.aws/credentials|\.netrc\b|\.git-credentials\b"
    r"|\.kube/config|/etc/(passwd|shadow)\b|\bmcp\.json\b|\.cursor/|\.gnupg/)", re.IGNORECASE)
_HAND_OVER = re.compile(r"\b(pass|include|send|attach|upload|copy|append|forward)\b", re.IGNORECASE)
# (label, pattern, companion): a hit needs the companion in the same text too, when one is given.
_POISONING_PATTERNS: list[tuple[str, re.Pattern[str], re.Pattern[str] | None]] = [
    ("Instruction block addressed to the model", re.compile(
        r"<\s*/?\s*(important|system|instructions?|hidden)\s*>", re.IGNORECASE), None),
    ("Instruction to keep something from the user", re.compile(
        r"\b(do\s+not|don'?t|never)\s+(tell|mention|inform|notify|reveal|alert)\b[^.\n]{0,60}"
        r"\b(user|human|owner|operator)s?\b", re.IGNORECASE), None),
    ("Secret file handed over by a tool definition", _SECRET_FILE, _HAND_OVER),
]


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Outcome of scanning a single MCP tool definition."""

    safe: bool
    threats: list[str]
    severity: str  # "info", "warning", "critical"
    # Wording-heuristic hits that do not make the tool unsafe (see
    # ``_wording_advisories``); audited and logged, the tool is kept.
    advisories: list[str] = field(default_factory=list)


class MCPGovernance:
    """Scans MCP tool definitions for safety and tracks fingerprints.

    Runs two scanning layers:
    1. AGT ``MCPSecurityScanner`` — detects prompt injection, hidden
       instructions, cross-server attacks, rug-pulls.
    2. Our own pattern checks — invisible Unicode, HTML comments,
       injection regex, the shapes of published poisoning attacks
       (``_POISONING_PATTERNS``), schema abuse.

    A tool must pass **both** layers to be considered safe.  AGT's wording
    heuristics (a URL, a send / post verb, a second-person rule, an id that
    looks like base64) are advisories when one kind appears alone: the tool
    stays, and the hit is reported in ``ScanResult.advisories``.
    """

    def __init__(self, fingerprints: dict[str, str] | None = None) -> None:
        # The approved fingerprints.  A caller that keeps them across
        # connections (the MCP proxy's pool, per tenant) passes its own
        # dict, which this instance then reads and writes.
        self._fingerprints: dict[str, str] = {} if fingerprints is None else fingerprints
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
        advisory = _wording_advisories(agt_threats, description)
        advisories: list[str] = []
        for threat in agt_threats:
            text = f"[AGT] {threat.threat_type.value}: {threat.message}"
            if id(threat) in advisory:
                advisories.append(text)
                continue
            threats.append(text)
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

        # 3. Prompt injection, and the shapes of published poisoning attacks
        for field_path, value in text_fields:
            for label, pattern, companion in _POISONING_PATTERNS:
                match = pattern.search(value)
                if match and (companion is None or companion.search(value)):
                    threats.append(f"{label} ({match.group()!r}) in {field_path}")
                    severity = _escalate(severity, "critical")
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
        return ScanResult(safe=safe, threats=threats, severity=severity, advisories=advisories)

    # ------------------------------------------------------------------
    # Fingerprinting
    # ------------------------------------------------------------------

    def register_fingerprint(self, tool_name: str, tool_def: dict) -> None:
        """Store the SHA-256 fingerprint of *tool_def* for future comparison.

        This store is the rug-pull check (:meth:`check_rug_pull`).  Nothing
        is registered with AGT's scanner.  Tools used to be registered there
        under the qualified name (``server.tool``) and scanned under the bare
        one, so AGT's own rug-pull check never matched, and its registry
        never outlived a connection anyway.  Registered under the bare name,
        its cross-server checks would refuse any tool whose name is within
        two edits of another server's (``get_user`` / ``get_users``).
        """
        self._fingerprints[tool_name] = _fingerprint(tool_def)

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

    def fingerprint_count(self) -> int:
        """How many tools have an approved fingerprint."""
        return len(self._fingerprints)

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

            if result.safe and result.advisories:
                logger.warning(
                    "MCP tool %s from server %s kept with scan advisories (wording heuristics, not dropped): %s",
                    tool_name, server_name, "; ".join(result.advisories),
                )

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


def _wording_advisories(agt_threats: list, description: str) -> set[int]:
    """The ids of the AGT hits that are only wording heuristics (see the
    constants above).  Of the three kinds (a role-override phrase, a URL, a
    send / post verb) one alone is advisory, two or more drop the tool.  An
    encoded run is advisory unless it is a hex-escape run or decodes to
    readable text (``_hides_text``)."""
    role = [t for t in agt_threats if _message(t).startswith(_ROLE_OVERRIDE_MESSAGE)]
    exfil = [t for t in agt_threats if _message(t).startswith(_EXFILTRATION_MESSAGE)]
    url = [t for t in exfil if getattr(t, "matched_pattern", None) == _URL_PATTERN]
    send = [t for t in exfil if getattr(t, "matched_pattern", None) in _SEND_PATTERNS]
    kinds = [group for group in (role, url, send) if group]
    advisory: set[int] = set()
    if len(kinds) == 1:
        advisory |= {id(t) for t in kinds[0]}
    if not _hides_text(description):
        advisory |= {id(t) for t in agt_threats if _message(t) == _ENCODED_PAYLOAD_MESSAGE}
    return advisory


def _message(threat) -> str:
    return str(getattr(threat, "message", "") or "")


def _hides_text(description: str) -> bool:
    """True when *description* carries hex escapes, or a base64 run that
    decodes to readable text (an instruction hidden from a reader), rather
    than an id or a URL path that only looks like base64."""
    if _HEX_ESCAPES.search(description):
        return True
    for match in _BASE64_RUN.finditer(description):
        run = match.group().rstrip("=")
        try:
            decoded = base64.b64decode(run + "=" * (-len(run) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        printable = sum(ch.isprintable() or ch.isspace() for ch in decoded)
        if decoded and printable / len(decoded) >= 0.9 and sum(ch.isalpha() for ch in decoded) >= 8:
            return True
    return False


def _escalate(current: str, proposed: str) -> str:
    """Return the more severe of *current* and *proposed*."""
    if _SEVERITY_ORDER.get(proposed, 0) > _SEVERITY_ORDER.get(current, 0):
        return proposed
    return current


def _fingerprint(tool_def: dict) -> str:
    """Compute a deterministic SHA-256 fingerprint for *tool_def*.

    Key order never counts, nor the order of a schema's ``required`` names
    (a server may build that list from a set, in a new order each start).
    """
    canonical = json.dumps(_canonical(tool_def), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _canonical(value):
    """*value* with every ``required`` list of names sorted."""
    if isinstance(value, dict):
        return {
            key: sorted(item) if key == "required" and isinstance(item, list)
            and all(isinstance(name, str) for name in item) else _canonical(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


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
