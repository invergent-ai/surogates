"""Tests for filesystem-path handling during tool execution.

Covers:

* Workspace-path sanitisation in events vs. tool-result messages — the
  frontend-visible event payload must hide real filesystem paths, while
  the tool-result content returned to the LLM must keep them so the
  model's mental map matches the sandbox.
* Shell-variable rejection in path-typed tool arguments — ``$HOME``,
  ``${HOME}``, etc. in path args produce a clear governance refusal
  instead of a literal ``$HOME`` directory being created.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from surogates.harness.tool_exec import (
    _sanitize_paths,
    execute_single_tool,
)
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema


# ---------------------------------------------------------------------------
# _sanitize_paths — pure helper
# ---------------------------------------------------------------------------


def test_sanitize_paths_replaces_workspace_in_string() -> None:
    assert _sanitize_paths("/tmp/sbx-abc/foo.py", "/tmp/sbx-abc") == \
        "__WORKSPACE__/foo.py"


def test_sanitize_paths_replaces_workspace_in_dict() -> None:
    out = _sanitize_paths(
        {"path": "/tmp/sbx-abc/foo.py", "size": 12}, "/tmp/sbx-abc",
    )
    assert out == {"path": "__WORKSPACE__/foo.py", "size": 12}


def test_sanitize_paths_returns_input_when_no_workspace() -> None:
    payload = {"path": "/tmp/sbx-abc/foo.py"}
    assert _sanitize_paths(payload, None) is payload


def test_sanitize_paths_strips_trailing_slash_consistently() -> None:
    # Both trailing and non-trailing forms of the workspace should match.
    assert _sanitize_paths("/tmp/sbx-abc/foo", "/tmp/sbx-abc/") == \
        "__WORKSPACE__/foo"


# ---------------------------------------------------------------------------
# Helpers for execute_single_tool tests
# ---------------------------------------------------------------------------


def _make_terminal_registry(handler_output: str) -> ToolRegistry:
    """Registry with a single ``terminal`` tool returning *handler_output*."""
    registry = ToolRegistry()
    handler = AsyncMock(return_value=handler_output)
    registry.register(
        "terminal",
        ToolSchema(
            name="terminal",
            description="run a shell command",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
        ),
        handler=handler,
    )
    return registry


def _make_write_file_registry(handler_output: str = '{"status": "ok"}') -> ToolRegistry:
    registry = ToolRegistry()
    handler = AsyncMock(return_value=handler_output)
    registry.register(
        "write_file",
        ToolSchema(
            name="write_file",
            description="write a file",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
        ),
        handler=handler,
    )
    return registry


def _make_session(workspace_path: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        config={"workspace_path": workspace_path},
        agent_id="test-agent",
        model="gpt-4o",
    )


def _make_lease() -> SimpleNamespace:
    return SimpleNamespace(lease_token=uuid4())


def _make_store() -> AsyncMock:
    store = AsyncMock()
    # emit_event is called multiple times across a single tool execution;
    # each call must return a distinct event id.
    store.emit_event = AsyncMock(side_effect=lambda *a, **k: next(_ids))
    store.advance_harness_cursor = AsyncMock()
    return store


_ids = iter(range(1, 10_000))


# ---------------------------------------------------------------------------
# Asymmetry: event payload sanitised, LLM message keeps real paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_result_event_sanitises_workspace_path() -> None:
    """The TOOL_RESULT event stored in the log redacts the workspace path."""
    workspace = "/tmp/sbx-real-path"
    raw_output = f"{workspace}\nls: file not found"
    registry = _make_terminal_registry(raw_output)
    store = _make_store()

    await execute_single_tool(
        {
            "id": "tc_1",
            "function": {
                "name": "terminal",
                "arguments": '{"command": "pwd"}',
            },
        },
        session=_make_session(workspace),
        lease=_make_lease(),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    # Inspect the TOOL_RESULT emit. The event payload must NOT contain the
    # raw workspace path — frontend SSE consumers should see the placeholder.
    tool_result_calls = [
        c for c in store.emit_event.call_args_list
        if c.args[1] is EventType.TOOL_RESULT
    ]
    assert tool_result_calls, "TOOL_RESULT was never emitted"
    payload = tool_result_calls[0].args[2]
    assert workspace not in payload["content"]
    assert "__WORKSPACE__" in payload["content"]


@pytest.mark.asyncio
async def test_tool_result_returned_to_llm_keeps_real_paths() -> None:
    """The dict returned for the LLM must keep the real workspace path.

    Sanitisation is a frontend privacy concern; the LLM must see real
    paths so its next tool call uses a path that actually exists.
    """
    workspace = "/tmp/sbx-real-path"
    raw_output = f"{workspace}\nls: file not found"
    registry = _make_terminal_registry(raw_output)
    store = _make_store()

    result = await execute_single_tool(
        {
            "id": "tc_1",
            "function": {
                "name": "terminal",
                "arguments": '{"command": "pwd"}',
            },
        },
        session=_make_session(workspace),
        lease=_make_lease(),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    assert result["role"] == "tool"
    assert workspace in result["content"]
    assert "__WORKSPACE__" not in result["content"]


# ---------------------------------------------------------------------------
# Path-arg hygiene: reject shell-variable patterns.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_rejects_dollar_var_in_path() -> None:
    """``$HOME/foo.py`` must be rejected with a clear, actionable error."""
    registry = _make_write_file_registry()
    handler = registry.get("write_file").handler
    store = _make_store()

    result = await execute_single_tool(
        {
            "id": "tc_1",
            "function": {
                "name": "write_file",
                "arguments": json.dumps(
                    {"path": "$HOME/fetch_bitcoin.py", "content": "x"},
                ),
            },
        },
        session=_make_session("/tmp/sbx-abc"),
        lease=_make_lease(),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    # The handler must NOT have been invoked — a literal $HOME directory
    # would have been created on disk had the call gone through.
    handler.assert_not_called()
    parsed = json.loads(result["content"])
    assert "error" in parsed or "Blocked" in result["content"]
    err = json.dumps(parsed)
    assert "$HOME" in err or "shell variable" in err.lower()


@pytest.mark.asyncio
async def test_write_file_rejects_dollar_brace_var_in_path() -> None:
    """``${HOME}/foo.py`` is also a shell expansion and must be rejected."""
    registry = _make_write_file_registry()
    handler = registry.get("write_file").handler
    store = _make_store()

    result = await execute_single_tool(
        {
            "id": "tc_1",
            "function": {
                "name": "write_file",
                "arguments": json.dumps(
                    {"path": "${HOME}/foo.py", "content": "x"},
                ),
            },
        },
        session=_make_session("/tmp/sbx-abc"),
        lease=_make_lease(),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    handler.assert_not_called()
    parsed = json.loads(result["content"])
    err = json.dumps(parsed)
    assert "${HOME}" in err or "shell variable" in err.lower()


@pytest.mark.asyncio
async def test_write_file_allows_relative_path() -> None:
    """A normal relative path must reach the handler — regression check."""
    registry = _make_write_file_registry()
    handler = registry.get("write_file").handler
    store = _make_store()

    await execute_single_tool(
        {
            "id": "tc_1",
            "function": {
                "name": "write_file",
                "arguments": json.dumps(
                    {"path": "fetch_bitcoin.py", "content": "x"},
                ),
            },
        },
        session=_make_session("/tmp/sbx-abc"),
        lease=_make_lease(),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    handler.assert_called_once()


@pytest.mark.asyncio
async def test_terminal_workdir_rejects_dollar_var() -> None:
    """``terminal.workdir`` is also a path-typed argument and must reject ``$HOME``."""
    registry = _make_terminal_registry("ok")
    handler = registry.get("terminal").handler
    store = _make_store()

    result = await execute_single_tool(
        {
            "id": "tc_1",
            "function": {
                "name": "terminal",
                "arguments": json.dumps(
                    {"command": "ls", "workdir": "$HOME/sub"},
                ),
            },
        },
        session=_make_session("/tmp/sbx-abc"),
        lease=_make_lease(),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    handler.assert_not_called()
    parsed = json.loads(result["content"])
    err = json.dumps(parsed)
    assert "$HOME" in err or "shell variable" in err.lower()


# ---------------------------------------------------------------------------
# System prompt guidance — file-tool paths are taken literally.
# ---------------------------------------------------------------------------


def test_workspace_rules_warn_about_literal_paths() -> None:
    """The injected workspace rules must call out literal-path interpretation.

    Without this guidance the model treats ``$HOME/foo.py`` as a normal
    relative-ish path and the file tools create a directory named ``$HOME``.
    """
    from surogates.harness.prompt_library import default_library

    body = default_library().get("identity/workspace_rules")
    text = body.lower()
    assert "literal" in text
    assert "$home" in text
    # Mention the specific tools whose path args are literal.
    assert "write_file" in body
    assert "read_file" in body


@pytest.mark.asyncio
async def test_terminal_command_can_contain_dollar_var() -> None:
    """``terminal.command`` is shell-interpreted — ``$HOME`` is normal there."""
    registry = _make_terminal_registry("ok")
    handler = registry.get("terminal").handler
    store = _make_store()

    await execute_single_tool(
        {
            "id": "tc_1",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": "echo $HOME"}),
            },
        },
        session=_make_session("/tmp/sbx-abc"),
        lease=_make_lease(),
        store=store,
        tools=registry,
        tenant=MagicMock(asset_root="/tmp/test"),
    )

    handler.assert_called_once()
