"""Commands to the document server (its Command Service)."""

from __future__ import annotations

import logging

import httpx

from surogates.config import OfficeSettings
from surogates.office.tokens import sign

logger = logging.getLogger(__name__)


async def drop_editors(office: OfficeSettings, doc_key: str, users: list[str]) -> None:
    """Disconnect *users* from the document, so their editors reload it."""
    if not users:
        return
    command = {"c": "drop", "key": doc_key, "users": users}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{office.internal_url.rstrip('/')}/command",
                json={**command, "token": sign(office.jwt_secret, command)},
            )
            response.raise_for_status()
            error = response.json().get("error")
    except (httpx.HTTPError, ValueError):
        logger.warning("could not drop the editors of %s", doc_key, exc_info=True)
        return
    # The Command Service answers 200 even when it refuses: the refusal is in the body.
    if error:
        logger.warning("the document server refused to drop the editors of %s (error %s)", doc_key, error)
