"""Tests for query-token auth path restrictions."""

from __future__ import annotations

from surogates.tenant.auth.middleware import (
    LIVE_VIEW_TOKEN_COOKIE,
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
