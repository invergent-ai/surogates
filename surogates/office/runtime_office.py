"""The worker's client for runtime-office, where the agent's editor sessions run."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

import httpx

from surogates.config import OfficeSettings

logger = logging.getLogger(__name__)

# Opening loads the whole editor in a browser.
OPEN_TIMEOUT_S = 90.0
CALL_TIMEOUT_S = 60.0
# runtime-office's slowest edit: the group's start, the command, the group's end and the check, 20 s each, then the
# flush wait of 10 s.
EDIT_TIMEOUT_S = 120.0


class RuntimeOfficeError(Exception):
    """runtime-office could not do what was asked; status 400 means the request was wrong, in words for the model."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class OfficeBusy(RuntimeOfficeError):
    """runtime-office has as many documents open as it holds."""


class PageGone(RuntimeOfficeError):
    """The agent's editor page is not open any more: open it again."""


def page_id(doc_key: str, session_id: str) -> str:
    """The page runtime-office keeps for one document key in one chat (its ``pageId``)."""
    return f"{doc_key}.{session_id}"


class RuntimeOffice:
    def __init__(self, office: OfficeSettings) -> None:
        self._base = office.runtime_office_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {office.runtime_office_token}"}

    async def open(self, *, doc_key: str, session_id: str, config: dict[str, Any], document_server_url: str) -> str:
        answer = await self._call("/pages", {
            "doc_key": doc_key, "session_id": session_id, "config": config, "document_server_url": document_server_url,
        }, timeout=OPEN_TIMEOUT_S)
        return str(answer["page_id"])

    async def read(self, page: str, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call(f"/pages/{quote(page, safe='')}/read", request)

    async def edit(self, page: str, ops: list[Any]) -> dict[str, Any]:
        return await self._call(f"/pages/{quote(page, safe='')}/edit", {"ops": ops}, timeout=EDIT_TIMEOUT_S, timed_out=(
            "runtime-office did not answer in time; the edit may have applied: read the document again before editing"
        ))

    async def flush(self, page: str) -> bool:
        return bool((await self._call(f"/pages/{quote(page, safe='')}/flush", {}))["flushed"])

    async def close(self, page: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=CALL_TIMEOUT_S) as client:
                response = await client.delete(f"{self._base}/pages/{quote(page, safe='')}", headers=self._headers)
        except httpx.HTTPError as exc:
            raise RuntimeOfficeError(f"runtime-office could not be reached: {exc}") from exc
        if response.status_code not in (204, 404, 410):
            raise RuntimeOfficeError(response.text[:300] or response.reason_phrase, response.status_code)

    async def _call(
        self, path: str, body: dict[str, Any], *, timeout: float = CALL_TIMEOUT_S, timed_out: str | None = None,
    ) -> dict[str, Any]:
        """*timed_out* is the error a timeout raises, where it says more than that runtime-office was not reached."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{self._base}{path}", json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            if timed_out and isinstance(exc, httpx.TimeoutException):
                raise RuntimeOfficeError(timed_out) from exc
            raise RuntimeOfficeError(f"runtime-office could not be reached: {exc}") from exc
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if response.status_code == 200 and isinstance(answer, dict):
            return answer
        said = answer.get("error") if isinstance(answer, dict) else None
        message = str(said or response.text[:300] or response.reason_phrase)
        if response.status_code == 503:
            raise OfficeBusy(message, 503)
        if response.status_code in (404, 410):
            raise PageGone(message, response.status_code)
        raise RuntimeOfficeError(message, response.status_code)


async def close_pages(office: OfficeSettings, doc_key: str, session_ids: Iterable[Any]) -> None:
    """Close the agent's editor pages on a document key that ended (a conflict, the reaper), best effort:
    a dropped page would hold runtime-office's room until it idled out."""
    if not office.agent_editing:
        return
    runtime = RuntimeOffice(office)
    for session_id in session_ids:
        try:
            await runtime.close(page_id(doc_key, str(session_id)))
        except RuntimeOfficeError:
            logger.warning("could not close the agent's page on %s", doc_key, exc_info=True)
