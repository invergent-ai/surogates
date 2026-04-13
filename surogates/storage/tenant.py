"""Tenant-aware storage helpers.

Wraps ``StorageBackend`` with methods that understand the directory layout inside tenant buckets.  
Used by API routes to read/write skills, memory, and workspace files without knowing the backend details.

Bucket naming:
- ``tenant-{org_id}`` — org/user skills, memory, MCP config
- ``session-{session_id}`` — workspace files

Key layout inside tenant bucket:
- ``shared/skills/{name}/SKILL.md``
- ``shared/skills/{category}/{name}/SKILL.md``
- ``users/{user_id}/skills/{name}/SKILL.md``
- ``users/{user_id}/memory/MEMORY.md``
- ``users/{user_id}/memory/USER.md``
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from surogates.storage.backend import StorageBackend

logger = logging.getLogger(__name__)


def tenant_bucket(org_id: UUID | str) -> str:
    """Return the bucket name for a tenant."""
    return f"tenant-{org_id}"


def session_bucket(session_id: UUID | str) -> str:
    """Return the bucket name for a session."""
    return f"session-{session_id}"


class TenantStorage:
    """Tenant-aware storage operations.

    Provides high-level methods for skills and memory that translate
    between the directory convention and ``StorageBackend``
    bucket/key calls.
    """

    def __init__(self, backend: StorageBackend, org_id: UUID, user_id: UUID) -> None:
        self._backend = backend
        self._bucket = tenant_bucket(org_id)
        self._org_id = str(org_id)
        self._user_id = str(user_id)

    # ── Bucket lifecycle ────────────────────────────────────────────

    async def ensure_bucket(self) -> None:
        """Create the tenant bucket if it doesn't exist."""
        if not await self._backend.bucket_exists(self._bucket):
            await self._backend.create_bucket(self._bucket)

    # ── Skills (user layer) ─────────────────────────────────────────

    def _user_skill_key(self, name: str, category: str | None = None) -> str:
        """Build the key prefix for a user skill directory."""
        if category:
            return f"users/{self._user_id}/skills/{category}/{name}"
        return f"users/{self._user_id}/skills/{name}"

    async def skill_exists(self, name: str) -> dict[str, Any] | None:
        """Find a skill by name across user, org-shared, and platform layers.

        Returns ``{"key_prefix": str, "layer": str}`` or ``None``.
        """
        # User layer
        user_prefix = f"users/{self._user_id}/skills/"
        user_keys = await self._backend.list_keys(self._bucket, prefix=user_prefix)
        for key in user_keys:
            parts = key.split("/")
            # users/{uid}/skills/{name}/SKILL.md or users/{uid}/skills/{cat}/{name}/SKILL.md
            if parts[-1] == "SKILL.md":
                skill_name = parts[-2]
                if skill_name == name:
                    prefix = "/".join(parts[:-1])
                    return {"key_prefix": prefix, "layer": "user"}

        # Org-shared layer
        shared_prefix = "shared/skills/"
        shared_keys = await self._backend.list_keys(self._bucket, prefix=shared_prefix)
        for key in shared_keys:
            parts = key.split("/")
            if parts[-1] == "SKILL.md":
                skill_name = parts[-2]
                if skill_name == name:
                    prefix = "/".join(parts[:-1])
                    return {"key_prefix": prefix, "layer": "org"}

        return None

    async def read_skill(self, key_prefix: str) -> str:
        """Read a SKILL.md file."""
        return await self._backend.read_text(self._bucket, f"{key_prefix}/SKILL.md")

    async def write_skill(self, name: str, content: str, category: str | None = None) -> str:
        """Write a new skill.  Returns the key prefix."""
        key_prefix = self._user_skill_key(name, category)
        await self._backend.write_text(self._bucket, f"{key_prefix}/SKILL.md", content)
        return key_prefix

    async def overwrite_skill(self, key_prefix: str, content: str) -> None:
        """Overwrite an existing SKILL.md."""
        await self._backend.write_text(self._bucket, f"{key_prefix}/SKILL.md", content)

    async def delete_skill(self, key_prefix: str) -> None:
        """Delete all files under a skill's key prefix."""
        keys = await self._backend.list_keys(self._bucket, prefix=key_prefix)
        for key in keys:
            await self._backend.delete(self._bucket, key)

    async def list_skill_files(self, key_prefix: str) -> list[str]:
        """List all files under a skill's key prefix (relative to prefix)."""
        keys = await self._backend.list_keys(self._bucket, prefix=key_prefix)
        prefix_len = len(key_prefix) + 1  # +1 for trailing /
        return [k[prefix_len:] for k in keys if len(k) > prefix_len]

    async def read_skill_file(self, key_prefix: str, file_path: str) -> str:
        """Read a supporting file from a skill directory."""
        return await self._backend.read_text(self._bucket, f"{key_prefix}/{file_path}")

    async def write_skill_file(self, key_prefix: str, file_path: str, content: str) -> None:
        """Write a supporting file to a skill directory."""
        await self._backend.write_text(self._bucket, f"{key_prefix}/{file_path}", content)

    async def delete_skill_file(self, key_prefix: str, file_path: str) -> None:
        """Delete a supporting file from a skill directory."""
        await self._backend.delete(self._bucket, f"{key_prefix}/{file_path}")

    async def skill_file_exists(self, key_prefix: str, file_path: str) -> bool:
        """Check if a supporting file exists."""
        return await self._backend.exists(self._bucket, f"{key_prefix}/{file_path}")

    # ── Skills listing (all layers) ─────────────────────────────────

    async def list_all_skills(self) -> list[dict[str, Any]]:
        """List all skills across user and org-shared layers.

        Returns a list of dicts with ``name``, ``key_prefix``, ``layer``.
        Does NOT include platform skills (those are on the container filesystem).
        """
        skills: dict[str, dict[str, Any]] = {}

        # User layer (highest precedence)
        user_prefix = f"users/{self._user_id}/skills/"
        user_keys = await self._backend.list_keys(self._bucket, prefix=user_prefix)
        for key in user_keys:
            if key.endswith("/SKILL.md"):
                parts = key.split("/")
                name = parts[-2]
                if name not in skills:
                    prefix = "/".join(parts[:-1])
                    skills[name] = {"name": name, "key_prefix": prefix, "layer": "user"}

        # Org-shared layer
        shared_prefix = "shared/skills/"
        shared_keys = await self._backend.list_keys(self._bucket, prefix=shared_prefix)
        for key in shared_keys:
            if key.endswith("/SKILL.md"):
                parts = key.split("/")
                name = parts[-2]
                if name not in skills:  # user layer takes precedence
                    prefix = "/".join(parts[:-1])
                    skills[name] = {"name": name, "key_prefix": prefix, "layer": "org"}

        return list(skills.values())

    # ── Memory ──────────────────────────────────────────────────────

    def _memory_key(self, filename: str) -> str:
        """Build key for a memory file."""
        return f"users/{self._user_id}/memory/{filename}"

    async def read_memory_file(self, filename: str) -> str | None:
        """Read MEMORY.md or USER.md.  Returns None if not found."""
        key = self._memory_key(filename)
        try:
            return await self._backend.read_text(self._bucket, key)
        except KeyError:
            return None

    async def write_memory_file(self, filename: str, content: str) -> None:
        """Write MEMORY.md or USER.md."""
        key = self._memory_key(filename)
        await self._backend.write_text(self._bucket, key, content)
