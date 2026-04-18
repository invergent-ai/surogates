"""Skill asset staging — materialises a skill's supporting files into the
session workspace so the sandbox can read and execute them directly.

Rationale
---------

The ``skill_view`` tool returns SKILL.md content plus a list of linked files
(``scripts/``, ``assets/``, ``templates/``, ``references/``).  Without staging,
the LLM has to serialise each file through its context window using a
follow-up ``skill_view(..., file_path=...)`` call — which burns tokens for
text files and outright fails for binary files like ``.pptx`` templates.

With staging, the API server copies the entire skill tree from its source
(platform filesystem or tenant bucket) into ``session-{session_id}/.skills/
{name}/`` as a side effect of ``skill_view``.  The sandbox then sees the files
at ``{workspace_path}/.skills/{name}/`` via s3fs-fuse (prod) or via the
LocalBackend directory (dev), and the LLM runs them by relative path.

The staging is idempotent: a ``.staged`` marker file inside the staged tree
short-circuits re-uploads on subsequent ``skill_view`` calls within the same
session.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Final
from uuid import UUID

from surogates.storage.backend import StorageBackend
from surogates.storage.tenant import session_bucket

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


#: Directory (relative to the session workspace root) where staged skills live.
STAGING_DIR: Final[str] = ".skills"

#: Zero-byte marker written once a skill has been fully staged.
STAGING_MARKER: Final[str] = ".staged"

#: Redis key prefix for per-skill staging locks.  Scoped by session_id so two
#: sessions staging the same skill never contend; scoped by skill_name so a
#: single session can stage different skills in parallel.
_LOCK_KEY_PREFIX: Final[str] = "surogates:skill-stage:"

#: How long a lock is held before Redis auto-expires it.  Chosen to exceed
#: staging time for even large skills while bounding how long a crashed
#: worker can block successors.
_LOCK_TIMEOUT_SECONDS: Final[int] = 60

#: How long a contending caller waits for the lock before giving up.  On
#: timeout, the caller raises — the alternative would be to silently skip
#: staging and return an unstaged path, which would confuse the LLM.
_LOCK_BLOCKING_TIMEOUT_SECONDS: Final[int] = 60


class SkillStager:
    """Stages skill trees into a session's workspace bucket.

    Parameters
    ----------
    backend:
        Storage backend used for all bucket I/O.  Must have access to both
        the tenant bucket (for tenant-bucket-backed skills) and the session
        bucket (for the staging writes).
    redis:
        Optional async Redis client used for a cross-worker lock keyed on
        ``(session_id, skill_name)``.  When provided, concurrent
        ``skill_view`` calls racing to stage the same skill are
        serialised: only one worker writes, the others wait for the
        ``.staged`` marker and return the already-staged path.  When
        ``None`` (dev / tests without Redis), an in-process
        ``asyncio.Lock`` per key provides single-worker safety.
    """

    def __init__(
        self,
        backend: StorageBackend,
        redis: "Redis | None" = None,
    ) -> None:
        self._backend = backend
        self._redis = redis
        # In-process fallback locks — only used when Redis isn't available.
        # Keyed by the same string as the Redis lock so the two code paths
        # produce identical contention semantics.
        self._local_locks: dict[str, asyncio.Lock] = {}
        self._local_locks_guard = asyncio.Lock()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def staged_key_prefix(skill_name: str) -> str:
        """Return the key prefix inside the session bucket for *skill_name*."""
        return f"{STAGING_DIR}/{skill_name}"

    def workspace_path_for(self, session_id: UUID | str, skill_name: str) -> str:
        """Return the workspace-visible path where *skill_name* is staged.

        In production (``S3Backend``) this resolves to
        ``/workspace/.skills/{name}/`` because the session bucket is mounted
        at ``/workspace``.  In development (``LocalBackend``) this resolves
        to ``{base_path}/session-{session_id}/.skills/{name}/``.
        """
        root = self._backend.resolve_bucket_path(session_bucket(session_id))
        return f"{root.rstrip('/')}/{STAGING_DIR}/{skill_name}/"

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    async def is_staged(self, session_id: UUID | str, skill_name: str) -> bool:
        """Return ``True`` if *skill_name* has already been staged for this session."""
        bucket = session_bucket(session_id)
        marker = f"{self.staged_key_prefix(skill_name)}/{STAGING_MARKER}"
        return await self._backend.exists(bucket, marker)

    # ------------------------------------------------------------------
    # Cross-worker lock (Redis) / single-worker lock (asyncio fallback)
    # ------------------------------------------------------------------

    @staticmethod
    def _lock_key(session_id: UUID | str, skill_name: str) -> str:
        """Return the Redis key used to serialise staging for this skill."""
        return f"{_LOCK_KEY_PREFIX}{session_id}:{skill_name}"

    @asynccontextmanager
    async def _stage_lock(
        self, session_id: UUID | str, skill_name: str,
    ) -> AsyncIterator[None]:
        """Hold the per-skill staging lock for the duration of the ``with`` body.

        Redis-backed when a client was supplied; otherwise an in-process
        ``asyncio.Lock`` is used.  Callers must re-check
        :meth:`is_staged` *after* acquiring — the double-check is what
        makes concurrent callers collapse onto a single copy operation.
        """
        key = self._lock_key(session_id, skill_name)
        if self._redis is not None:
            lock = self._redis.lock(
                key,
                timeout=_LOCK_TIMEOUT_SECONDS,
                blocking=True,
                blocking_timeout=_LOCK_BLOCKING_TIMEOUT_SECONDS,
            )
            acquired = await lock.acquire()
            if not acquired:
                raise TimeoutError(
                    f"Timed out waiting for staging lock on '{skill_name}' "
                    f"for session {session_id}",
                )
            try:
                yield
            finally:
                # release() raises if the token expired in-flight — swallow
                # to avoid masking the real exception when the body fails.
                try:
                    await lock.release()
                except Exception:
                    logger.warning(
                        "Failed to release staging lock for %s:%s "
                        "(lock may have expired)",
                        session_id, skill_name, exc_info=True,
                    )
        else:
            async with self._local_locks_guard:
                local_lock = self._local_locks.setdefault(key, asyncio.Lock())
            async with local_lock:
                yield

    # ------------------------------------------------------------------
    # Staging from a filesystem source (platform skills)
    # ------------------------------------------------------------------

    async def stage_from_filesystem(
        self,
        session_id: UUID | str,
        skill_name: str,
        source_dir: Path,
    ) -> str:
        """Copy a platform skill's directory tree into the session bucket.

        All regular files under *source_dir* are copied to
        ``session-{session_id}/.skills/{skill_name}/<relpath>``.  A
        ``.staged`` marker is written last to signal completion.

        Returns the workspace-visible path where the skill is staged.

        Concurrent callers for the same ``(session_id, skill_name)`` are
        serialised via :meth:`_stage_lock`; all but the first find the
        marker on re-check and skip the copy.
        """
        # Fast path: no lock needed when already staged.
        if await self.is_staged(session_id, skill_name):
            return self.workspace_path_for(session_id, skill_name)

        if not source_dir.is_dir():
            raise FileNotFoundError(f"Skill source directory not found: {source_dir}")

        async with self._stage_lock(session_id, skill_name):
            # Double-checked: another caller may have staged while we waited.
            if await self.is_staged(session_id, skill_name):
                return self.workspace_path_for(session_id, skill_name)

            bucket = session_bucket(session_id)
            dest_prefix = self.staged_key_prefix(skill_name)

            copied = 0
            for src_file in sorted(source_dir.rglob("*")):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(source_dir).as_posix()
                data = src_file.read_bytes()
                await self._backend.write(bucket, f"{dest_prefix}/{rel}", data)
                copied += 1

            await self._backend.write(
                bucket, f"{dest_prefix}/{STAGING_MARKER}", b"",
            )
            logger.info(
                "Staged platform skill '%s' (%d files) for session %s",
                skill_name, copied, session_id,
            )
            return self.workspace_path_for(session_id, skill_name)

    # ------------------------------------------------------------------
    # Staging from a tenant bucket (user / org-shared file skills)
    # ------------------------------------------------------------------

    async def stage_from_tenant_bucket(
        self,
        session_id: UUID | str,
        skill_name: str,
        tenant_bucket_name: str,
        source_prefix: str,
    ) -> str:
        """Copy a tenant-bucket-backed skill into the session bucket.

        Reads every object under *source_prefix* in *tenant_bucket_name*
        and rewrites it under
        ``session-{session_id}/.skills/{skill_name}/<relpath>``.  A
        ``.staged`` marker is written last.

        Concurrent callers for the same ``(session_id, skill_name)`` are
        serialised — see :meth:`stage_from_filesystem` for the locking
        contract.
        """
        # Fast path: no lock needed when already staged.
        if await self.is_staged(session_id, skill_name):
            return self.workspace_path_for(session_id, skill_name)

        async with self._stage_lock(session_id, skill_name):
            # Double-checked: another caller may have staged while we waited.
            if await self.is_staged(session_id, skill_name):
                return self.workspace_path_for(session_id, skill_name)

            dest_bucket = session_bucket(session_id)
            dest_prefix = self.staged_key_prefix(skill_name)
            normalized_src = source_prefix.rstrip("/")
            strip_len = len(normalized_src) + 1  # +1 for the trailing "/"

            src_keys = await self._backend.list_keys(
                tenant_bucket_name, prefix=f"{normalized_src}/",
            )

            copied = 0
            for key in src_keys:
                if len(key) < strip_len:
                    continue
                rel = key[strip_len:]
                if not rel:
                    continue
                data = await self._backend.read(tenant_bucket_name, key)
                await self._backend.write(dest_bucket, f"{dest_prefix}/{rel}", data)
                copied += 1

            await self._backend.write(
                dest_bucket, f"{dest_prefix}/{STAGING_MARKER}", b"",
            )
            logger.info(
                "Staged tenant skill '%s' (%d files) for session %s",
                skill_name, copied, session_id,
            )
            return self.workspace_path_for(session_id, skill_name)

    # ------------------------------------------------------------------
    # Binary file lookup within a staged skill
    # ------------------------------------------------------------------

    def staged_file_path(
        self,
        session_id: UUID | str,
        skill_name: str,
        file_path: str,
    ) -> str:
        """Return the workspace-visible path for a single file within a staged skill."""
        return self.workspace_path_for(session_id, skill_name) + file_path.lstrip("/")


def has_stageable_assets(linked_files: dict[str, list[str]] | list[str] | None) -> bool:
    """Return ``True`` if a skill has any supporting file worth staging.

    Accepts either the dict form (``{"scripts": [...], "assets": [...]}``)
    used by the in-process handler or the flat list form used by the API
    detail response.
    """
    if not linked_files:
        return False
    if isinstance(linked_files, dict):
        return any(linked_files.values())
    return bool(linked_files)
