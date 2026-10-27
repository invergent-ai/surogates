"""A CDP connection that dies says so at once, to its callers and to whoever waits on it."""

from __future__ import annotations

import asyncio

import pytest

from surogates.browser.cdp import CdpClient


class DyingSocket:
    def __init__(self) -> None:
        self.dead = asyncio.Event()

    async def send(self, raw: str) -> None:
        return None

    async def recv(self) -> str:
        await self.dead.wait()
        raise ConnectionError("connection closed")

    async def close(self) -> None:
        return None


async def test_a_call_in_flight_fails_when_the_socket_dies_not_at_its_timeout() -> None:
    socket = DyingSocket()
    async with CdpClient(socket) as cdp:
        call = asyncio.ensure_future(cdp.call("Page.enable", timeout=30.0))
        await asyncio.sleep(0)
        socket.dead.set()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(call, 1.0)
        await asyncio.wait_for(cdp.closed.wait(), 1.0)
        with pytest.raises(ConnectionError):
            await cdp.call("Page.enable")
