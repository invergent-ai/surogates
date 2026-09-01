"""Minimal Chrome DevTools Protocol client.

The rest of the repo reaches CDP only through Playwright, by posting JavaScript
to the browser pod's ``/playwright/execute``. The browser shell has to speak it
directly: it needs a flat session, screencast events, and input dispatch on a
connection held open for the life of a viewer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class _Socket(Protocol):
    """The slice of a websocket connection this client uses."""

    async def send(self, raw: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


class CdpClient:
    """One CDP connection, with id-correlated calls and event fan-out."""

    def __init__(self, socket: _Socket) -> None:
        self._socket = socket
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[str, list[Callable[[dict], None]]] = {}
        self._pump: asyncio.Task | None = None

    async def __aenter__(self) -> "CdpClient":
        self._pump = asyncio.create_task(self._read_loop())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        await self._socket.close()

    def on(self, method: str, handler: Callable[[dict], None]) -> None:
        """Register a handler for one event method. It receives ``params``."""

        self._handlers.setdefault(method, []).append(handler)

    async def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        session: str | None = None,
        timeout: float = 10.0,
    ) -> dict:
        """Send one command and await its reply.

        Raises ``RuntimeError`` on a protocol error and ``asyncio.TimeoutError``
        when no reply arrives, so a stalled call cannot wedge a viewer forever.
        """

        self._next_id += 1
        message_id = self._next_id
        message: dict[str, Any] = {
            "id": message_id,
            "method": method,
            "params": params or {},
        }
        # Page, Runtime and Input are unreachable without this: the
        # devtoolsproxy in front of :9222 makes the /devtools/page/<id> path
        # behave like a browser session, so an unsessioned Page.enable comes
        # back "'Page.enable' wasn't found" rather than working.
        if session:
            message["sessionId"] = session

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self._socket.send(json.dumps(message))
        try:
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(message_id, None)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message', '?')}")
        return reply.get("result", {})

    async def targets(self) -> list[dict]:
        """Return the page targets, ignoring workers, iframes and extensions."""

        result = await self.call("Target.getTargets")
        return [t for t in result.get("targetInfos", []) if t.get("type") == "page"]

    async def attach_page(self, target_id: str) -> str:
        """Attach to a page target and return its flat session id."""

        result = await self.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        return result["sessionId"]

    async def _read_loop(self) -> None:
        while True:
            try:
                payload = json.loads(await self._socket.recv())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dead or noisy socket ends the loop
                return
            message_id = payload.get("id")
            if message_id is not None:
                future = self._pending.pop(message_id, None)
                if future is not None and not future.done():
                    future.set_result(payload)
                continue
            for handler in self._handlers.get(payload.get("method", ""), ()):
                # Screencast frames arrive continuously; one handler raising
                # must not take the connection down with it.
                try:
                    handler(payload.get("params", {}))
                except Exception:  # noqa: BLE001 - logged, never fatal
                    logger.exception("cdp event handler failed")
