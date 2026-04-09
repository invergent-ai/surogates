"""Telegram channel adapter (Phase 2).

Architecture overview
---------------------

When implemented this adapter will use the `Telegram Bot API`_ via long
polling (``getUpdates``) or a webhook to receive messages.  The flow:

1. **Inbound** -- Telegram ``Update`` objects arrive via long polling or
   webhook.  The adapter normalises ``message`` / ``callback_query``
   payloads into :class:`MessageEvent` objects, resolves the Telegram
   user ID to an internal user via ``channel_identities(platform=
   'telegram', …)``, and routes into the matching Surogates session.

2. **Outbound** -- Agent responses land in ``delivery_outbox`` with
   ``channel='telegram'``.  A background loop claims rows, formats them
   as Telegram ``sendMessage`` / ``sendPhoto`` payloads, and delivers via
   the Bot API.

3. **Identity resolution** -- Telegram numeric user IDs are mapped to
   Surogates users through the ``channel_identities`` table.

Configuration (via environment / Settings)::

    SUROGATES_TELEGRAM_BOT_TOKEN = <BotFather token>
    SUROGATES_TELEGRAM_WEBHOOK_URL = <optional, for webhook mode>

.. _Telegram Bot API: https://core.telegram.org/bots/api
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from surogates.channels.base import ChannelAdapter, SendResult

if TYPE_CHECKING:
    from surogates.channels.delivery import DeliveryService

__all__ = ["TelegramAdapter"]

logger = logging.getLogger(__name__)


class TelegramAdapter:
    """Telegram channel adapter.

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
        """Start long-polling or register the webhook with Telegram."""
        raise NotImplementedError(
            "Telegram adapter is Phase 2 -- not yet implemented"
        )

    async def disconnect(self) -> None:
        """Stop polling and deregister the webhook."""
        raise NotImplementedError(
            "Telegram adapter is Phase 2 -- not yet implemented"
        )

    async def send(
        self,
        target: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a message to a Telegram chat."""
        raise NotImplementedError(
            "Telegram adapter is Phase 2 -- not yet implemented"
        )

    async def send_typing(self, target: str) -> None:
        """Send a ``sendChatAction(typing)`` indicator."""
        raise NotImplementedError(
            "Telegram adapter is Phase 2 -- not yet implemented"
        )
