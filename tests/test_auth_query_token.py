"""Tests for query-token auth path restrictions."""

from __future__ import annotations

from surogates.tenant.auth.middleware import (
    LIVE_VIEW_TOKEN_COOKIE,
    _allows_live_view_cookie,
    _allows_query_token,
    _query_or_live_view_cookie_token,
)


def test_query_token_allowed_for_user_sse() -> None:
    assert _allows_query_token(
        "/v1/sessions/00000000-0000-0000-0000-000000000001/events",
    )


def test_query_token_allowed_for_api_sse() -> None:
    assert _allows_query_token(
        "/v1/api/sessions/00000000-0000-0000-0000-000000000001/events",
    )


def test_query_token_allowed_for_browser_live_view() -> None:
    assert _allows_query_token(
        "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/",
    )


def test_query_token_allowed_for_api_browser_live_view() -> None:
    assert _allows_query_token(
        "/v1/api/sessions/00000000-0000-0000-0000-000000000001/browser/live/ws",
    )


def test_query_token_allowed_for_browser_shell() -> None:
    # The shell websocket is the live view's replacement and sits in the
    # same category the allow-list exists for: a browser primitive that
    # cannot attach an Authorization header. Omitting it 401'd every web
    # SPA viewer while ops — which proxies with a service-account token in
    # a header — worked, so the gap read as "only the SPA is broken".
    assert _allows_query_token(
        "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/shell",
    )


def test_query_token_allowed_for_api_browser_shell() -> None:
    assert _allows_query_token(
        "/v1/api/sessions/00000000-0000-0000-0000-000000000001/browser/shell",
    )


def test_browser_shell_does_not_accept_the_live_view_cookie() -> None:
    # The cookie exists so live-view SUBRESOURCES (js/css the iframe pulls)
    # authenticate without a query string. The shell is a single socket that
    # always carries ?token=, so it needs no cookie surface.
    assert not _allows_live_view_cookie(
        "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/shell",
    )


def test_query_token_allowed_for_workspace_download() -> None:
    assert _allows_query_token(
        "/v1/sessions/00000000-0000-0000-0000-000000000001/workspace/download",
    )


def test_query_token_allowed_for_api_workspace_download() -> None:
    assert _allows_query_token(
        "/v1/api/sessions/00000000-0000-0000-0000-000000000001/workspace/download",
    )


def test_query_token_rejected_for_browser_state() -> None:
    assert not _allows_query_token(
        "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/state",
    )


def test_query_token_rejected_for_generic_api() -> None:
    assert not _allows_query_token("/v1/auth/me")


def test_browser_live_view_can_authenticate_subresources_with_cookie() -> None:
    token = _query_or_live_view_cookie_token(
        path="/v1/sessions/00000000-0000-0000-0000-000000000001/browser/live/js/app.js",
        query_params={},
        cookies={LIVE_VIEW_TOKEN_COOKIE: "jwt"},
    )

    assert token == "jwt"


def test_cookie_token_rejected_for_non_live_view_paths() -> None:
    token = _query_or_live_view_cookie_token(
        path="/v1/sessions/00000000-0000-0000-0000-000000000001/events",
        query_params={},
        cookies={LIVE_VIEW_TOKEN_COOKIE: "jwt"},
    )

    assert token is None
