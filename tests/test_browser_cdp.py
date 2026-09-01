"""Tests for surogates.browser.cdp.CdpClient."""

from __future__ import annotations

import asyncio
import json

import pytest

from surogates.browser.cdp import CdpClient


class FakeSocket:
    """Stands in for a websockets connection: records sends, replays scripted frames."""

    def __init__(self, replies: dict[str, dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._replies = replies or {}
        self._inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        canned = self._replies.get(msg["method"])
        if canned is not None:
            await self._inbox.put(json.dumps({"id": msg["id"], "result": canned}))

    async def recv(self) -> str:
        return await self._inbox.get()

    async def push(self, payload: dict) -> None:
        await self._inbox.put(json.dumps(payload))

    async def close(self) -> None:
        return None


async def test_call_correlates_reply_by_id() -> None:
    sock = FakeSocket({"Browser.getVersion": {"product": "Chrome/147"}})
    async with CdpClient(sock) as cdp:
        result = await cdp.call("Browser.getVersion")
    assert result == {"product": "Chrome/147"}
    assert sock.sent[0]["method"] == "Browser.getVersion"


async def test_page_commands_carry_the_session_id() -> None:
    # Without sessionId every Page.* call returns "'Page.enable' wasn't found",
    # because devtoolsproxy fronts 9222 and the page path behaves like a
    # browser session. The session id is not optional decoration.
    sock = FakeSocket({"Page.enable": {}})
    async with CdpClient(sock) as cdp:
        await cdp.call("Page.enable", session="SESSION123")
    assert sock.sent[0]["sessionId"] == "SESSION123"


async def test_browser_level_call_omits_the_session_id() -> None:
    sock = FakeSocket({"Target.getTargets": {"targetInfos": []}})
    async with CdpClient(sock) as cdp:
        await cdp.call("Target.getTargets")
    assert "sessionId" not in sock.sent[0]


async def test_call_raises_on_protocol_error() -> None:
    sock = FakeSocket()
    async with CdpClient(sock) as cdp:
        task = asyncio.create_task(cdp.call("Page.enable", timeout=2))
        await asyncio.sleep(0)
        await sock.push({
            "id": 1,
            "error": {"code": -32601, "message": "'Page.enable' wasn't found"},
        })
        with pytest.raises(RuntimeError, match="wasn't found"):
            await task


async def test_events_reach_registered_handlers() -> None:
    sock = FakeSocket()
    seen: list[dict] = []
    async with CdpClient(sock) as cdp:
        cdp.on("Page.screencastFrame", seen.append)
        await sock.push({
            "method": "Page.screencastFrame",
            "params": {"data": "AAA", "sessionId": "s1"},
        })
        await asyncio.sleep(0.05)
    assert seen and seen[0]["data"] == "AAA"


async def test_a_pending_call_survives_interleaved_events() -> None:
    # The pump must not mistake an event for a reply, or a screencast frame
    # arriving mid-call resolves the wrong future.
    sock = FakeSocket()
    async with CdpClient(sock) as cdp:
        task = asyncio.create_task(cdp.call("Page.enable", timeout=2))
        await asyncio.sleep(0)
        await sock.push({"method": "Page.loadEventFired", "params": {}})
        await sock.push({"id": 1, "result": {"ok": True}})
        assert await task == {"ok": True}


async def test_one_failing_handler_does_not_stop_the_pump() -> None:
    # Screencast frames arrive continuously; a handler that throws once must
    # not take the connection down with it.
    sock = FakeSocket()
    seen: list[dict] = []

    def boom(_params: dict) -> None:
        raise ValueError("handler blew up")

    async with CdpClient(sock) as cdp:
        cdp.on("Page.screencastFrame", boom)
        cdp.on("Page.screencastFrame", seen.append)
        await sock.push({"method": "Page.screencastFrame", "params": {"data": "A"}})
        await sock.push({"method": "Page.screencastFrame", "params": {"data": "B"}})
        await asyncio.sleep(0.05)
    assert [f["data"] for f in seen] == ["A", "B"]


async def test_targets_returns_only_page_targets() -> None:
    sock = FakeSocket({
        "Target.getTargets": {
            "targetInfos": [
                {"targetId": "t1", "type": "page", "url": "https://a.test/"},
                {"targetId": "t2", "type": "service_worker", "url": "sw.js"},
                {"targetId": "t3", "type": "page", "url": "https://b.test/"},
            ]
        }
    })
    async with CdpClient(sock) as cdp:
        pages = await cdp.targets()
    assert [t["targetId"] for t in pages] == ["t1", "t3"]


async def test_attach_page_returns_the_flat_session_id() -> None:
    sock = FakeSocket({"Target.attachToTarget": {"sessionId": "FLAT1"}})
    async with CdpClient(sock) as cdp:
        session = await cdp.attach_page("t1")
    assert session == "FLAT1"
    # flatten:true is what makes the session usable for Page.* commands.
    assert sock.sent[0]["params"] == {"targetId": "t1", "flatten": True}


async def test_call_times_out_when_no_reply_arrives() -> None:
    sock = FakeSocket()
    async with CdpClient(sock) as cdp:
        with pytest.raises(asyncio.TimeoutError):
            await cdp.call("Page.enable", timeout=0.05)
