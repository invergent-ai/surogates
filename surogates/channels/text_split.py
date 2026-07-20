"""Outbound message splitting shared by channel platforms.

Every messaging platform caps message length (Slack ~40k chars, Telegram
4096).  :func:`split_text` cuts an over-limit reply into chunks at the most
natural boundary available — paragraph break, then line break, then word
boundary, then a hard cut — so no chunk ever exceeds the platform limit and
no content is lost.
"""

from __future__ import annotations

__all__ = ["split_text"]

# Boundary separators in preference order.  Each is searched backwards from
# the limit so the chunk is as full as possible while still ending cleanly.
_SEPARATORS = ("\n\n", "\n", " ")

# Don't accept a boundary that would leave a tiny fragment as the chunk —
# below this fraction of the limit, fall through to the next separator.
_MIN_FILL = 0.25


def split_text(text: str, limit: int) -> list[str]:
    """Split *text* into chunks of at most *limit* characters.

    Returns ``[text]`` unchanged when it already fits.  Chunks are stripped
    of the boundary whitespace they were cut on; empty chunks are never
    returned.  ``limit`` must be positive.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        cut = -1
        for sep in _SEPARATORS:
            idx = window.rfind(sep, 0, limit + 1 if sep != " " else limit + 1)
            # For multi-char separators the cut point is the separator start;
            # for a space it is the space itself.
            if idx >= int(limit * _MIN_FILL):
                cut = idx
                sep_len = len(sep)
                break
        if cut == -1:
            cut = limit
            sep_len = 0
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut + sep_len:].lstrip("\n") if sep_len else remaining[cut:]
    if remaining.strip():
        chunks.append(remaining.rstrip())
    return chunks
