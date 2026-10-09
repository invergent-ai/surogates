"""Workspace path sandbox regression tests for local tool fallbacks."""

from __future__ import annotations

import json

import pytest

from surogates.tools.builtin.file_ops import (
    _read_file_handler,
    _write_file_handler,
)
from surogates.harness.image_read import handle_image_read
from surogates.tools.builtin.terminal import _terminal_handler
from surogates.tools.utils.workspace_sandbox import validate_path, validate_workdir
from surogates.tools.workspace_io import NUL_REFUSED


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


# -- a NUL ---------------------------------------------------------------------

@pytest.mark.parametrize("path", ["a\0b", "a\0b.png", "/etc/a\0b", "~/a\0b"])
def test_a_nul_in_a_path_or_a_working_folder_is_refused_in_surogates_own_sentence(tmp_path, path):
    """Before the path is resolved: what Python says of a NUL depends on its release."""
    for validate in (validate_path, validate_workdir):
        with pytest.raises(ValueError) as refused:
            validate(str(tmp_path), path)
        assert str(refused.value) == NUL_REFUSED


@pytest.mark.asyncio
async def test_read_file_of_an_image_path_with_a_nul_is_refused_as_any_other_path(tmp_path):
    """An image is read by the worker, not by the file tool: the same answer, and nothing is asked to look at it."""
    async def dispatch(name, arguments, **kwargs):
        raise AssertionError(f"{name} was asked for a path with a NUL")

    answer = await handle_image_read(
        path="a\0b.png", arguments={"path": "a\0b.png"}, dispatch=dispatch, kwargs={"workspace_path": str(tmp_path)},
    )
    assert json.loads(answer) == {"error": NUL_REFUSED}

