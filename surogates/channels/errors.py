"""Platform-neutral channel API error signal.

Channel platform adapters raise :class:`ChannelApiError` to tell the fetch
layer *why* an API call failed, so it can return a precise status instead of
collapsing every failure into "not found".
"""

from __future__ import annotations


class ChannelApiError(Exception):
    """A channel-platform API call failed.

    ``reason`` is a stable token the fetch layer maps to a precise result:
    ``"forbidden"`` (the bot cannot access the resource), ``"rate_limited"``
    (back off and retry), or ``"unavailable"`` (transient/unknown failure).
    """

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)
