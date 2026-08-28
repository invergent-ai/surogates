"""Async client for the surogates harness HTTP API.

Transport only -- no retry policy, no completion semantics. Those live in
the runner, so this stays trivially testable against a mock server.

Every path is under /v1/api/*: service-account tokens are hard-restricted
to that prefix and get 403 anywhere else. The agent is always selected by
the ?agent_id= query parameter, the highest-precedence resolution in the
harness.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from httpx_sse import aconnect_sse

# The stream closes itself at these events; neither is a session event.
_STREAM_END_EVENTS = {"session.done", "stream.timeout"}

# How long to keep retrying while the platform is transiently unavailable.
_RETRY_WINDOW_S = 300.0


class HarnessError(Exception):
    """Any non-success response from the harness API."""


@dataclass(frozen=True)
class Event:
    id: int
    type: str
    data: dict[str, Any]


class HarnessClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        agent_id: str,
        timeout: float = 30.0,
        retry_window_s: float = _RETRY_WINDOW_S,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._agent_id = agent_id
        self._retry_window_s = retry_window_s
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            # Sent on EVERY request, not just create_session. Most
            # /v1/api/sessions/* routes resolve the agent through
            # agent_runtime_context_dep and reject the call with
            # 400 "no agent_id in request" without it. Setting it at the
            # client level means no method can forget it; routes that do
            # not resolve an agent (workspace upload, SSE) ignore it.
            params={"agent_id": agent_id},
            timeout=httpx.Timeout(timeout, connect=10.0),
            follow_redirects=True,
        )

    async def __aenter__(self) -> "HarnessClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    def _check(self, resp: httpx.Response, what: str) -> None:
        if resp.status_code >= 400:
            raise HarnessError(
                f"{what} failed (HTTP {resp.status_code}): {resp.text[:500]}"
            )

    async def _retry(self, call, what: str):
        """Retry a request while the platform is transiently unavailable.

        A paused ops server makes the harness return 503 immediately, so a
        whole split burns through at full speed instead of waiting -- one
        debugger breakpoint voids a 70-minute run. Transport errors (a hung
        request hitting the read timeout) are the same class.

        ponytail: ~5 min of tolerance; longer pauses still fail the task.
        """
        deadline = time.monotonic() + self._retry_window_s
        attempt = 0
        while True:
            try:
                return await call()
            except (HarnessError, httpx.TransportError) as exc:
                transient = isinstance(exc, httpx.TransportError) or (
                    "HTTP 503" in str(exc) or "HTTP 502" in str(exc)
                )
                if not transient or time.monotonic() >= deadline:
                    raise
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, 30))

    async def create_session(self) -> str:
        return await self._retry(self._create_session, "create_session")

    async def _create_session(self) -> str:
        resp = await self._http.post(
            f"{self._base}/v1/api/sessions",
            json={"system": None, "config": {}},
        )
        self._check(resp, "create_session")
        return resp.json()["id"]

    async def upload_file(
        self, session_id: str, local_path: str, filename: str
    ) -> str:
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(local_path, "rb") as fh:
            resp = await self._http.post(
                f"{self._base}/v1/api/sessions/{session_id}/workspace/upload",
                files={"file": (filename, fh, mime)},
            )
        self._check(resp, "upload_file")
        return resp.json()["path"]

    async def send_message(
        self,
        session_id: str,
        content: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> int:
        body: dict[str, Any] = {"content": content}
        if attachments:
            body["attachments"] = attachments
        async def _post():
            resp = await self._http.post(
                f"{self._base}/v1/api/sessions/{session_id}/messages", json=body
            )
            self._check(resp, "send_message")
            return resp

        resp = await self._retry(_post, "send_message")
        return resp.json()["event_id"]

    async def get_session_status(self, session_id: str) -> str:
        async def _get():
            resp = await self._http.get(
                f"{self._base}/v1/api/sessions/{session_id}"
            )
            self._check(resp, "get_session_status")
            return resp

        resp = await self._retry(_get, "get_session_status")
        return resp.json()["status"]

    async def stream_events(
        self, session_id: str, after: int = 0
    ) -> AsyncIterator[Event]:
        """Yield session events until the server ends the stream.

        Terminates on session.done or stream.timeout. The server closes
        every stream after 300s regardless of session state, so callers
        must reconnect with the last seen event id -- see the runner.
        """
        url = f"{self._base}/v1/api/sessions/{session_id}/events"
        async with aconnect_sse(
            self._http, "GET", url, params={"after": after}
        ) as source:
            async for sse in source.aiter_sse():
                if sse.event in _STREAM_END_EVENTS:
                    return
                if not sse.id:
                    continue  # keepalive comment or unnumbered frame
                try:
                    data = json.loads(sse.data) if sse.data else {}
                except json.JSONDecodeError:
                    data = {"_raw": sse.data}
                yield Event(id=int(sse.id), type=sse.event, data=data)
