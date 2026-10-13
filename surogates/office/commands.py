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
    except httpx.HTTPError:
        logger.warning("could not drop the editors of %s", doc_key, exc_info=True)
