"""``/workspace/...`` names the session's workspace with local storage too.

Every sandbox mounts the session's files at ``/workspace``, the path task
texts and the model use.  With S3 storage that is the session's
``workspace_path``; with local storage, the default of the published
images, ``workspace_path`` is a folder on the worker's host, which the
Docker sandbox mounts at ``/workspace``.  The governance check and the file
tools resolved ``/workspace/...`` literally on the host and refused it.  They
now read it as the workspace; everything that escapes it is still refused.
"""

from __future__ import annotations

import json
import shutil

import pytest

from surogates.governance.policy import GovernanceGate
from surogates.tools.builtin.file_ops import (
    _list_files_handler,
    _patch_handler,
    _read_file_handler,
    _search_files_handler,
    _write_file_handler,
)
from surogates.tools.utils import workspace_sandbox
from surogates.tools.builtin.terminal import _terminal_handler
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError, in_workspace_terms, validate_path


@pytest.fixture
def host(tmp_path):
    """A local-storage session folder, a folder beside it, and a symlink out."""
    root = tmp_path / "sessions" / "s1"
    (root / "input").mkdir(parents=True)
    (root / "input" / "a.txt").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not leak", encoding="utf-8")
    (root / "link").symlink_to(outside)
    return root, outside


def _allowed(path: str, workspace_path: str | None, tool: str = "read_file") -> bool:
    return GovernanceGate().check(tool, {"path": path}, workspace_path=workspace_path).allowed


@pytest.mark.parametrize("path", ["/workspace/input/a.txt", "/workspace", "/workspace/", "/workspace/new/b.txt",
                                  "input/a.txt"])
@pytest.mark.parametrize("tool", ["read_file", "write_file", "list_files"])
def test_the_gate_allows_the_mount_path_with_local_storage(host, path, tool):
    root, _ = host
    assert _allowed(path, str(root), tool)
    assert _allowed(str(root / "input" / "a.txt"), str(root), tool)  # the host path, as before


@pytest.mark.parametrize("path", [
    "/workspace/../x.txt",
    "/workspace/../s1/input/a.txt",  # inside a sandbox: /s1/input/a.txt, not the workspace
    "/workspace/input/../../x.txt",
    "../x.txt",
    "/workspace/link/secret.txt",  # a symlink out
    "link/secret.txt",
    "/workspace2/a.txt",  # prefix tricks
    "/workspacex",
    "//workspace/input/a.txt",
    "/etc/passwd",  # host paths outside the session folder
])
@pytest.mark.parametrize("tool", ["read_file", "write_file"])
def test_the_gate_still_refuses_what_escapes_the_workspace(host, path, tool):
    root, outside = host
    assert not _allowed(path, str(root), tool)
    assert not _allowed(str(outside / "secret.txt"), str(root), tool)
    assert not _allowed(str(root.parent / "s2" / "a.txt"), str(root), tool)  # another session's folder


def test_s3_and_the_desktop_are_unchanged(host):
    root, outside = host
    # S3: the workspace is /workspace itself.
    assert _allowed("/workspace/input/a.txt", "/workspace")
    assert not _allowed("/workspace/../etc/passwd", "/workspace")
    assert not _allowed(str(outside / "secret.txt"), "/workspace")
    assert in_workspace_terms("/workspace", "/workspace/input/a.txt") == "/workspace/input/a.txt"
    # The user's computer checks containment itself: this host checks nothing.
    assert _allowed("/workspace/input/a.txt", None)
    assert _allowed(str(outside / "secret.txt"), None)


def test_a_host_folder_under_workspace_keeps_its_own_paths():
    root = "/workspace/data/sessions/s1"
    assert in_workspace_terms(root, "/workspace/data/sessions/s1/a.txt") == "/workspace/data/sessions/s1/a.txt"
    assert in_workspace_terms(root, "/workspace/a.txt") == "/workspace/data/sessions/s1/a.txt"


def test_the_file_tools_resolve_the_mount_path_to_the_session_folder(host):
    root, _ = host
    assert validate_path(str(root), "/workspace/input/a.txt") == str((root / "input" / "a.txt").resolve())
    assert validate_path(str(root), "/workspace") == str(root.resolve())
    for path in ("/workspace/../x.txt", "/workspace/link/secret.txt", "/workspace2/a.txt", "/etc/passwd"):
        with pytest.raises(WorkspaceSandboxError):
            validate_path(str(root), path)


async def test_read_and_write_file_on_the_mount_path(host):
    root, _ = host
    read = json.loads(await _read_file_handler({"path": "/workspace/input/a.txt"}, workspace_path=str(root)))
    assert "inside" in json.dumps(read)
    written = json.loads(await _write_file_handler(
        {"path": "/workspace/out/b.txt", "content": "made"}, workspace_path=str(root),
    ))
    assert "error" not in written
    assert (root / "out" / "b.txt").read_text(encoding="utf-8") == "made"


async def test_read_and_write_file_still_refuse_escapes_through_the_mount_path(host):
    root, outside = host
    raw = await _read_file_handler({"path": "/workspace/link/secret.txt"}, workspace_path=str(root))
    assert "Path traversal blocked" in json.loads(raw)["error"] and "do not leak" not in raw
    raw = await _write_file_handler({"path": "/workspace/../escape.txt", "content": "owned"}, workspace_path=str(root))
    assert "Path traversal blocked" in json.loads(raw)["error"]
    assert not (root.parent / "escape.txt").exists()


async def test_the_terminal_runs_in_a_mount_path_workdir(host):
    root, _ = host
    result = json.loads(await _terminal_handler({"command": "pwd", "workdir": "/workspace/input"},
                                                workspace_path=str(root)))
    assert result["exit_code"] == 0, result
    assert result["output"].strip().endswith("input")
    blocked = json.loads(await _terminal_handler({"command": "pwd", "workdir": "/workspace/link"},
                                                 workspace_path=str(root)))
    assert blocked["status"] == "blocked"


def test_a_root_under_the_mount_path_is_read_as_a_sandbox_would(tmp_path, monkeypatch):
    mount = tmp_path / "mnt"
    root = mount / "a"
    (root / "x").mkdir(parents=True)
    (mount / "b").mkdir()
    monkeypatch.setattr(workspace_sandbox, "WORKSPACE_MOUNT", str(mount))
    for path in (f"{mount}/a/../b/x", f"{mount}/../x", f"{mount}/a/x/../../b"):
        assert not _allowed(path, str(root)), path
        with pytest.raises(WorkspaceSandboxError):
            validate_path(str(root), path)
    assert in_workspace_terms(str(root), f"{mount}/ab/x") == f"{root}/ab/x"
    assert _allowed(f"{mount}/a/x", str(root)) and _allowed(f"{mount}/x", str(root))
    assert in_workspace_terms("", "/workspace/x") == "/workspace/x"


def test_a_symlink_loop_is_refused_not_raised(host):
    root, _ = host
    (root / "loop").symlink_to(root / "loop")
    decision = GovernanceGate().check("read_file", {"path": "/workspace/loop/x"}, workspace_path=str(root))
    assert not decision.allowed


async def test_patch_on_the_mount_path(host):
    root, _ = host
    patched = json.loads(await _patch_handler(
        {"path": "/workspace/input/a.txt", "old_string": "inside", "new_string": "changed"},
        workspace_path=str(root),
    ))
    assert "error" not in patched, patched
    assert (root / "input" / "a.txt").read_text(encoding="utf-8") == "changed"


async def test_a_write_through_a_symlink_out_is_refused(host):
    root, outside = host
    raw = await _write_file_handler({"path": "/workspace/link/new.txt", "content": "owned"}, workspace_path=str(root))
    assert "Path traversal blocked" in json.loads(raw)["error"]
    assert not (outside / "new.txt").exists()


async def test_a_safe_write_root_takes_the_mount_path_as_the_workspace(host, monkeypatch):
    root, _ = host
    monkeypatch.setenv("SUROGATES_WRITE_SAFE_ROOT", str(root))
    raw = await _write_file_handler({"path": "/workspace/out/c.txt", "content": "made"}, workspace_path=str(root))
    assert "error" not in json.loads(raw), raw
    assert (root / "out" / "c.txt").read_text(encoding="utf-8") == "made"


@pytest.mark.skipif(shutil.which("rg") is None, reason="search_files and list_files need ripgrep")
async def test_search_and_list_on_the_mount_path(host):
    root, _ = host
    found = await _search_files_handler({"pattern": "inside", "path": "/workspace/input"}, workspace_path=str(root))
    assert "a.txt" in found
    escaped = await _search_files_handler({"pattern": "leak", "path": "/workspace/link"}, workspace_path=str(root))
    assert "do not leak" not in escaped
    listed = await _list_files_handler({"path": "/workspace/input"}, workspace_path=str(root))
    assert "a.txt" in listed
    raw = await _list_files_handler({"path": "/workspace/link"}, workspace_path=str(root))
    assert "secret.txt" not in raw
