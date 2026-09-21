"""EU AI Act Art. 13/50 disclosure copy.

The levels and their texts; the disclosure itself is delivered by
``surogates.runtime.governance`` (which resolves the level per agent)
and rendered by the channels and the public ``GET /transparency``
endpoint.
"""

from __future__ import annotations

from enum import Enum


class TransparencyLevel(str, Enum):
    """Disclosure level per EU AI Act risk classification."""

    NONE = "none"
    BASIC = "basic"
    ENHANCED = "enhanced"
    FULL = "full"


# Disclosure texts deliberately do NOT self-classify the system (the
# earlier drafts claimed "high-risk AI system", which is a legal
# classification under AI Act Art. 6 that a patient-communication or
# assistant agent does not carry — asserting it in end-user copy is
# both wrong and harmful). The levels scale detail, not risk claims.
DISCLOSURE_TEXTS = {
    TransparencyLevel.BASIC: (
        "You are interacting with an AI assistant. Replies are "
        "machine-generated and may contain errors. (EU AI Act Art. 50(1))"
    ),
    TransparencyLevel.ENHANCED: (
        "You are interacting with an AI assistant. Replies are "
        "machine-generated, may contain errors, and are logged. The AI "
        "follows the operator's usage policy and you can always ask for "
        "a human contact. (EU AI Act Art. 50(1))"
    ),
    TransparencyLevel.FULL: (
        "You are interacting with an AI assistant operated on the "
        "Surogate platform. Replies are machine-generated, may contain "
        "errors, and are logged and policy-governed; a human operator "
        "reviews escalations and you can request a human contact at any "
        "time. This notice is provided under EU AI Act Art. 50(1); "
        "further information is available from the operator."
    ),
}


def disclosure_text_for(level: TransparencyLevel) -> str:
    """The disclosure copy for a level, falling back to BASIC."""
    return DISCLOSURE_TEXTS.get(level, DISCLOSURE_TEXTS[TransparencyLevel.BASIC])
