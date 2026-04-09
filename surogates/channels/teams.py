"""Microsoft Teams channel adapter (Phase 2).

Architecture overview
---------------------

When implemented this adapter will use the `Bot Framework SDK`_ to
communicate with Teams via the Azure Bot Service.  The flow:

1. **Inbound** -- Teams sends activity payloads to a webhook endpoint.
   The adapter normalises ``message`` activities into :class:`MessageEvent`
   objects, resolves the Teams ``aadObjectId`` to an internal user via
   ``channel_identities(platform='teams', …)``, and routes into the
   matching Surogates session.

2. **Outbound** -- Agent responses land in ``delivery_outbox`` with
   ``channel='teams'``.  A background loop claims rows, converts them
   to Adaptive Cards, and sends them via the Bot Framework connector.

3. **Identity resolution** -- Teams Azure AD object IDs are mapped to
   Surogates users through the ``channel_identities`` table.

Configuration (via environment / Settings)::

    SUROGATES_TEAMS_APP_ID      = <Azure Bot registration app ID>
    SUROGATES_TEAMS_APP_SECRET  = <Azure Bot registration secret>

.. _Bot Framework SDK: https://learn.microsoft.com/en-us/azure/bot-service/
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from surogates.channels.base import ChannelAdapter, SendResult

if TYPE_CHECKING:
    from surogates.channels.delivery import DeliveryService

__all__ = ["TeamsAdapter"]

logger = logging.getLogger(__name__)


class TeamsAdapter:
    """Microsoft Teams channel adapter.

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
        """Register the bot and start listening for Teams activities."""
        raise NotImplementedError(
            "Teams adapter is Phase 2 -- not yet implemented"
        )

    async def disconnect(self) -> None:
        """Gracefully disconnect from Azure Bot Service."""
        raise NotImplementedError(
            "Teams adapter is Phase 2 -- not yet implemented"
        )

    async def send(
        self,
        target: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a message (or Adaptive Card) to a Teams conversation."""
        raise NotImplementedError(
            "Teams adapter is Phase 2 -- not yet implemented"
        )

    async def send_typing(self, target: str) -> None:
        """Send a typing indicator in a Teams conversation."""
        raise NotImplementedError(
            "Teams adapter is Phase 2 -- not yet implemented"
        )
