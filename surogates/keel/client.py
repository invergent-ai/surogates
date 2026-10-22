"""The runtime's client for Keel's harness door.

The worker reports a step session's end to Keel through the door: a
completion, a raised hand, or how a turn ended with neither.  The door also
tells an intake agent which case types it may start, and starts them.

Every request carries the door key as a bearer.  An answer is a
:class:`KeelAnswer`: the status Keel gave, and its decoded JSON body.  A
request that never got an answer has no status, and is never raised.  Nothing
here logs a body: what goes to Keel and what comes back is case data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from surogates.config import Settings

__all__ = ["KeelAnswer", "KeelClient", "keel_client", "use_keel_client"]

logger = logging.getLogger(__name__)

#: How long one request to the door may take.
TIMEOUT_SECONDS = 30.0
#: The longest ``detail`` the door takes.
DETAIL_CHARS = 2000


@dataclass(frozen=True, slots=True)
class KeelAnswer:
    """Keel's answer to one request.

    ``status`` is ``None`` when Keel could not be reached.  ``data`` is the
    decoded body: an object for every route but the intake list, which is a
    list; ``{}`` when there was no JSON body.
    """

    status: int | None
    data: dict | list = field(default_factory=dict)

    @property
    def code(self) -> str | None:
        """The error code Keel gave, if any."""
        code = self.data.get("code") if isinstance(self.data, dict) else None
        return code if isinstance(code, str) else None

    @property
    def message(self) -> str | None:
        """The message Keel gave, if any."""
        message = self.data.get("message") if isinstance(self.data, dict) else None
        return message if isinstance(message, str) else None


def _segment(value: object) -> str:
    return quote(str(value), safe="")


class KeelClient:
    """Calls Keel's door at *url* with the door *key*.

    *transport* replaces the network, for tests.
    """

    def __init__(self, url: str, key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = url.rstrip("/")
        self._key = key
        self._transport = transport

    def __repr__(self) -> str:
        return f"KeelClient(url={self._url!r})"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> KeelAnswer:
        try:
            async with httpx.AsyncClient(
                base_url=self._url, timeout=TIMEOUT_SECONDS, transport=self._transport,
            ) as client:
                response = await client.request(
                    method, path, json=body,
                    headers={"Authorization": f"Bearer {self._key}", **(headers or {})},
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            logger.warning("Keel's door could not be reached for %s %s (%s)", method, path, type(exc).__name__)
            return KeelAnswer(None)
        try:
            data = response.json()
        except ValueError:
            data = {}
        return KeelAnswer(response.status_code, data if isinstance(data, (dict, list)) else {})

    async def complete(self, session_id: object, body: dict) -> KeelAnswer:
        """Report a step session's result: ``{"call_id", "outcome", "output", "sources", "pages_read", "usage"}``."""
        return await self._send("POST", f"/api/harness/sessions/{_segment(session_id)}/complete", body=body)

    async def raise_hand(self, session_id: object, body: dict) -> KeelAnswer:
        """Raise a hand on a step session's step: ``{"call_id", "question", "choices", "evidence", "pages_read", "usage"}``."""
        return await self._send("POST", f"/api/harness/sessions/{_segment(session_id)}/raise-hand", body=body)

    async def ended(
        self,
        session_id: object,
        *,
        reason: str,
        detail: str,
        usage: dict | None = None,
    ) -> KeelAnswer:
        """Tell Keel a step session's turn ended without a report (``no-outcome``) or failed (``failed``).

        Logs, by session id, an end Keel did not take, and never raises: the
        turn ends whatever Keel answers.
        """
        body: dict[str, Any] = {"reason": reason, "detail": detail[:DETAIL_CHARS]}
        if usage is not None:
            body["usage"] = usage
        try:
            answer = await self._send("POST", f"/api/harness/sessions/{_segment(session_id)}/ended", body=body)
        except Exception as exc:
            logger.warning("Keel was not told how session %s ended (%s)", session_id, type(exc).__name__)
            return KeelAnswer(None)
        if answer.status != 200:
            logger.warning(
                "Keel did not take how session %s ended (%s %s)", session_id, answer.status, answer.code or "",
            )
        return answer

    async def intake(self, org_id: object, agent_id: str) -> KeelAnswer:
        """The case types *agent_id* may start in project *org_id*: a list of ``{"case_type", "name", "form", "required"}``."""
        return await self._send(
            "GET", f"/api/harness/projects/{_segment(org_id)}/agents/{_segment(agent_id)}/intake",
        )

    async def start_case(self, org_id: object, body: dict, *, idempotency_key: str) -> KeelAnswer:
        """Start a case in project *org_id*: ``{"agent_id", "session_id", "user_id", "channel", "case_type", "data"}``."""
        return await self._send(
            "POST", f"/api/harness/projects/{_segment(org_id)}/cases",
            body=body, headers={"Idempotency-Key": idempotency_key},
        )


_UNSET: Any = object()
_client: KeelClient | None = _UNSET


def keel_client() -> KeelClient | None:
    """The process's client for Keel's door, built once from the settings.

    ``None`` unless both the door's URL and the door key are set: the
    integration is off.
    """
    global _client
    if _client is _UNSET:
        settings = Settings().keel
        _client = KeelClient(settings.url, settings.door_key) if settings.url and settings.door_key else None
    return _client


def use_keel_client(client: KeelClient | None) -> None:
    """Replace the process's client, for tests: ``None`` turns the integration off."""
    global _client
    _client = client
