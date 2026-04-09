"""Ttenant asset directory manager.

Every organisation (and optionally each user within it) gets an isolated
directory tree for memories, skills, MCP configs, and tools.  This module
creates and resolves those paths.

Directory layout under ``base_path``::

    {base}/{org_id}/shared/memories/
    {base}/{org_id}/shared/skills/
    {base}/{org_id}/shared/mcp/
    {base}/{org_id}/shared/tools/
    {base}/{org_id}/users/{user_id}/memories/
    {base}/{org_id}/users/{user_id}/skills/
    {base}/{org_id}/users/{user_id}/mcp/
    {base}/{org_id}/users/{user_id}/tools/
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

__all__ = ["TenantAssetManager"]

_SUBDIRS = ("memories", "skills", "mcp", "tools")


class TenantAssetManager:
    """Manages asset roots for tenants."""

    def __init__(self, base_path: str) -> None:
        self._base = Path(base_path)

    # ------------------------------------------------------------------
    # Root accessors
    # ------------------------------------------------------------------

    def get_asset_root(self, org_id: UUID) -> str:
        """Return the top-level asset directory for an organisation."""
        return str(self._base / str(org_id))

    def get_user_asset_root(self, org_id: UUID, user_id: UUID) -> str:
        """Return the asset directory scoped to a specific user."""
        return str(self._base / str(org_id) / "users" / str(user_id))

    # ------------------------------------------------------------------
    # Directory provisioning
    # ------------------------------------------------------------------

    async def ensure_asset_dirs(self, org_id: UUID, user_id: UUID) -> None:
        """Create the full directory tree for *org_id* / *user_id*.

        This is safe to call repeatedly; existing directories are left
        untouched.
        """
        org_str = str(org_id)
        user_str = str(user_id)

        shared_root = self._base / org_str / "shared"
        user_root = self._base / org_str / "users" / user_str

        for subdir in _SUBDIRS:
            os.makedirs(shared_root / subdir, exist_ok=True)
            os.makedirs(user_root / subdir, exist_ok=True)

    # ------------------------------------------------------------------
    # Convenience path builders
    # ------------------------------------------------------------------

    def memory_dir(self, org_id: UUID, user_id: UUID) -> str:
        """Return the user-scoped memories directory."""
        return str(
            self._base / str(org_id) / "users" / str(user_id) / "memories"
        )

    def skills_dir(self, org_id: UUID, user_id: UUID | None = None) -> str:
        """Return the skills directory (shared or user-scoped)."""
        if user_id is None:
            return str(self._base / str(org_id) / "shared" / "skills")
        return str(
            self._base / str(org_id) / "users" / str(user_id) / "skills"
        )

    def mcp_config_path(self, org_id: UUID, user_id: UUID | None = None) -> str:
        """Return the path to the MCP configuration directory."""
        if user_id is None:
            return str(self._base / str(org_id) / "shared" / "mcp")
        return str(
            self._base / str(org_id) / "users" / str(user_id) / "mcp"
        )
