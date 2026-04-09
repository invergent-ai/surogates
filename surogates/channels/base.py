"""Abstract channel adapter protocol and shared message types.

Every external messaging platform (Slack, Teams, Telegram, webhooks) is
represented by an adapter that conforms to :class:`ChannelAdapter`.  The web
channel (REST API + SSE) does not need an adapter because the FastAPI routes
*are* the channel -- but the same :class:`MessageEvent` and
:class:`SendResult` types are used to normalise data flowing through the
system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from surogates.channels.source import SessionSource

__all__ = [
    "ChannelAdapter",
    "MessageEvent",
    "MessageType",
    "SendResult",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class MessageType(str, Enum):
    """Content type for an inbound or outbound message."""

    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    COMMAND = "command"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MessageEvent:
    """Normalised inbound message from any platform.

    Every channel adapter converts its platform-native payload into a
    ``MessageEvent`` before handing it to the session layer.  This keeps
    business logic platform-agnostic.
    """

    source: SessionSource
    content: str
    message_type: MessageType = MessageType.TEXT
    media_urls: list[str] = field(default_factory=list)
    reply_to_message_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    """Outcome of sending a message through a channel adapter."""

    success: bool
    message_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ChannelAdapter(Protocol):
    """Protocol that all messaging-channel adapters must satisfy.

    The web channel (REST API + SSE) does **not** implement this protocol
    because it *is* the API -- there is nothing to adapt.  This protocol
    exists for external messaging platforms that require a persistent
    connection (socket mode, long polling, etc.).
    """

    async def connect(self) -> None:
        """Establish the connection to the external platform.

        Implementations should be idempotent: calling ``connect`` on an
        already-connected adapter is a no-op.
        """
        ...

    async def disconnect(self) -> None:
        """Gracefully tear down the platform connection.

        After ``disconnect`` returns, no further events will be received and
        no outbound messages can be sent.
        """
        ...

    async def send(
        self,
        target: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a message to *target* (a platform-specific chat/channel ID).

        Parameters
        ----------
        target:
            Platform-specific identifier for the conversation to post into.
        content:
            The message body (plain text or platform-native markup).
        reply_to:
            If set, the platform message ID to reply to (threading).
        metadata:
            Adapter-specific extras (e.g. Slack ``blocks``, Teams cards).
        """
        ...

    async def send_typing(self, target: str) -> None:
        """Send a typing / "agent is working" indicator to *target*.

        Best-effort: implementations may silently swallow errors if the
        platform does not support typing indicators.
        """
        ...
