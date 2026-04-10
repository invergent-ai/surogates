"""Workspace path sandboxing — enforced at the tool level.

Every tool that touches the filesystem (read, write, patch, search, list,
terminal) MUST validate paths through this module.  The workspace root is
set per-session at session creation time and passed to tools via the
``workspace_path`` kwarg.

**Threat model:**

- Process sandbox (Phase 1): tool constraints are the ONLY barrier.
  The worker process has full filesystem access.
- K8s pod sandbox (Phase 4): tool constraints are defense-in-depth.
  The pod's Linux user can still read ``/etc/``, ``/proc/``, env vars, etc.

In both cases, an LLM that is tricked into path traversal (``../../etc/passwd``)
or absolute paths (``/etc/shadow``) must be stopped at the tool layer.

**Guarantees:**

- All resolved paths MUST be children of the workspace root.
- Symlinks are resolved before comparison (prevents symlink escapes).
- ``~`` expansion is performed before resolution.
- Relative paths are resolved against the workspace root, not CWD.
- Empty or missing workspace_path raises immediately (fail-closed).
"""

from __future__ import annotations

import os
from pathlib import Path


class WorkspaceSandboxError(Exception):
    """Raised when a path violates the workspace sandbox."""


def validate_workspace_root(workspace_path: str | None) -> Path:
    """Validate and return the resolved workspace root.

    Raises :class:`WorkspaceSandboxError` if *workspace_path* is ``None``,
    empty, or does not exist.  This is fail-closed: if the workspace is
    not configured, no tool should execute.
    """
    if not workspace_path:
        raise WorkspaceSandboxError(
            "No workspace_path configured for this session. "
            "Cannot execute filesystem operations."
        )
    resolved = Path(workspace_path).resolve()
    if not resolved.is_dir():
        raise WorkspaceSandboxError(
            f"Workspace directory does not exist: {workspace_path}"
        )
    return resolved


def resolve_in_workspace(
    workspace_root: Path,
    user_path: str,
) -> Path:
    """Resolve *user_path* within the workspace, preventing traversal.

    The path is resolved in this order:

    1. ``~`` expansion (``expanduser``).
    2. If relative, joined to *workspace_root*.
    3. Symlink resolution (``resolve()``).
    4. Containment check (``is_relative_to``).

    Returns the resolved absolute path.

    Raises :class:`WorkspaceSandboxError` if the resolved path escapes
    the workspace root.
    """
    expanded = os.path.expanduser(user_path)
    candidate = Path(expanded)

    # Relative paths are resolved against workspace root, not CWD.
    if not candidate.is_absolute():
        candidate = workspace_root / candidate

    resolved = candidate.resolve()

    if not resolved.is_relative_to(workspace_root):
        raise WorkspaceSandboxError(
            f"Path traversal blocked: '{user_path}' resolves to "
            f"'{resolved}' which is outside the workspace "
            f"'{workspace_root}'."
        )

    return resolved


def validate_path(
    workspace_path: str | None,
    user_path: str,
) -> str:
    """One-shot convenience: validate workspace + resolve path.

    Returns the resolved path as a string.  Raises
    :class:`WorkspaceSandboxError` on any violation.
    """
    root = validate_workspace_root(workspace_path)
    return str(resolve_in_workspace(root, user_path))


def validate_workdir(
    workspace_path: str | None,
    workdir: str | None,
) -> str:
    """Validate a working directory for subprocess execution.

    If *workdir* is ``None`` or empty, returns the workspace root itself.
    Otherwise validates that *workdir* is inside the workspace.

    Returns the resolved workdir as a string.
    """
    root = validate_workspace_root(workspace_path)
    if not workdir:
        return str(root)
    return str(resolve_in_workspace(root, workdir))


def get_workspace_or_default(kwargs: dict) -> str | None:
    """Extract workspace_path from tool kwargs.

    Returns ``None`` only if no workspace is configured (caller must
    decide whether to fail or fall back).
    """
    return kwargs.get("workspace_path")
