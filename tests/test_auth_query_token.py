"""Tests for query-token auth path restrictions."""

from __future__ import annotations

from surogates.tenant.auth.middleware import _allows_query_token


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


def test_query_token_rejected_for_browser_state() -> None:
    assert not _allows_query_token(
        "/v1/sessions/00000000-0000-0000-0000-000000000001/browser/state",
    )


def test_query_token_rejected_for_generic_api() -> None:
    assert not _allows_query_token("/v1/auth/me")
