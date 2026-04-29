"""Bucket-routing helpers for the knowledge-base layer.

Two storage layouts coexist:

  - **Org KBs** live in ``tenant-{org_id}/shared/knowledge_bases/{kb_name}/...``.
  - **Platform KBs** live in a single shared bucket
    ``platform-shared/knowledge_bases/{kb_name}/...`` so platform content
    is never copied per tenant.

The discriminator is the ``kb.org_id`` column: ``NULL`` -> platform, set
-> the owning org. Callers pass it explicitly because the same KB name
can exist in both layers (a platform default + a per-org override).

This module owns the bucket / key construction so the rest of the code
never has to know the prefix scheme. Tests use the same helper to seed
content; production tools (kb_read, future kb_write, ingestors) use it
to read and write.
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from surogates.storage.backend import StorageBackend
from surogates.storage.tenant import tenant_bucket

logger = logging.getLogger(__name__)


#: Bucket name for platform-shared KBs (read-only to app roles via RLS in
#: Postgres; bucket-level ACLs at the storage backend itself control
#: write access — only the migration / admin role can write).
PLATFORM_BUCKET: str = "platform-shared"


class KbStorage:
    """Tenant-aware KB object I/O.

    Wraps a :class:`StorageBackend` with helpers that know the per-KB
    bucket and key layout.  No tenant authorization is enforced here —
    callers must supply the correct ``org_id`` derived from the KB row.
    Authorization is enforced one layer up (RLS on ``kb`` rows + the
    explicit org-or-platform filter in :class:`KbStore`).
    """

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    # ── Path construction ────────────────────────────────────────────

    @staticmethod
    def bucket_for(kb_org_id: UUID | None) -> str:
        """Return the bucket name for a KB.

        ``kb_org_id`` must be the value from ``kb.org_id`` on the row —
        ``None`` for platform KBs, the org UUID otherwise.
        """
        if kb_org_id is None:
            return PLATFORM_BUCKET
        return tenant_bucket(kb_org_id)

    @staticmethod
    def key_for(
        kb_org_id: UUID | None,
        kb_name: str,
        path: str,
    ) -> str:
        """Return the object key for an entry under a KB.

        Org KBs nest under ``shared/knowledge_bases/{kb_name}/`` so they
        live alongside the existing ``shared/skills/`` and ``shared/
        agents/`` prefixes inside the tenant bucket. Platform KBs sit
        directly under ``knowledge_bases/{kb_name}/`` because the whole
        bucket is platform-shared and there's no other prefix to share
        space with.
        """
        path = path.lstrip("/")
        if kb_org_id is None:
            return f"knowledge_bases/{kb_name}/{path}"
        return f"shared/knowledge_bases/{kb_name}/{path}"

    # ── I/O ──────────────────────────────────────────────────────────

    async def ensure_bucket(self, kb_org_id: UUID | None) -> None:
        """Create the destination bucket if it does not exist.

        Idempotent. Production deployments typically pre-create the
        platform bucket as part of cluster setup; the per-tenant bucket
        is created on-demand the first time a tenant uses storage.
        """
        bucket = self.bucket_for(kb_org_id)
        if not await self._backend.bucket_exists(bucket):
            await self._backend.create_bucket(bucket)

    async def write_entry(
        self,
        kb_org_id: UUID | None,
        kb_name: str,
        path: str,
        data: bytes,
    ) -> None:
        """Write ``data`` to the entry at ``path`` within the KB.

        Overwrites any existing object. Used by ingest workers and the
        wiki maintainer; not exposed through ``kb_write`` to the agent
        directly (that tool is policy-locked to the maintainer
        sub-agent in a later step).
        """
        await self.ensure_bucket(kb_org_id)
        await self._backend.write(
            self.bucket_for(kb_org_id),
            self.key_for(kb_org_id, kb_name, path),
            data,
        )

    async def read_entry(
        self,
        kb_org_id: UUID | None,
        kb_name: str,
        path: str,
    ) -> Optional[bytes]:
        """Read the entry at ``path``.

        Returns ``None`` if the object is missing (key not present, or
        bucket not yet created). All other backend errors propagate so
        infrastructure failures don't masquerade as "not found".
        """
        try:
            return await self._backend.read(
                self.bucket_for(kb_org_id),
                self.key_for(kb_org_id, kb_name, path),
            )
        except KeyError:
            return None
        except FileNotFoundError:
            # LocalBackend may raise this if the bucket dir is missing
            # (i.e. nothing has ever been written to it). Treat as
            # missing-object semantics.
            return None

    async def delete_entry(
        self,
        kb_org_id: UUID | None,
        kb_name: str,
        path: str,
    ) -> None:
        """Delete the object at ``path``. No-op if it doesn't exist."""
        await self._backend.delete(
            self.bucket_for(kb_org_id),
            self.key_for(kb_org_id, kb_name, path),
        )
