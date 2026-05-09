from __future__ import annotations

import re


class ScheduledPromptBlocked(ValueError):
    """Raised when a scheduled prompt is too risky to persist."""


_THREAT_PATTERNS = [
    (
        r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions",
        "prompt_injection",
    ),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "system_prompt_override"),
    (r"disregard\s+(?:your|all|any)\s+(?:instructions|rules|guidelines)", "disregard_rules"),
    (r"curl\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_curl"),
    (r"wget\s+[^\n]*\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", "exfil_wget"),
    (r"cat\s+[^\n]*(?:\.env|credentials|\.netrc|\.pgpass)", "read_secrets"),
    (r"authorized_keys", "ssh_backdoor"),
    (r"/etc/sudoers|visudo", "sudoers_mod"),
    (r"rm\s+-rf\s+/", "destructive_root_rm"),
]
_INVISIBLE_CHARS = {
    "\u200b",
    "\u200c",
    "\u200d",
    "\u2060",
    "\ufeff",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
}


def validate_scheduled_prompt(prompt: str, *, source: str = "cron_create") -> None:
    text = (prompt or "").strip()
    if not text:
        raise ScheduledPromptBlocked("Scheduled prompt cannot be empty.")
    for char in _INVISIBLE_CHARS:
        if char in text:
            raise ScheduledPromptBlocked(
                f"Blocked: prompt contains invisible unicode U+{ord(char):04X}.",
            )
    for pattern, reason in _THREAT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            raise ScheduledPromptBlocked(
                f"Blocked: scheduled prompt matches threat pattern '{reason}'.",
            )

    try:
        from agent_os.prompt_injection import PromptInjectionDetector

        result = PromptInjectionDetector().detect(text, source=source)
    except Exception:
        return
    if getattr(result, "is_injection", False):
        explanation = getattr(result, "explanation", "prompt injection detected")
        raise ScheduledPromptBlocked(f"Blocked: {explanation}")
