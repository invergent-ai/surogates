"""Async client for the surogates harness HTTP API.

Transport only: no completion semantics, which live in the runner. Lifted
from ``benchmarks/workspace_bench`` with two changes: a session is created
on a declared channel with a config (as surogate-ops creates its Work chat
sessions), and a refused request keeps its HTTP status so the runner can
tell a credential that can never work from a failing task.

Paths are under ``api_prefix``: ``/v1/api`` for a service-account token
(restricted to that prefix), ``/v1`` for a signed-in web user's token,
whose sessions land on the ``web`` channel. The agent is always selected by
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
# Stream bookkeeping the server sends with an id of its own, which are no session event either.
_STREAM_MARKERS = {"stream.live", "stream.start"}

# How long to keep retrying while the platform is transiently unavailable.
_RETRY_WINDOW_S = 300.0


class HarnessError(Exception):
    """Any non-success response from the harness API."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


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
        timeout: float = 60.0,
        retry_window_s: float = _RETRY_WINDOW_S,
        api_prefix: str = "/v1/api",
    ) -> None:
        self._base = base_url.rstrip("/") + "/" + api_prefix.strip("/")
        self._agent_id = agent_id
        self._retry_window_s = retry_window_s
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            # Sent on every request: most /v1/api/sessions/* routes resolve
            # the agent from it; the others ignore it.
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
                f"{what} failed (HTTP {resp.status_code}): {resp.text[:500]}",
                status=resp.status_code,
            )

    async def _retry(self, call):
        """Retry while the platform is transiently unavailable (502/503 or a
        transport error), for up to the retry window."""
        deadline = time.monotonic() + self._retry_window_s
        attempt = 0
        while True:
            try:
                return await call()
            except (HarnessError, httpx.TransportError) as exc:
                transient = isinstance(exc, httpx.TransportError) or (
                    getattr(exc, "status", 0) in (502, 503)
                )
                if not transient or time.monotonic() >= deadline:
                    raise
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, 30))

    async def create_session(self, channel: str, config: dict[str, Any]) -> str:
        """Create a session the way surogate-ops creates a Work chat: the
        declared channel and a config carrying the operator stamp."""

        async def _post() -> httpx.Response:
            resp = await self._http.post(
                f"{self._base}/sessions",
                json={"system": None, "config": config, "channel": channel},
            )
            self._check(resp, "create_session")
            return resp

        resp = await self._retry(_post)
        if resp.status_code == 200:
            # The web route hands back an agent's one kept conversation (its
            # "multi session" capability is off) instead of creating one: a
            # task run in it would see the previous task's chat and files.
            raise HarnessError(
                "create_session returned an existing conversation (HTTP 200): the agent keeps a single "
                "session per user; turn its multi-session capability on to benchmark it",
                status=200,
            )
        return resp.json()["id"]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        async def _get() -> httpx.Response:
            resp = await self._http.get(f"{self._base}/sessions/{session_id}")
            self._check(resp, "get_session")
            return resp

        resp = await self._retry(_get)
        return resp.json()

    async def get_session_status(self, session_id: str) -> str:
        return (await self.get_session(session_id))["status"]

    async def upload_file(
        self, session_id: str, local_path: str, filename: str, subdir: str = ""
    ) -> str:
        """Upload one file into the session workspace, as the file pane's
        upload does. Returns the workspace-relative key."""
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        async def _post() -> httpx.Response:
            with open(local_path, "rb") as fh:
                resp = await self._http.post(
                    f"{self._base}/sessions/{session_id}/workspace/upload",
                    params={"path": subdir} if subdir else None,
                    files={"file": (filename, fh, mime)},
                )
            self._check(resp, f"upload_file {filename}")
            return resp

        resp = await self._retry(_post)
        return resp.json()["path"]

    async def get_workspace_tree(self, session_id: str) -> list[dict[str, Any]]:
        """Flat list of the session's workspace files: {path, size}."""

        async def _get() -> httpx.Response:
            resp = await self._http.get(
                f"{self._base}/sessions/{session_id}/workspace/tree"
            )
            self._check(resp, "get_workspace_tree")
            return resp

        resp = await self._retry(_get)
        files: list[dict[str, Any]] = []

        def walk(entries: list[dict[str, Any]]) -> None:
            for entry in entries:
                if entry.get("kind") == "file":
                    files.append({"path": entry["path"], "size": entry.get("size") or 0})
                walk(entry.get("children") or [])

        walk(resp.json().get("entries") or [])
        return files

    async def download_file(self, session_id: str, path: str) -> bytes:
        async def _get() -> httpx.Response:
            resp = await self._http.get(
                f"{self._base}/sessions/{session_id}/workspace/download",
                params={"path": path},
            )
            self._check(resp, f"download_file {path}")
            return resp

        resp = await self._retry(_get)
        return resp.content

    async def delete_session(self, session_id: str) -> None:
        """Archive the session and delete its workspace, as deleting a chat does."""

        async def _delete() -> httpx.Response:
            resp = await self._http.delete(f"{self._base}/sessions/{session_id}")
            self._check(resp, "delete_session")
            return resp

        await self._retry(_delete)

    async def send_message(self, session_id: str, content: str) -> int:
        """Post the user's message. A non-2xx raises: a session left with no
        user.message is swept later as a recovery_loop failure, which looks
        exactly like a harness hang."""

        async def _post() -> httpx.Response:
            resp = await self._http.post(
                f"{self._base}/sessions/{session_id}/messages",
                json={"content": content},
            )
            self._check(resp, "send_message")
            return resp

        resp = await self._retry(_post)
        return resp.json()["event_id"]

    async def stream_events(self, session_id: str, after: int = 0) -> AsyncIterator[Event]:
        """Yield session events until the server ends the stream (session.done
        or stream.timeout; the server closes every stream after 300 s, so the
        runner reconnects from the last seen id)."""
        url = f"{self._base}/sessions/{session_id}/events"
        async with aconnect_sse(self._http, "GET", url, params={"after": after}) as source:
            async for sse in source.aiter_sse():
                if sse.event in _STREAM_END_EVENTS:
                    return
                if not sse.id or sse.event in _STREAM_MARKERS or (sse.event == "message" and not sse.data):
                    # A keepalive comment, or the stream's own markers. Per the SSE spec the last id is sticky, so a
                    # keepalive after a numbered event decodes as an empty "message" carrying that id: no event.
                    continue
                try:
                    data = json.loads(sse.data) if sse.data else {}
                except json.JSONDecodeError:
                    data = {"_raw": sse.data}
                yield Event(id=int(sse.id), type=sse.event, data=data)
