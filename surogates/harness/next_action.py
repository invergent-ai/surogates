"""Outbound stripping of the per-turn ``<next_action>`` footer.

The harness no longer instructs the model to emit a ``<next_action>``
footer (the ``guidance/next_action`` prompt fragment and the complexity
parser that consumed it were removed).  This stripper is retained as a
purely defensive outbound filter: replayed history from older sessions
and the occasional unprompted emission can still carry the block, which
is harness/UI metadata rather than user-facing content.  The web chat UI
strips it client-side (``sdk/agent-chat-react/src/lib/next-action.ts``)
and messaging channels (Telegram, Slack, Teams, ...) deliver raw text,
so the session store strips it server-side before enqueueing channel
deliveries.

The strip is a closer-driven linear scan, NOT a single block regex.  Any
pattern of the shape ``<next_action[^>]*>...</next_action>`` — lazy,
greedy, or tempered — costs O(openers x length) on input with many
unclosed openers, because every opener position walks toward a closing
tag that never arrives (measured ~300s at 50k openers for both the bare
and the tempered form).  Model output can echo prompt-injected garbage
verbatim, and this runs on the delivery path, so worst-case input must
stay linear.  Scanning for the rare ``</next_action>`` closer first and
matching one bounded opener per segment is O(length) by construction.

Kept dependency-free so the session layer can import it without pulling
LLM-client modules.
"""

from __future__ import annotations

import re

__all__ = ["strip_next_action_blocks"]

# One opening tag.  Only ever searched within a bounded, non-overlapping
# segment (or once against the tail), so it cannot compound.
_OPEN_TAG_RE = re.compile(r"<next_action\b[^>]*>", re.IGNORECASE)

_CLOSE_TAG = "</next_action>"
_OPEN_PREFIX = "<next_action"


def strip_next_action_blocks(text: str) -> str:
    """Return *text* with every ``<next_action>`` block removed.

    Mirrors the web SDK's client-side stripping so messaging-channel
    users never see the raw XML footer:

    - every opener..closer block goes, including malformed openers with
      no attributes;
    - a dangling opener with no closer (token-limit truncation) is
      dropped through end-of-text;
    - an orphan closer with no opener is left verbatim.

    Trailing whitespace left by the removal is trimmed; interior text is
    otherwise preserved.  A message that consisted solely of the footer
    strips to ``""`` — callers treat that as nothing-to-deliver.
    """
    lower = text.lower()
    if _OPEN_PREFIX not in lower:
        return text

    pieces: list[str] = []
    pos = 0
    while True:
        close = lower.find(_CLOSE_TAG, pos)
        if close == -1:
            break
        match = _OPEN_TAG_RE.search(text, pos, close)
        if match is None:
            # Orphan closer with no opener: keep it verbatim.
            pieces.append(text[pos : close + len(_CLOSE_TAG)])
        else:
            # Drop from the first opener through this closer.
            pieces.append(text[pos : match.start()])
        pos = close + len(_CLOSE_TAG)

    tail = text[pos:]
    match = _OPEN_TAG_RE.search(tail)
    if match is not None:
        # Dangling opener, no closer (truncated footer): drop to EOF.
        tail = tail[: match.start()]
    pieces.append(tail)
    return "".join(pieces).rstrip()
