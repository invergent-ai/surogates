"""Meta's webhook, as a rehearsal world sends it to the channels service."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def envelope(phone_number_id: str, value: dict[str, Any]) -> dict[str, Any]:
    return {"object": "whatsapp_business_account", "entry": [{
        "id": phone_number_id,
        "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": phone_number_id, "phone_number_id": phone_number_id},
            **value,
        }}],
    }]}


async def post_webhook(
    client: httpx.AsyncClient, *, channels_url: str, phone_number_id: str,
    app_secret: str, value: dict[str, Any],
) -> int:
    """POST one change to ``/whatsapp/{phone_number_id}``; returns the HTTP status (0 on failure)."""
    raw = json.dumps(envelope(phone_number_id, value)).encode()
    signature = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    try:
        response = await client.post(
            f"{channels_url.rstrip('/')}/whatsapp/{phone_number_id}", content=raw,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={signature}"},
        )
    except httpx.HTTPError as exc:
        logger.warning("[rehearsal] webhook to channels failed (%s)", exc)
        return 0
    return response.status_code


async def post_telegram_update(
    client: httpx.AsyncClient, *, channels_url: str, bot_username: str,
    webhook_secret: str, update: dict[str, Any],
) -> int:
    """POST one Update to ``/telegram/{username}`` as Telegram does; returns the HTTP status (0 on failure)."""
    try:
        response = await client.post(
            f"{channels_url.rstrip('/')}/telegram/{bot_username}", json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret},
        )
    except httpx.HTTPError as exc:
        logger.warning("[rehearsal] telegram update to channels failed (%s)", exc)
        return 0
    return response.status_code


async def post_slack(
    client: httpx.AsyncClient, *, channels_url: str, app_id: str, signing_secret: str,
    path: str, raw: bytes, content_type: str,
) -> int:
    """POST to ``/slack/{app_id}{path}`` signed as Slack signs; returns the HTTP status (0 on failure)."""
    ts = str(int(time.time()))
    signature = "v0=" + hmac.new(signing_secret.encode(), b"v0:" + ts.encode() + b":" + raw,
                                 hashlib.sha256).hexdigest()
    try:
        response = await client.post(
            f"{channels_url.rstrip('/')}/slack/{app_id}{path}", content=raw,
            headers={"Content-Type": content_type, "X-Slack-Request-Timestamp": ts,
                     "X-Slack-Signature": signature},
        )
    except httpx.HTTPError as exc:
        logger.warning("[rehearsal] slack delivery to channels failed (%s)", exc)
        return 0
    return response.status_code
