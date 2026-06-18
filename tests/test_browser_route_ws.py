"""Tests for browser live-view WebSocket gating helpers."""

from __future__ import annotations

from uuid import UUID

from starlette.datastructures import QueryParams

from surogates.api.routes.browser import (
    _live_view_client_payload,
    _send_live_view_frame_to_client,
    _live_view_upstream_ws_url,
)
from surogates.tenant.context import TenantContext


USER_1 = UUID("10000000-0000-0000-0000-000000000001")


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | bytes]] = []

    async def send_text(self, frame: str) -> None:
        self.sent.append(("text", frame))

    async def send_bytes(self, frame: bytes) -> None:
        self.sent.append(("bytes", frame))


def _tenant(user_id: UUID | None = USER_1) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=user_id,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/surogates-test",
    )


class TestLiveViewUpstreamWsUrl:
    def test_builds_websocket_url_for_any_live_view_path(self) -> None:
        assert (
            _live_view_upstream_ws_url("ws://browser:8080", "api/ws")
            == "ws://browser:8080/api/ws"
        )

    def test_preserves_websockify_path_for_legacy_clients(self) -> None:
        assert (
            _live_view_upstream_ws_url("ws://browser:8080/", "websockify")
            == "ws://browser:8080/websockify"
        )

    def test_builds_root_websocket_url(self) -> None:
        assert (
            _live_view_upstream_ws_url("ws://browser:8080/", "")
            == "ws://browser:8080/"
        )

    def test_preserves_live_view_query_params_except_token(self) -> None:
        assert (
            _live_view_upstream_ws_url(
                "ws://browser:8080",
                "api/ws",
                QueryParams("token=secret&foo=bar&foo=baz"),
            )
            == "ws://browser:8080/api/ws?foo=bar&foo=baz"
        )


class TestLiveViewWebSocketFrameTypes:
    async def test_sends_upstream_text_frames_to_browser_as_text(self) -> None:
        websocket = FakeWebSocket()

        await _send_live_view_frame_to_client(websocket, '{"event":"member/list"}')

        assert websocket.sent == [("text", '{"event":"member/list"}')]

    async def test_sends_upstream_binary_frames_to_browser_as_binary(self) -> None:
        websocket = FakeWebSocket()

        await _send_live_view_frame_to_client(websocket, b"\x02frame")

        assert websocket.sent == [("bytes", b"\x02frame")]

    def test_preserves_browser_text_frames_for_upstream(self) -> None:
        assert _live_view_client_payload({"text": '{"event":"client/heartbeat"}'}) == (
            '{"event":"client/heartbeat"}'
        )

    def test_preserves_browser_binary_frames_for_upstream(self) -> None:
        assert _live_view_client_payload({"bytes": b"\x04frame"}) == b"\x04frame"


# --- Full-route coverage of proxy_live_view_ws (control-required + RFB proxy) ---

import asyncio

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import surogates.api.routes.browser as browser_mod


class _FakeEndpoint:
    def __init__(self, live_view_url: str) -> None:
        self.live_view_url = live_view_url


class _FakeResolved:
    def __init__(self, live_view_url: str) -> None:
        self.endpoint = _FakeEndpoint(live_view_url)


class _FakeResolver:
    def __init__(self, resolved: _FakeResolved | None) -> None:
        self._resolved = resolved

    async def resolve(self, session_id: str, expected_org_id: str | None = None):
        return self._resolved


class _FakeControlHeld:
    def __init__(self, holder: str | None) -> None:
        self._holder = holder

    async def held_by(self, session_id: str) -> str | None:
        return self._holder


class _FakeUpstream:
    def __init__(self, sends: list[bytes]) -> None:
        self._sends = list(sends)
        self.received: list[bytes | str] = []

    def __aiter__(self):
        async def _gen():
            for frame in self._sends:
                yield frame
            # Keep the read side open so the client->upstream pump has time to
            # forward input before the connection is torn down.
            await asyncio.Event().wait()
        return _gen()

    async def send(self, frame) -> None:
        self.received.append(frame)

    async def close(self) -> None:
        pass


def _build_ws_app(
    monkeypatch, *, control_holder, upstream_sends, tenant_user_id=USER_1, prefix=""
):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(browser_mod.router, prefix=prefix)
    app.state.browser_resolver = _FakeResolver(_FakeResolved("ws://browser:8080"))
    app.state.browser_control = _FakeControlHeld(control_holder)

    async def _fake_auth(_app, *, path, token, cookies, authorization):
        return _tenant(user_id=tenant_user_id)

    monkeypatch.setattr(browser_mod, "authenticate_websocket_tenant", _fake_auth)

    upstream = _FakeUpstream(upstream_sends)

    async def _fake_connect(url: str):
        return upstream

    monkeypatch.setattr(browser_mod, "_connect_live_view_ws", _fake_connect)
    return app, upstream


_SID = "00000000-0000-0000-0000-0000000000aa"


def test_live_view_ws_rejects_non_control_holder(monkeypatch) -> None:
    app, _ = _build_ws_app(monkeypatch, control_holder="someone-else", upstream_sends=[])
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/sessions/{_SID}/browser/live/?token=t"):
            pass
    assert exc.value.code == 4403


def test_live_view_ws_proxies_rfb_for_holder(monkeypatch) -> None:
    app, _ = _build_ws_app(
        monkeypatch,
        control_holder=str(USER_1),
        upstream_sends=[b"RFB 003.008\n"],
    )
    client = TestClient(app)
    with client.websocket_connect(f"/api/sessions/{_SID}/browser/live/?token=t") as ws:
        frame = ws.receive_bytes()
    assert frame.startswith(b"RFB 00")


def test_service_account_holder_input_is_forwarded(monkeypatch) -> None:
    # PROD "take control": the ops proxy connects to the runtime as a service
    # account (tenant.user_id is None) on the /v1/api path and asserts the
    # holder via ?owner_user_id=. Input must still be forwarded for that holder
    # — the gate must key on the effective user, not tenant.user_id.
    import time

    app, upstream = _build_ws_app(
        monkeypatch,
        control_holder="owner-x",
        upstream_sends=[],
        tenant_user_id=None,
        prefix="/v1",
    )
    client = TestClient(app)
    url = f"/v1/api/sessions/{_SID}/browser/live/ws?owner_user_id=owner-x&token=t"
    handshake = b"RFB 003.008\n\x01\x01"  # 14 client handshake bytes (forwarded raw)
    pointer = b"\x05\x00\x00\x0a\x00\x0b"  # RFB PointerEvent (type 5) — input

    with client.websocket_connect(url) as ws:
        ws.send_bytes(handshake + pointer)
        deadline = time.time() + 3
        while time.time() < deadline and len(
            b"".join(c for c in upstream.received if isinstance(c, bytes))
        ) < len(handshake) + len(pointer):
            time.sleep(0.02)

    forwarded = b"".join(c for c in upstream.received if isinstance(c, bytes))
    assert pointer in forwarded, "PointerEvent dropped — live view is read-only"
