"""Workspace path sandbox regression tests for local tool fallbacks."""

from __future__ import annotations

import json

import pytest

from surogates.tools.builtin.file_ops import (
    _read_file_handler,
    _write_file_handler,
)
from surogates.tools.builtin.terminal import _terminal_handler


@pytest.mark.asyncio
async def test_read_file_blocks_symlink_escape_when_workspace_set(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("do not leak", encoding="utf-8")
    (workspace / "link").symlink_to(outside)

    raw = await _read_file_handler(
        {"path": "link/secret.txt"},
        workspace_path=str(workspace),
    )

    result = json.loads(raw)
    assert "Path traversal blocked" in result["error"]
    assert "do not leak" not in raw


@pytest.mark.asyncio
async def test_write_file_blocks_parent_traversal_when_workspace_set(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    raw = await _write_file_handler(
        {"path": "../escape.txt", "content": "owned"},
        workspace_path=str(workspace),
    )

    result = json.loads(raw)
    assert "Path traversal blocked" in result["error"]
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.asyncio
async def test_terminal_blocks_symlink_workdir_escape(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "link").symlink_to(outside)

    raw = await _terminal_handler(
        {"command": "pwd", "workdir": "link"},
        workspace_path=str(workspace),
    )

    result = json.loads(raw)
    assert result["status"] == "blocked"
    assert result["exit_code"] == -1
    assert "Path traversal blocked" in result["error"]
