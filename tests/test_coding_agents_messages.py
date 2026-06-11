"""Unit tests for /code rendered chat messages."""

from __future__ import annotations

from surogates.coding_agents.messages import (
    render_connect_first,
    render_help,
    render_login_instructions,
    render_status,
)


def test_render_help_lists_subcommands():
    text = render_help()
    for token in ("/code claude", "/code codex", "/code login", "/code status"):
        assert token in text


def test_render_login_instructions_claude():
    text = render_login_instructions("claude")
    assert "claude setup-token" in text


def test_render_login_instructions_codex():
    text = render_login_instructions("codex")
    assert "codex login" in text


def test_render_status_marks_connected():
    statuses = [
        {"provider": "anthropic", "connected": True, "auth_mode": "oauth", "expires_at": None},
        {"provider": "openai", "connected": False, "auth_mode": None, "expires_at": None},
    ]
    text = render_status(statuses)
    assert "claude" in text and "codex" in text
    assert "connected" in text.lower()
    assert "not connected" in text.lower()


def test_render_connect_first():
    text = render_connect_first("claude")
    assert "/code login claude" in text
