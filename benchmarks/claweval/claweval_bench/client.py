"""Async client for the surogates harness HTTP API.

A port of ``gaia_bench.client`` (kept dependency-free of it: each benchmark
stays self-contained). Transport only -- no retry policy beyond transient
platform blips, no completion semantics; those live in the runner.

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

# Native tools the agent must NOT have for a faithful claw-eval run.
# Upstream claw-eval hands the agent ONLY the task's declared tools; here
# the agent under test carries its own toolset, and its native web /
# deep-research tools let it answer from the open internet instead of the
# task's mock services -- the graders score against the mock-service audit
# logs, so a bypassed task scores ~0 for reasons that are the agent's tool
# config, not the harness. Excluding them at the session level (honored by
# the harness as ``config.excluded_tools``, stripped from the model-visible
# schema) makes the tool universe match upstream's. Browser tools are left
# to the agent's "Live browser" capability toggle.
DEFAULT_EXCLUDED_TOOLS = (
    "web_search",
    "web_extract",
    "web_crawl",
    "research_memory",
    "research_outline",
)


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
        excluded_tools: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._agent_id = agent_id
        self._retry_window_s = retry_window_s
        # None -> the faithful default; an empty list -> exclude nothing
        # (explicit opt-out for measuring the agent's own toolset).
        self._excluded_tools = list(
            DEFAULT_EXCLUDED_TOOLS if excluded_tools is None else excluded_tools
        )
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            # Sent on EVERY request, not just create_session: most
            # /v1/api/sessions/* routes resolve the agent through
            # agent_runtime_context_dep and reject the call with 400
            # without it; routes that do not resolve an agent ignore it.
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

        A paused server returns 503 immediately, so a whole run burns
        through at full speed instead of waiting. Transport errors (a hung
        request hitting the read timeout) are the same class. Roughly five
        minutes of tolerance; longer outages still fail the task.
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
        config: dict[str, Any] = {}
        if self._excluded_tools:
            config["excluded_tools"] = self._excluded_tools
        resp = await self._http.post(
            f"{self._base}/v1/api/sessions",
            json={"system": None, "config": config},
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
