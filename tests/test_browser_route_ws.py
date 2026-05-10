"""Tests for browser live-view WebSocket gating helpers."""

from __future__ import annotations

from uuid import UUID

from starlette.datastructures import QueryParams

from surogates.api.routes.browser import (
    _live_view_upstream_ws_url,
    _should_forward_client_frame,
)
from surogates.tenant.context import TenantContext


USER_1 = UUID("10000000-0000-0000-0000-000000000001")


class FakeControl:
    def __init__(self, holder: str | None) -> None:
        self.holder = holder
        self.calls: list[str] = []

    async def held_by(self, session_id: str) -> str | None:
        self.calls.append(session_id)
        return self.holder


def _tenant(user_id: UUID | None = USER_1) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=user_id,
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/surogates-test",
    )


class TestShouldForwardClientFrame:
    async def test_drops_input_when_control_not_held(self) -> None:
        control = FakeControl(holder=None)

        allowed = await _should_forward_client_frame(
            session_id="sess-1",
            tenant=_tenant(),
            control=control,
            frame=bytes([4]) + bytes(7),
        )

        assert allowed is False
        assert control.calls == ["sess-1"]

    async def test_forwards_input_when_user_holds_control(self) -> None:
        control = FakeControl(holder=str(USER_1))

        allowed = await _should_forward_client_frame(
            session_id="sess-1",
            tenant=_tenant(),
            control=control,
            frame=bytes([5]) + bytes(5),
        )

        assert allowed is True
        assert control.calls == ["sess-1"]

    async def test_forwards_non_input_without_control_lookup(self) -> None:
        control = FakeControl(holder=None)

        allowed = await _should_forward_client_frame(
            session_id="sess-1",
            tenant=_tenant(),
            control=control,
            frame=bytes([2]) + bytes(7),
        )

        assert allowed is True
        assert control.calls == []

    async def test_drops_input_when_tenant_has_no_user_identity(self) -> None:
        control = FakeControl(holder="ops-user")

        allowed = await _should_forward_client_frame(
            session_id="sess-1",
            tenant=_tenant(user_id=None),
            control=control,
            frame=bytes([6]) + bytes(7),
        )

        assert allowed is False
        assert control.calls == []


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
