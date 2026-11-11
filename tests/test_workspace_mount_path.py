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

import pytest

from surogates.governance.policy import GovernanceGate
from surogates.tools.builtin.file_ops import _read_file_handler, _write_file_handler
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
