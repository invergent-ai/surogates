"""Commands to the document server (its Command Service)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from surogates.config import OfficeSettings
from surogates.office.tokens import sign

logger = logging.getLogger(__name__)


# The Command Service's error for a key it has no document by.
UNKNOWN_KEY = 1


async def _command(office: OfficeSettings, command: dict[str, Any], *, quiet: bool = False) -> dict[str, Any] | None:
    """The Command Service's answer to *command*; None when it could not be asked (logged, unless *quiet*: the caller
    says so itself)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{office.internal_url.rstrip('/')}/command",
                json={**command, "token": sign(office.jwt_secret, command)},
            )
            response.raise_for_status()
            answer = response.json()
    except (httpx.HTTPError, ValueError):
        if not quiet:
            logger.warning("the document server did not answer %s for %s", command["c"], command["key"], exc_info=True)
        return None
    if not isinstance(answer, dict):
        if not quiet:
            logger.warning("the document server answered %s for %s with %r", command["c"], command["key"], answer)
        return None
    return answer


async def drop_editors(office: OfficeSettings, doc_key: str, users: list[str]) -> None:
    """Disconnect *users* from the document, so their editors reload it."""
    if not users:
        return
    answer = await _command(office, {"c": "drop", "key": doc_key, "users": users})
    # The Command Service answers 200 even when it refuses: the refusal is in the body.
    if answer is not None and answer.get("error"):
        logger.warning("the document server refused to drop the editors of %s (error %s)", doc_key, answer["error"])


async def force_save(office: OfficeSettings, doc_key: str, userdata: str) -> int | None:
    """Ask the document server to save the document now, its save callback carrying *userdata*.

    Its error code: 0 a save is coming, 4 there was nothing unsaved; None when it could not be asked.
    """
    answer = await _command(office, {"c": "forcesave", "key": doc_key, "userdata": userdata})
    if answer is None:
        return None
    try:
        return int(answer.get("error", -1))
    except (TypeError, ValueError):
        return None


async def document_info(office: OfficeSettings, doc_key: str) -> int | None:
    """Ask the document server whether it has the document open.

    Its error code: 0 it has the key, and calls back with who is in the document; :data:`UNKNOWN_KEY` it has no
    document by that key; None when it could not be asked.  Nothing is logged: the caller says what it makes of it.
    """
    answer = await _command(office, {"c": "info", "key": doc_key}, quiet=True)
    if answer is None:
        return None
    try:
        return int(answer.get("error", -1))
    except (TypeError, ValueError):
        return None
