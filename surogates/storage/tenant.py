"""Tenant-aware storage helpers.

Wraps ``StorageBackend`` with methods that understand the directory layout inside tenant buckets.  
Used by API routes to read/write skills, memory, and workspace files without knowing the backend details.

Bucket naming:
- ``tenant-{org_id}`` — org/user skills, memory, MCP config
- configured agent bucket — session workspace files under
  ``sessions/{session_id}/``

Key layout inside tenant bucket:
- ``shared/skills/{name}/SKILL.md``
- ``shared/skills/{category}/{name}/SKILL.md``
- ``shared/agents/{name}/AGENT.md``
- ``shared/agents/{category}/{name}/AGENT.md``
- ``users/{user_id}/skills/{name}/SKILL.md``
- ``users/{user_id}/agents/{name}/AGENT.md``
- ``users/{user_id}/memory/MEMORY.md``
- ``users/{user_id}/memory/USER.md``
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from uuid import UUID

from surogates.storage.backend import StorageBackend

logger = logging.getLogger(__name__)


def tenant_bucket(org_id: UUID | str) -> str:
    """Return the bucket name for a tenant."""
    return f"tenant-{org_id}"


_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _validate_s3_bucket_name(bucket: str) -> str:
    if (
        not _S3_BUCKET_RE.fullmatch(bucket)
        or ".." in bucket
        or ".-" in bucket
        or "-." in bucket
    ):
        raise ValueError(
            f"Agent bucket '{bucket}' is not S3-compatible."
        )
    return bucket


def agent_session_bucket(bucket: str) -> str:
    """Return the configured per-agent bucket name for session workspaces."""
    if not bucket:
        raise ValueError("agent_session_bucket requires a configured storage bucket")
    return _validate_s3_bucket_name(bucket)


def session_workspace_prefix(session_id: UUID | str) -> str:
    """Return the hard-coded object prefix for a session workspace."""
    return f"sessions/{session_id}/"


def session_workspace_key(session_id: UUID | str, key: str = "") -> str:
    """Return *key* scoped under the session workspace prefix."""
    return f"{session_workspace_prefix(session_id)}{key.lstrip('/')}"


class TenantStorage:
    """Tenant-aware storage operations.

    Provides high-level methods for skills and memory that translate
    between the directory convention and ``StorageBackend``
    bucket/key calls.

    *user_id* may be ``None`` for service-account sessions (channel
    ``"api"``) which have no per-user storage scope.  In that case
    memory operations route to ``shared/memory/*`` and skill writes
    land in the org-shared layer.  Listing skills still surfaces both
    layers; only the user-scoped one is empty by construction.
    """

    def __init__(
        self,
        backend: StorageBackend,
        org_id: UUID,
        user_id: UUID | None,
    ) -> None:
        self._backend = backend
        self._bucket = tenant_bucket(org_id)
        self._org_id = str(org_id)
        self._user_id = str(user_id) if user_id is not None else None

    # ── Bucket lifecycle ────────────────────────────────────────────

    async def ensure_bucket(self) -> None:
        """Create the tenant bucket if it doesn't exist."""
        if not await self._backend.bucket_exists(self._bucket):
            await self._backend.create_bucket(self._bucket)

    # ── Skills (user layer) ─────────────────────────────────────────

    def _shared_skill_key(self, name: str, category: str | None = None) -> str:
        """Build the key prefix for an org-shared skill directory."""
        if category:
            return f"shared/skills/{category}/{name}"
        return f"shared/skills/{name}"

    def _user_skill_key(self, name: str, category: str | None = None) -> str:
        """Build the key prefix for a user-scoped skill directory.

        Raises ``ValueError`` when there is no user scope (service-account
        sessions); callers must pick :meth:`_shared_skill_key` explicitly
        in that case.
        """
        if self._user_id is None:
            raise ValueError(
                "_user_skill_key requires a user-scoped TenantStorage; "
                "use _shared_skill_key for service-account contexts."
            )
        if category:
            return f"users/{self._user_id}/skills/{category}/{name}"
        return f"users/{self._user_id}/skills/{name}"

    def _default_skill_write_key(
        self, name: str, category: str | None = None
    ) -> str:
        """Return the key prefix where new skills from this context land.

        User-scoped contexts write into their ``users/{uid}/`` subtree;
        service-account contexts have no per-user scope and land in
        ``shared/``.  Kept as an explicit helper so ``write_skill``
        reads straight rather than relying on an implicit fallback.
        """
        if self._user_id is None:
            return self._shared_skill_key(name, category)
        return self._user_skill_key(name, category)

    async def _iter_user_skills(self) -> list[tuple[str, str]]:
        """Yield ``(skill_name, key_prefix)`` for every user-layer SKILL.md.

        Returns an empty list when this storage has no user scope
        (service-account sessions) so callers can loop over the result
        without repeating the ``_user_id is None`` guard.  Both
        category-bare and category-nested layouts are recognised:
        ``users/{uid}/skills/{name}/SKILL.md`` and
        ``users/{uid}/skills/{category}/{name}/SKILL.md``.
        """
        if self._user_id is None:
            return []
        prefix = f"users/{self._user_id}/skills/"
        keys = await self._backend.list_keys(self._bucket, prefix=prefix)
        found: list[tuple[str, str]] = []
        for key in keys:
            if not key.endswith("/SKILL.md"):
                continue
            parts = key.split("/")
            skill_name = parts[-2]
            key_prefix = "/".join(parts[:-1])
            found.append((skill_name, key_prefix))
        return found

    async def skill_exists(self, name: str) -> dict[str, Any] | None:
        """Find a skill by name across user, org-shared, and platform layers.

        Returns ``{"key_prefix": str, "layer": str}`` or ``None``.

        The bare-category layout (``{layer}/skills/{name}/SKILL.md``) is
        probed directly via :meth:`StorageBackend.exists` so the common
        case avoids listing every key under the layer.  The nested
        ``{layer}/skills/{category}/{name}/`` layout still requires a
        listing walk.
        """
        if self._user_id is not None:
            user_prefix = self._user_skill_key(name)
            if await self._backend.exists(self._bucket, f"{user_prefix}/SKILL.md"):
                return {"key_prefix": user_prefix, "layer": "user"}

        shared_prefix = self._shared_skill_key(name)
        if await self._backend.exists(self._bucket, f"{shared_prefix}/SKILL.md"):
            return {"key_prefix": shared_prefix, "layer": "org"}

        # Fall back to a listing walk for the category-nested layout.
        for skill_name, key_prefix in await self._iter_user_skills():
            if skill_name == name:
                return {"key_prefix": key_prefix, "layer": "user"}

        shared_keys = await self._backend.list_keys(
            self._bucket, prefix="shared/skills/",
        )
        for key in shared_keys:
            parts = key.split("/")
            if parts[-1] == "SKILL.md" and parts[-2] == name:
                return {"key_prefix": "/".join(parts[:-1]), "layer": "org"}

        return None

    async def read_skill(self, key_prefix: str) -> str:
        """Read a SKILL.md file."""
        return await self._backend.read_text(self._bucket, f"{key_prefix}/SKILL.md")

    async def write_skill(self, name: str, content: str, category: str | None = None) -> str:
        """Write a new skill and return its key prefix.

        Scope follows the owning context: user-scoped contexts write to
        ``users/{uid}/skills/...``; service-account contexts write to
        ``shared/skills/...`` because they have no per-user scope.  See
        :meth:`_default_skill_write_key`.
        """
        key_prefix = self._default_skill_write_key(name, category)
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
        Service-account sessions see the shared layer only.
        """
        skills: dict[str, dict[str, Any]] = {}

        # User layer (highest precedence).  Empty for service-account
        # contexts, which collapses this block to a no-op loop.
        for name, key_prefix in await self._iter_user_skills():
            if name not in skills:
                skills[name] = {
                    "name": name, "key_prefix": key_prefix, "layer": "user",
                }

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

    # ── Sub-agent types ─────────────────────────────────────────────

    def _shared_agent_key(self, name: str, category: str | None = None) -> str:
        """Build the key prefix for an org-shared sub-agent directory."""
        if category:
            return f"shared/agents/{category}/{name}"
        return f"shared/agents/{name}"

    def _user_agent_key(self, name: str, category: str | None = None) -> str:
        """Build the key prefix for a user-scoped sub-agent directory."""
        if self._user_id is None:
            raise ValueError(
                "_user_agent_key requires a user-scoped TenantStorage; "
                "use _shared_agent_key for service-account contexts."
            )
        if category:
            return f"users/{self._user_id}/agents/{category}/{name}"
        return f"users/{self._user_id}/agents/{name}"

    def _default_agent_write_key(
        self, name: str, category: str | None = None,
    ) -> str:
        """Where new sub-agents land: user scope when present, shared otherwise."""
        if self._user_id is None:
            return self._shared_agent_key(name, category)
        return self._user_agent_key(name, category)

    async def _iter_user_agents(self) -> list[tuple[str, str]]:
        """Yield ``(agent_name, key_prefix)`` for every user-layer AGENT.md."""
        if self._user_id is None:
            return []
        prefix = f"users/{self._user_id}/agents/"
        keys = await self._backend.list_keys(self._bucket, prefix=prefix)
        found: list[tuple[str, str]] = []
        for key in keys:
            if not key.endswith("/AGENT.md"):
                continue
            parts = key.split("/")
            name = parts[-2]
            key_prefix = "/".join(parts[:-1])
            found.append((name, key_prefix))
        return found

    async def agent_exists(self, name: str) -> dict[str, Any] | None:
        """Find a sub-agent by name across user and org-shared layers.

        Returns ``{"key_prefix": str, "layer": str}`` or ``None``.  The
        bare-category layout (``{layer}/agents/{name}/AGENT.md``) is
        probed via :meth:`StorageBackend.exists` for the common case;
        the nested ``{layer}/agents/{category}/{name}/`` layout needs
        a listing walk.
        """
        if self._user_id is not None:
            user_prefix = self._user_agent_key(name)
            if await self._backend.exists(self._bucket, f"{user_prefix}/AGENT.md"):
                return {"key_prefix": user_prefix, "layer": "user"}

        shared_prefix = self._shared_agent_key(name)
        if await self._backend.exists(self._bucket, f"{shared_prefix}/AGENT.md"):
            return {"key_prefix": shared_prefix, "layer": "org"}

        for agent_name, key_prefix in await self._iter_user_agents():
            if agent_name == name:
                return {"key_prefix": key_prefix, "layer": "user"}

        shared_keys = await self._backend.list_keys(
            self._bucket, prefix="shared/agents/",
        )
        for key in shared_keys:
            parts = key.split("/")
            if parts[-1] == "AGENT.md" and parts[-2] == name:
                return {"key_prefix": "/".join(parts[:-1]), "layer": "org"}

        return None

    async def read_agent(self, key_prefix: str) -> str:
        """Read an AGENT.md file."""
        return await self._backend.read_text(
            self._bucket, f"{key_prefix}/AGENT.md",
        )

    async def write_agent(
        self, name: str, content: str, category: str | None = None,
    ) -> str:
        """Write a new sub-agent and return its key prefix."""
        key_prefix = self._default_agent_write_key(name, category)
        await self._backend.write_text(
            self._bucket, f"{key_prefix}/AGENT.md", content,
        )
        return key_prefix

    async def overwrite_agent(self, key_prefix: str, content: str) -> None:
        """Overwrite an existing AGENT.md."""
        await self._backend.write_text(
            self._bucket, f"{key_prefix}/AGENT.md", content,
        )

    async def delete_agent(self, key_prefix: str) -> None:
        """Delete all files under a sub-agent's key prefix in parallel."""
        keys = await self._backend.list_keys(self._bucket, prefix=key_prefix)
        await asyncio.gather(
            *(self._backend.delete(self._bucket, k) for k in keys),
        )

    async def list_all_agents(self) -> list[dict[str, Any]]:
        """List sub-agents across user and org-shared layers.

        Returns entries with ``name``, ``key_prefix``, ``layer``.  Does
        not include platform agents (those live on the container
        filesystem).
        """
        agents: dict[str, dict[str, Any]] = {}

        for name, key_prefix in await self._iter_user_agents():
            if name not in agents:
                agents[name] = {
                    "name": name, "key_prefix": key_prefix, "layer": "user",
                }

        shared_keys = await self._backend.list_keys(
            self._bucket, prefix="shared/agents/",
        )
        for key in shared_keys:
            if key.endswith("/AGENT.md"):
                parts = key.split("/")
                name = parts[-2]
                if name not in agents:
                    prefix = "/".join(parts[:-1])
                    agents[name] = {
                        "name": name, "key_prefix": prefix, "layer": "org",
                    }

        return list(agents.values())

    # ── Memory ──────────────────────────────────────────────────────

    def _memory_key(self, filename: str) -> str:
        """Build key for a memory file.

        Service-account sessions (no user scope) route to
        ``shared/memory/{filename}`` — org-wide memory that persists
        across every SA-submitted session for the tenant.
        """
        if self._user_id is None:
            return f"shared/memory/{filename}"
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
