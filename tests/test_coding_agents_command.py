"""Unit tests for the /code command parser."""

from __future__ import annotations

from surogates.coding_agents.command import (
    is_code_command,
    parse_code_command,
)


def test_is_code_command():
    assert is_code_command("/code") is True
    assert is_code_command("  /code claude hi  ") is True
    assert is_code_command("/codex hi") is False  # not /code
    assert is_code_command("hello") is False


def test_bare_and_help():
    assert parse_code_command("/code").action == "help"
    assert parse_code_command("/code help").action == "help"
    assert parse_code_command("not a command") is None


def test_status():
    assert parse_code_command("/code status").action == "status"


def test_login_logout():
    login = parse_code_command("/code login claude")
    assert login.action == "login"
    assert login.provider == "anthropic"
    assert login.agent == "claude"

    logout = parse_code_command("/code logout codex")
    assert logout.action == "logout"
    assert logout.provider == "openai"

    bad = parse_code_command("/code login")
    assert bad.action == "login"
    assert bad.error is not None


def test_run_with_quoted_prompt_and_flags():
    cmd = parse_code_command('/code claude "fix the build" --model opus --effort high')
    assert cmd.action == "run"
    assert cmd.agent == "claude"
    assert cmd.provider == "anthropic"
    assert cmd.prompt == "fix the build"
    assert cmd.flags == {"model": "opus", "effort": "high"}


def test_run_requires_prompt():
    cmd = parse_code_command("/code codex")
    assert cmd.action == "run"
    assert cmd.error is not None


def test_unknown_subcommand_is_help_with_error():
    cmd = parse_code_command("/code wat")
    assert cmd.action == "help"
    assert cmd.error is not None
