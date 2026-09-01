"""Tests for surogates.browser.shell.ShellSession — the pump and the lease gate."""

from __future__ import annotations

import asyncio
import base64
import json

from surogates.browser.shell import ShellSession


class FakeCdp:
    """Records calls and lets a test push events at registered handlers."""

    def __init__(self, results: dict[str, dict] | None = None) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self._results = results or {}
        self._handlers: dict[str, list] = {}

    async def call(self, method, params=None, *, session=None, timeout=10.0):
        self.calls.append((method, params or {}, session))
        return self._results.get(method, {})

    def on(self, method, handler) -> None:
        self._handlers.setdefault(method, []).append(handler)

    async def targets(self):
        return self._results.get("__targets__", [{"targetId": "t1", "url": "u"}])

    async def attach_page(self, target_id):
        self.calls.append(("Target.attachToTarget", {"targetId": target_id}, None))
        return f"session-{target_id}"

    def emit(self, method: str, params: dict) -> None:
        for handler in self._handlers.get(method, ()):
            handler(params)

    def methods(self) -> list[str]:
        return [method for method, _p, _s in self.calls]


class FakeClient:
    def __init__(self) -> None:
        self.binary: list[bytes] = []
        self.text: list[dict] = []

    async def send_bytes(self, payload: bytes) -> None:
        self.binary.append(payload)

    async def send_text(self, payload: str) -> None:
        self.text.append(json.loads(payload))


def _layout(width: int = 1000, height: int = 500) -> dict:
    return {"cssVisualViewport": {"clientWidth": width, "clientHeight": height}}


async def _session(*, lease: bool = True, cdp: FakeCdp | None = None):
    cdp = cdp or FakeCdp({"Page.getLayoutMetrics": _layout()})
    client = FakeClient()

    async def lease_held() -> bool:
        return lease

    session = ShellSession(cdp, client, lease_held=lease_held)
    await session.start()
    return session, cdp, client


class TestLeaseGate:
    async def test_command_without_the_lease_is_dropped_not_forwarded(self) -> None:
        session, cdp, _client = await _session(lease=False)
        before = len(cdp.calls)
        await session.handle(json.dumps({"t": "click", "x": 0.5, "y": 0.5}))
        # Dropped, not raised, and nothing reached the browser.
        assert cdp.calls[before:] == []
        await session.close()

    async def test_command_with_the_lease_reaches_the_browser(self) -> None:
        session, cdp, _client = await _session(lease=True)
        await session.handle(json.dumps({"t": "click", "x": 0.5, "y": 0.5}))
        mouse = [c for c in cdp.calls if c[0] == "Input.dispatchMouseEvent"]
        # Press and release, both scaled against the 1000x500 viewport.
        assert [c[1]["type"] for c in mouse] == ["mousePressed", "mouseReleased"]
        assert mouse[0][1]["x"] == 500 and mouse[0][1]["y"] == 250
        await session.close()

    async def test_switch_tab_works_without_the_lease(self) -> None:
        # Changing what you watch is not acting on the page.
        session, cdp, _client = await _session(lease=False)
        await session.handle(json.dumps({"t": "switch_tab", "id": "t2"}))
        assert ("Target.attachToTarget", {"targetId": "t2"}, None) in cdp.calls
        await session.close()


class TestFrames:
    async def test_frames_are_decoded_forwarded_and_acked(self) -> None:
        session, cdp, client = await _session()
        cdp.emit(
            "Page.screencastFrame",
            {"data": base64.b64encode(b"JPEGBYTES").decode(), "sessionId": "sc1"},
        )
        await asyncio.sleep(0.05)
        assert client.binary == [b"JPEGBYTES"]
        # An unacked stream stalls, so the ack is not optional.
        assert "Page.screencastFrameAck" in cdp.methods()
        await session.close()

    async def test_frames_flow_without_the_lease(self) -> None:
        # Watching is never gated; only the command half is.
        session, cdp, client = await _session(lease=False)
        cdp.emit(
            "Page.screencastFrame",
            {"data": base64.b64encode(b"XY").decode(), "sessionId": "sc1"},
        )
        await asyncio.sleep(0.05)
        assert client.binary == [b"XY"]
        await session.close()

    async def test_frames_are_forwarded_in_order(self) -> None:
        session, cdp, client = await _session()
        for payload in (b"one", b"two", b"three"):
            cdp.emit(
                "Page.screencastFrame",
                {"data": base64.b64encode(payload).decode(), "sessionId": "s"},
            )
        await asyncio.sleep(0.05)
        # An out-of-order frame paints a stale image.
        assert client.binary == [b"one", b"two", b"three"]
        await session.close()


class TestStartupOrder:
    async def test_screencast_starts_before_anything_navigates(self) -> None:
        session, cdp, _client = await _session()
        methods = cdp.methods()
        assert "Page.startScreencast" in methods
        assert methods.index("Page.enable") < methods.index("Page.startScreencast")
        # Page.startScreencast is refused with "Not attached to an active page"
        # while a navigation is in flight, so start() must never navigate.
        assert "Page.navigate" not in methods
        await session.close()

    async def test_switch_tab_stops_the_old_stream_before_starting_the_new(
        self,
    ) -> None:
        session, cdp, _client = await _session()
        cdp.calls.clear()
        await session.handle(json.dumps({"t": "switch_tab", "id": "t2"}))
        methods = cdp.methods()
        assert methods.index("Page.stopScreencast") < methods.index(
            "Page.startScreencast"
        )
        await session.close()


class TestBadInput:
    async def test_oversized_message_is_dropped(self) -> None:
        session, cdp, _client = await _session()
        before = len(cdp.calls)
        await session.handle(json.dumps({"t": "type", "text": "x" * 5_000_000}))
        assert cdp.calls[before:] == []
        await session.close()

    async def test_malformed_json_is_dropped(self) -> None:
        session, cdp, _client = await _session()
        before = len(cdp.calls)
        await session.handle("{not json")
        assert cdp.calls[before:] == []
        await session.close()

    async def test_rejected_message_keeps_the_session_open(self) -> None:
        session, cdp, _client = await _session()
        await session.handle(json.dumps({"t": "navigate", "url": "file:///etc/passwd"}))
        assert "Page.navigate" not in cdp.methods()
        # Still usable afterwards: a bad message is dropped, not fatal.
        await session.handle(json.dumps({"t": "reload"}))
        assert "Page.reload" in cdp.methods()
        await session.close()


class TestTabs:
    async def test_target_changes_push_a_fresh_tab_list(self) -> None:
        session, cdp, client = await _session()
        client.text.clear()
        cdp.emit("Target.targetCreated", {"targetInfo": {"targetId": "t2"}})
        await asyncio.sleep(0.05)
        assert any(msg.get("t") == "tabs" for msg in client.text)
        await session.close()
