"""Generic webhook channel adapter (Phase 2).

Architecture overview
---------------------

The webhook adapter provides a platform-agnostic integration point for
custom applications.  Unlike Slack/Teams/Telegram, there is no
vendor-specific SDK -- the adapter exposes a simple HTTP contract:

1. **Inbound** -- External systems POST a JSON payload to
   ``POST /v1/webhooks/{webhook_id}/messages``.  The adapter validates
   the payload against a shared secret (HMAC-SHA256), normalises it into
   a :class:`MessageEvent`, and routes it into the matching session.

2. **Outbound** -- Agent responses land in ``delivery_outbox`` with
   ``channel='webhook'``.  A background loop claims rows and POSTs
   them to the registered callback URL with HMAC signatures.

3. **Registration** -- Webhook endpoints are configured per-org via
   the admin API: ``POST /v1/admin/webhooks`` with a ``callback_url``
   and ``secret``.

Configuration::

    Webhook registrations live in the database (a future ``webhooks``
    table), not in environment variables.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from surogates.channels.base import ChannelAdapter, SendResult

if TYPE_CHECKING:
    from surogates.channels.delivery import DeliveryService

__all__ = ["WebhookAdapter"]

logger = logging.getLogger(__name__)


class WebhookAdapter:
    """Generic webhook channel adapter.

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
        """Start the outbound delivery loop for webhook destinations."""
        raise NotImplementedError(
            "Webhook adapter is Phase 2 -- not yet implemented"
        )

    async def disconnect(self) -> None:
        """Stop the delivery loop and drain pending callbacks."""
        raise NotImplementedError(
            "Webhook adapter is Phase 2 -- not yet implemented"
        )

    async def send(
        self,
        target: str,
        content: str,
        *,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """POST a signed JSON payload to the registered callback URL."""
        raise NotImplementedError(
            "Webhook adapter is Phase 2 -- not yet implemented"
        )

    async def send_typing(self, target: str) -> None:
        """No-op -- webhooks do not support typing indicators."""
        raise NotImplementedError(
            "Webhook adapter is Phase 2 -- not yet implemented"
        )
