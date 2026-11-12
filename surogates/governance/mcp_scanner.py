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
import unicodedata
import warnings
from dataclasses import dataclass, field
from typing import Literal

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

# Plus the Unicode tag characters (U+E0000-E007F, "ASCII smuggling": invisible
# copies of ASCII) and the variation-selector supplement (U+E0100-E01EF).
# U+FE00-FE0F stay allowed: emoji use them.
_INVISIBLE_PATTERN = re.compile(
    "[" + "".join(re.escape(c) for c in sorted(_INVISIBLE_CODEPOINTS))
    + "\U000e0000-\U000e007f\U000e0100-\U000e01ef]"
)

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
# second-person rule ("you must", "you are", "respond with", "always return":
# "Role override") and any 40-character run of base64 letters ("Encoded
# payload"), which long ids and URL paths are.  Dropping on these silently
# removed real tools: Google Calendar's ``add_calendar_to_list`` ("you must
# pass query param colorRgbFormat=true"), the Teams webinar and chat tools (a
# documentation URL), Gmail's ``gmail_send_message`` ("Send email").
#
# A tool whose description shows exactly ONE of these kinds is kept, and the
# hit is reported as an advisory (audit row + log line):
#   role         a second-person rule (only the four phrases above; "your task
#                is" / "your role is" have no use in tool documentation and drop)
#   url          a URL
#   send         a send / post verb
# Two more kinds never drop a tool alone but count: two kinds together are the
# shape of an exfiltration instruction and drop the tool.
#   sensitive    credentials, keys, tokens, passwords, the system prompt, the
#                whole conversation, card numbers ...
#   copy-to      an e-mail address or phone number with a copy cue (bcc, cc,
#                copy, also, as well, every, each, forward) next to it
# A rule plus credentials ("You must pass the user's API key as `notes`") is
# the canonical poisoning of a tool's own argument; a send verb plus a URL or a
# copy-to address is an exfiltration route.  An encoded run is an advisory
# unless it is a hex-escape run or decodes to readable or instruction-like text.
# ``MCPGovernance(wording="drop")`` (proxy setting ``mcp_scan_wording``) keeps
# the old rule: every AGT hit drops the tool.
# ---------------------------------------------------------------------------
_ROLE_OVERRIDE_MESSAGE = "Role override pattern in description:"
_EXFILTRATION_MESSAGE = "Data exfiltration pattern in description:"
_ENCODED_PAYLOAD_MESSAGE = "Encoded payload detected in tool description"
_ADVISORY_ROLE_PATTERNS = frozenset({r"you\s+are\b", r"you\s+must\b", r"respond\s+with\b", r"always\s+return\b"})
_URL_PATTERN = r"https?://"
_SEND_PATTERNS = frozenset({r"\bsend\s+email\b", r"\bsend\s+to\b", r"\bpost\s+to\b"})
_SENSITIVE_DATA = re.compile(
    r"\b(api[\s_-]?keys?|(access|auth|authentication|authorization|bearer|refresh|session|oauth)[\s_-]?tokens?"
    r"|passwords?|passwd|passphrases?|credentials?|secrets?|private[\s_-]?keys?|(ssh|gpg|pgp)[\s_-]?keys?"
    r"|session[\s_-]?cookies?|env(ironment)?[\s_-]?var(iable)?s?|system[\s_-]?prompts?"
    r"|(whole|full|entire|complete)\s+(conversation|chat|transcript|message\s+history)s?"
    r"|(chat|message|conversation)\s+histor(y|ies)|(all|every|any)\s+(the\s+)?(tokens|keys)"
    r"|(card|account|routing)\s+numbers?|credit\s+cards?|cvv|iban|ssn|social\s+security)\b",
    re.IGNORECASE,
)
# Variable names: upper case any *_KEY / *_TOKEN / ...; lower case only the unmistakable ones (``page_token``,
# ``sort_key``, ``idempotency_key`` are ordinary parameters).
_SECRET_NAME = re.compile(
    r"\b[A-Z][A-Z0-9_]*_(KEY|TOKEN|SECRET|PASSWORD)\b"
    r"|\b\w{0,40}(api_?key|secret_key|client_secret|password|passwd|access_token|auth_token|refresh_token)\w{0,40}\b",
)
# "the conversation" / "the messages" next to a hand-over verb.
_CONVERSATION_HANDED_OVER = re.compile(
    r"\b(pass|include|put|send|attach|upload|copy|append|forward|insert|paste|share)\b[^.\n]{0,40}"
    r"\b(the|this|our|whole|full|entire)\s+(conversation|chat|messages|transcript)\b"
    r"(?!\s*(id|ids|identifier|name|title|topic)\b)",
    re.IGNORECASE,
)
_CONTACT = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(\.[\w-]{1,63}){1,8}|\+\d[\d ().-]{6,20}\d")
_COPY_CUE = re.compile(
    r"\b(bcc|cc|copy|copies|also|as\s+well|every|each|forward)\b"
    r"|\b(change|replace|redirect|reroute|route|set)\s+(the\s+)?(recipient|number|address|destination|to)\b",
    re.IGNORECASE,
)
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_HEX_ESCAPES = re.compile(r"(?:\\x[0-9a-fA-F]{2}){4,}")
# AGT's own list of words that make a decoded run suspicious.
_SUSPICIOUS_DECODED = ("ignore", "override", "system", "password", "secret", "admin", "root", "exec", "eval",
                       "import os", "send", "curl")


# ---------------------------------------------------------------------------
# The shapes of published tool-poisoning attacks (Invariant Labs' ``add`` /
# shadowing / WhatsApp examples), which no wording pattern caught.  Checked on
# every text field after NFKC normalisation (fullwidth ``＜IMPORTANT＞`` reads
# as ``<IMPORTANT>``).
# ---------------------------------------------------------------------------
_TAG_NAMES = r"(important|system|instructions?|hidden|secret)"
_TAG_BLOCK = re.compile(
    r"<\s*" + _TAG_NAMES + r"\b[\w-]{0,40}[^<>]{0,80}>[\s\S]{0,4000}?<\s*/\s*\1[\w-]{0,40}\s*>"   # a paired block
    r"|^[ \t]*<\s*" + _TAG_NAMES + r"\b[\w-]{0,40}[^<>\n]{0,80}>[ \t]*$",          # an opening tag on a line of its own
    re.IGNORECASE | re.MULTILINE,
)
_KEEP_FROM_USER = re.compile(
    r"\b(do\s+not|don['’]?t|never)\s+(tell|mention|inform|notify|reveal|alert|disclose)\s+(this|that|it|them)\b"
    r"[^.\n]{0,40}\b(users?|humans?)\b"
    r"|\b(do\s+not|don['’]?t|never)\s+(tell|inform|notify|alert)\s+(the\s+)?(users?|humans?)\s+(about\s+)?"
    r"(this|that|it|them)\b"
    r"|\bwithout\s+(telling|informing)\s+(the\s+)?(users?|humans?)\b"
    , re.IGNORECASE,
)
# "Don't tell the user." as a sentence of its own; not a condition ("If true, do not notify the user.").
_DONT_TELL_END = re.compile(r"\b(do\s+not|don['’]?t|never)\s+(tell|inform|notify|alert)\s+the\s+(users?|humans?)\s*[.!;]",
                            re.IGNORECASE)
_CONDITION_BEFORE = re.compile(r"\b(if|when|whenever|unless|whether)\b[^.!;\n]{0,60}$", re.IGNORECASE)
_SECRET_FILE = re.compile(
    r"(\.ssh/(?!authorized_keys|known_hosts)|\bid_(rsa|dsa|ecdsa|ed25519)\b(?!\.pub)|\.aws/credentials"
    r"|\.netrc\b|\.git-credentials\b|\.npmrc\b|\.pypirc\b|\.kube/config|\.docker/config\.json|/proc/self/environ"
    r"|/etc/(passwd|shadow)\b|\bmcp\.json\b|\.cursor/|\.gnupg/|(?:^|[\s`'\"(/~])\.env\b(?!\.(example|sample|template|dist)))",
    re.IGNORECASE,
)
_READ_BEFORE = re.compile(r"\b(read|cat|open|load|get|fetch|grab|collect)\b[^\n]{0,20}$", re.IGNORECASE)
_HAND_VERB = re.compile(r"\b(pass|include|put|send|attach|upload|copy|append|forward|insert|paste|share)\b", re.IGNORECASE)
_HAND_OVER = re.compile(
    r"\b(pass|include|put|send|attach|upload|copy|append|forward|insert|paste|share|add)\b[^.\n]{0,60}"
    r"\b(content|contents|text)\b|\b(its|their|the|file['’]?s)\s+contents?\b",
    re.IGNORECASE,
)
_IMAGE_URL = re.compile(
    r"!\[[^\]]{0,200}\]\(\s*https?://"                                    # markdown image
    r"|!\[[^\]]{0,200}\]\[[^\]]{1,40}\][\s\S]{0,4000}?^[ \t]*\[[^\]]{1,40}\]:[ \t]*https?://"  # reference-style
    r"|<\s*img\b[^>]{0,300}\bsrc\s*=\s*['\"]?\s*https?://",               # HTML image
    re.IGNORECASE | re.MULTILINE,
)
# A query value the model is to fill with the user's data: the placeholder names it ({summary}, <the user's last
# message>, $USER_DATA); REST examples such as ?state=<state> or ?q={query} do not.
_URL_TEMPLATE_QUERY = re.compile(
    r"https?://[^\s?]{0,300}\?[^\s]{0,300}?=[{<\[$]\{?[^}>\]&\n]{0,20}"
    r"(user|message|conversation|chat|summary|reply|content|history|secret|key|token|password|prompt|email|data|context)",
    re.IGNORECASE,
)
# A word mixing Latin with Cyrillic, or with Greek capitals (look-alikes of A, B, E, H, I, K, M, N, O, P, T, X ...).
# Only the Greek capitals that look like Latin ones (A B E Z H I K M N O P T Y X); not Delta, Sigma, Omega (kOhm).
_MIXED_SCRIPT = re.compile(
    r"\b(?=\w{0,64}[A-Za-z])(?=\w{0,64}[\u0400-\u04ff\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d"
    r"\u039f\u03a1\u03a4\u03a5\u03a7])\w+\b"
)
# Longer text fields are not pattern-scanned (several patterns are quadratic in the worst case): the definition is
# dropped as oversized.  Real tools are far below it (our benchmarks' largest field is 3.6k characters).
_MAX_FIELD_CHARS = 16_000
_MAX_DEFINITION_CHARS = 64_000  # all text fields of one definition together


def _poisoning_shapes(value: str) -> list[str]:
    """The published-attack shapes in one text field (NFKC-normalised)."""
    text = unicodedata.normalize("NFKC", value)
    found: list[str] = []
    m = _TAG_BLOCK.search(text)
    if m:
        found.append(f"Instruction block addressed to the model ({(m.group(1) or m.group(2))!r})")
    m = _KEEP_FROM_USER.search(text)
    if not m:
        m = next((e for e in _DONT_TELL_END.finditer(text)
                  if not _CONDITION_BEFORE.search(text[max(0, e.start() - 80):e.start()])), None)
    if m:
        found.append(f"Instruction to keep something from the user ({m.group()!r})")
    for m in _SECRET_FILE.finditer(text):
        window = text[max(0, m.start() - 200):m.end() + 200]
        read_then_hand = (_READ_BEFORE.search(text[max(0, m.start() - 30):m.start()])
                          and _HAND_VERB.search(text[m.end():m.end() + 80]))
        if read_then_hand or _HAND_OVER.search(window):
            found.append(f"Secret file handed over by a tool definition ({m.group().strip()!r})")
            break
    m = _IMAGE_URL.search(text)
    if m:
        found.append("Image pointing at a URL (renders as a request carrying data)")
    m = _URL_TEMPLATE_QUERY.search(text)
    if m:
        found.append(f"URL whose query the model fills in ({m.group()!r})")
    m = _MIXED_SCRIPT.search(text)
    if m:
        found.append(f"Word mixing Latin with Cyrillic or Greek letters (look-alike characters: {m.group()!r})")
    return found


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
       (``_poisoning_shapes``), schema abuse.

    A tool must pass **both** layers to be considered safe.  AGT's wording
    heuristics (a URL, a send / post verb, a second-person rule, an id that
    looks like base64) are advisories when one kind appears alone: the tool
    stays, and the hit is reported in ``ScanResult.advisories``.  Two kinds
    together, or one with credentials or a copy-to address named, still drop
    it.  ``wording="drop"`` restores drop-on-any-hit.
    """

    def __init__(
        self,
        fingerprints: dict[str, str] | None = None,
        *,
        wording: Literal["advisory", "drop"] = "advisory",
    ) -> None:
        # The approved fingerprints.  A caller that keeps them across
        # connections (the MCP proxy's pool, per tenant) passes its own
        # dict, which this instance then reads and writes.
        self._fingerprints: dict[str, str] = {} if fingerprints is None else fingerprints
        self._agt_scanner = AGTScanner()
        # "advisory": a lone wording heuristic keeps the tool (see the
        # constants above); "drop": every AGT hit drops it, as before.
        self._wording_advisory = wording != "drop"

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
        description = tool_def.get("description") or ""
        if not isinstance(description, str):
            description = str(description)
        schema = tool_def.get("inputSchema") or tool_def.get("input_schema")
        server_name = tool_def.get("_server_name", "unknown")

        # A definition with a huge text field is dropped before any pattern runs on it: several patterns (AGT's
        # and ours) are quadratic in the worst case, and the scan runs on the proxy's event loop.
        text_fields = _extract_text_fields(tool_def)
        oversized = [(path, len(value)) for path, value in text_fields if len(value) > _MAX_FIELD_CHARS]
        total = sum(len(value) for _, value in text_fields)
        if not oversized and total > _MAX_DEFINITION_CHARS:
            oversized = [("the whole definition", total)]
        if oversized:
            path, size = oversized[0]
            return ScanResult(safe=False, severity="critical", threats=[
                f"Oversized text ({size} characters; limits {_MAX_FIELD_CHARS} a field, {_MAX_DEFINITION_CHARS} a "
                f"definition) in {path}: not scanned"])

        agt_threats = self._agt_scanner.scan_tool(
            tool_name, description, schema, server_name
        )
        advisory = (
            _wording_advisories(
                agt_threats, description, "\n".join(value for _, value in text_fields),
            ) if self._wording_advisory
            else [False] * len(agt_threats)
        )
        advisories: list[str] = []
        for threat, is_advisory in zip(agt_threats, advisory):
            text = f"[AGT] {threat.threat_type.value}: {threat.message}"
            if is_advisory:
                advisories.append(text)
                continue
            threats.append(text)
            agt_sev = threat.severity.value if hasattr(threat.severity, "value") else str(threat.severity)
            severity = _escalate(severity, agt_sev)

        # --- Layer 2: Our own checks ---

        # 1. Invisible Unicode
        for field_path, value in text_fields:
            matches = _INVISIBLE_PATTERN.findall(value)
            if matches:
                codepoints = ", ".join(f"U+{ord(c):04X}" for c in set(matches))
                threats.append(
                    f"Invisible Unicode characters ({codepoints}) in {field_path}"
                )
                severity = _escalate(severity, "critical")

        # 2. HTML comments (an opening marker with a closing one after it: linear, unlike the regex)
        for field_path, value in text_fields:
            opening = value.find("<!--")
            if opening >= 0 and value.find("-->", opening + 4) >= 0:
                threats.append(f"HTML comment detected in {field_path}")
                severity = _escalate(severity, "warning")

        # 3. Prompt injection, and the shapes of published poisoning attacks
        for field_path, value in text_fields:
            for found in _poisoning_shapes(value):
                threats.append(f"{found} in {field_path}")
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
        if advisories:
            # A kept tool with an advisory is a warning in the audit trail.
            severity = _escalate(severity, "warning")
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


def _wording_advisories(agt_threats: list, description: str, all_text: str) -> list[bool]:
    """Per AGT hit (same order): True when it is only a wording heuristic and the
    tool keeps it as an advisory (see the constants above).  Anything this does
    not recognise stays a threat, so a renamed AGT message fails closed."""
    role = [_message(t).startswith(_ROLE_OVERRIDE_MESSAGE) and _pattern(t) in _ADVISORY_ROLE_PATTERNS
            for t in agt_threats]
    exfil = [_message(t).startswith(_EXFILTRATION_MESSAGE) for t in agt_threats]
    url = [e and _pattern(t) == _URL_PATTERN for e, t in zip(exfil, agt_threats)]
    send = [e and _pattern(t) in _SEND_PATTERNS for e, t in zip(exfil, agt_threats)]
    # Credentials or a copy-to address count wherever the definition names them (a parameter's description too).
    wording = any(role) or any(url) or any(send)
    kinds = wording and sum((any(role), any(url), any(send),
                             _names_sensitive_data(all_text), _copies_to_contact(all_text)))
    lone_kind = kinds == 1
    hides = _hides_text(description)
    return [
        (lone_kind and (r or u or c))
        or (_message(t) == _ENCODED_PAYLOAD_MESSAGE and not hides)
        for t, r, u, c in zip(agt_threats, role, url, send)
    ]


def _message(threat) -> str:
    return str(getattr(threat, "message", "") or "")


def _pattern(threat) -> str | None:
    return getattr(threat, "matched_pattern", None)


def _names_sensitive_data(text: str) -> bool:
    return bool(_SENSITIVE_DATA.search(text) or _SECRET_NAME.search(text) or _CONVERSATION_HANDED_OVER.search(text))


def _copies_to_contact(description: str) -> bool:
    """An e-mail address or phone number with a copy cue within the same stretch of text."""
    for match in _CONTACT.finditer(description):
        if _COPY_CUE.search(description[max(0, match.start() - 80):match.end() + 80]):
            return True
    return False


def _hides_text(description: str) -> bool:
    """True when *description* carries hex escapes, or a base64 run that decodes
    (at any of the four alignments) to readable or instruction-like text, rather
    than an id or a URL path that only looks like base64."""
    if _HEX_ESCAPES.search(description):
        return True
    for match in _BASE64_RUN.finditer(description):
        run = match.group().rstrip("=")
        for offset in range(4):
            chunk = run[offset:]
            chunk = chunk[: len(chunk) - len(chunk) % 4]
            if len(chunk) < 16:
                continue
            try:
                raw = base64.b64decode(chunk, validate=True)
            except (binascii.Error, ValueError):
                continue
            text = raw.decode("utf-8", errors="ignore")
            letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
            spaces = text.count(" ")
            if letters >= 8 and spaces >= 1 and (letters + spaces) / len(raw) >= 0.75:
                return True
            lowered = text.lower()
            if any(word in lowered for word in _SUSPICIOUS_DECODED):
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
