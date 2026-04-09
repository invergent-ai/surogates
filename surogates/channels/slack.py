"""Slack channel adapter (Phase 2).

Architecture overview
---------------------

When implemented this adapter will use `slack-bolt`_ in **Socket Mode** to
receive real-time events from Slack workspaces without exposing a public
HTTP endpoint.  The high-level flow:

1. **Inbound** -- Slack events (``message``, ``app_mention``) arrive via
   Socket Mode.  The adapter normalises them into :class:`MessageEvent`
   objects, resolves the Slack user to an internal user via the
   ``channel_identities`` table, builds a :class:`SessionSource`, and
   routes the message into the appropriate Surogates session.

2. **Outbound** -- Agent responses land in the ``delivery_outbox`` with
   ``channel='slack'``.  A background loop calls
   :meth:`DeliveryService.claim_batch`, converts the payload to Slack
   Block Kit, and posts via ``chat.postMessage``.

3. **Identity resolution** -- Slack user IDs (``U…``) are mapped to
   Surogates ``users.id`` through ``channel_identities(platform='slack',
   platform_user_id=<slack_uid>)``.  Unknown users receive a
   "please register" ephemeral message.

Configuration (via environment / Settings)::

    SUROGATES_SLACK_APP_TOKEN   = xapp-1-…  (Socket Mode token)
    SUROGATES_SLACK_BOT_TOKEN   = xoxb-…    (Bot OAuth token)

.. _slack-bolt: https://slack.dev/bolt-python/
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from surogates.channels.base import ChannelAdapter, SendResult

if TYPE_CHECKING:
    from surogates.channels.delivery import DeliveryService

__all__ = ["SlackAdapter"]

logger = logging.getLogger(__name__)


class SlackAdapter:
    """Slack channel adapter.

    Satisfies the :class:`ChannelAdapter` protocol.  All methods raise
    ``NotImplementedError`` until Phase 2 implementation.
    """

    def __init__(
        self,
        settings: dict[str, Any],
        delivery_service: DeliveryService,
    ) -> None:
        self._settings = settings
        self._delivery = delivery_service

    async def connect(self) -> None:
        """Establish the Socket Mode connection to Slack."""
        raise NotImplementedError(
            "Slack adapter is Phase 2 -- not yet implemented"
        )

    async def disconnect(self) -> None:
        """Gracefully disconnect from Slack."""
        raise NotImplementedError(
            "Slack adapter is Phase 2 -- not yet implemented"
        )

    async def send(
        self,
        target: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a message to a Slack channel or DM."""
        raise NotImplementedError(
            "Slack adapter is Phase 2 -- not yet implemented"
        )

    async def send_typing(self, target: str) -> None:
        """Send a typing indicator in a Slack conversation."""
        raise NotImplementedError(
            "Slack adapter is Phase 2 -- not yet implemented"
        )
