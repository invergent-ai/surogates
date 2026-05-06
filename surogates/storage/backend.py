"""Storage backend abstraction — local filesystem and S3-compatible.

The ``StorageBackend`` protocol defines the contract for all storage
operations.  Two concrete implementations are provided:

- ``LocalBackend`` — maps ``(bucket, key)`` to ``{base_path}/{bucket}/{key}``
  on the local filesystem.  Used for development.
- ``S3Backend`` — talks to Garage / MinIO / AWS S3 via ``aioboto3``.  Used in
  production K8s deployments.

A factory function ``create_backend`` returns the right implementation
based on :class:`~surogates.storage.settings.StorageSettings`.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Async storage backend for bucket-based object storage."""

    # ── Bucket lifecycle ────────────────────────────────────────────

    async def create_bucket(self, bucket: str) -> None:
        """Create a new bucket.  No-op if it already exists."""
        ...

    async def delete_bucket(self, bucket: str) -> None:
        """Delete a bucket and all its contents."""
        ...

    async def bucket_exists(self, bucket: str) -> bool:
        """Return True if the bucket exists."""
        ...

    # ── Object operations ───────────────────────────────────────────

    async def read(self, bucket: str, key: str) -> bytes:
        """Read an object.  Raises ``KeyError`` if not found."""
        ...

    async def read_text(self, bucket: str, key: str, encoding: str = "utf-8") -> str:
        """Read an object as text.  Raises ``KeyError`` if not found."""
        ...

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        """Write (or overwrite) an object."""
        ...

    async def write_text(
        self, bucket: str, key: str, text: str, encoding: str = "utf-8",
    ) -> None:
        """Write (or overwrite) an object as text."""
        ...

    async def exists(self, bucket: str, key: str) -> bool:
        """Return True if the object exists."""
        ...

    async def delete(self, bucket: str, key: str) -> None:
        """Delete an object.  No-op if it doesn't exist."""
        ...

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        """List object keys under *prefix*.  Returns relative keys."""
        ...

    async def stat(self, bucket: str, key: str) -> dict[str, Any]:
        """Return metadata for an object (size, etc.).

        Raises ``KeyError`` if not found.
        """
        ...

    async def list_buckets(self, prefix: str = "") -> list[str]:
        """List bucket names, optionally filtered by prefix."""
        ...

    def resolve_bucket_path(self, bucket: str) -> str:
        """Return the filesystem path for a bucket.

        Only meaningful for ``LocalBackend`` — returns the directory
        path.  ``S3Backend`` returns the bucket name (used as the
        s3fs-fuse mount source).
        """
        ...

    def resolve_workspace_path(self, bucket: str, session_id: str) -> str:
        """Return the workspace path visible to tools for a session."""
        ...


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------


class LocalBackend:
    """Maps ``(bucket, key)`` to ``{base_path}/{bucket}/{key}`` on the
    local filesystem.

    This preserves the directory layout used by ``ResourceLoader``,
    ``MemoryStore``, and workspace file APIs, so existing code works
    unmodified during development.
    """

    def __init__(self, base_path: str) -> None:
        self._base = Path(base_path)

    def _resolve(self, bucket: str, key: str) -> Path:
        """Build an absolute path, rejecting traversal attempts."""
        path = (self._base / bucket / key).resolve()
        bucket_root = (self._base / bucket).resolve()
        if not path.is_relative_to(bucket_root):
            raise ValueError(f"Path traversal denied: {key}")
        return path

    def _bucket_path(self, bucket: str) -> Path:
        return (self._base / bucket).resolve()

    # ── Bucket lifecycle ────────────────────────────────────────────

    async def create_bucket(self, bucket: str) -> None:
        self._bucket_path(bucket).mkdir(parents=True, exist_ok=True)

    async def delete_bucket(self, bucket: str) -> None:
        path = self._bucket_path(bucket)
        if path.is_dir():
            shutil.rmtree(path)

    async def bucket_exists(self, bucket: str) -> bool:
        return self._bucket_path(bucket).is_dir()

    # ── Object operations ───────────────────────────────────────────

    async def read(self, bucket: str, key: str) -> bytes:
        path = self._resolve(bucket, key)
        if not path.is_file():
            raise KeyError(f"{bucket}/{key}")
        return path.read_bytes()

    async def read_text(self, bucket: str, key: str, encoding: str = "utf-8") -> str:
        path = self._resolve(bucket, key)
        if not path.is_file():
            raise KeyError(f"{bucket}/{key}")
        return path.read_text(encoding=encoding)

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        path = self._resolve(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(path, data)

    async def write_text(
        self, bucket: str, key: str, text: str, encoding: str = "utf-8",
    ) -> None:
        path = self._resolve(bucket, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, text, encoding)

    async def exists(self, bucket: str, key: str) -> bool:
        return self._resolve(bucket, key).is_file()

    async def delete(self, bucket: str, key: str) -> None:
        path = self._resolve(bucket, key)
        if path.is_file():
            path.unlink()
            # Clean up empty parent directories up to the bucket root.
            bucket_root = self._bucket_path(bucket)
            parent = path.parent
            while parent != bucket_root and parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        bucket_root = self._bucket_path(bucket)
        search_root = bucket_root / prefix if prefix else bucket_root
        if not search_root.is_dir():
            return []
        keys: list[str] = []
        for path in search_root.rglob("*"):
            if path.is_file():
                keys.append(str(path.relative_to(bucket_root)))
        keys.sort()
        return keys

    async def stat(self, bucket: str, key: str) -> dict[str, Any]:
        path = self._resolve(bucket, key)
        if not path.is_file():
            raise KeyError(f"{bucket}/{key}")
        st = path.stat()
        return {"size": st.st_size, "modified": st.st_mtime}

    async def list_buckets(self, prefix: str = "") -> list[str]:
        if not self._base.is_dir():
            return []
        return sorted(
            d.name for d in self._base.iterdir()
            if d.is_dir() and d.name.startswith(prefix)
        )

    def resolve_bucket_path(self, bucket: str) -> str:
        return str(self._bucket_path(bucket))

    def resolve_workspace_path(self, bucket: str, session_id: str) -> str:
        path = (self._bucket_path(bucket) / "sessions" / str(session_id)).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


# ---------------------------------------------------------------------------
# S3Backend (stub — implemented in Phase 4)
# ---------------------------------------------------------------------------


class S3Backend:
    """S3-compatible backend using aioboto3 (Garage / MinIO / AWS S3).

    Each bucket maps to an S3 bucket.  Keys are S3 object keys.
    The ``aioboto3.Session`` is created once and reused across calls.
    """

    def __init__(
        self,
        endpoint: str,
        access_key: str = "",
        secret_key: str = "",
        region: str = "",
    ) -> None:
        import aioboto3

        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region or "garage"
        self._session = aioboto3.Session()

    def _session_kwargs(self) -> dict[str, Any]:
        """Build kwargs for aioboto3 client creation."""
        kwargs: dict[str, Any] = {
            "endpoint_url": self._endpoint,
            "region_name": self._region,
        }
        if self._access_key:
            kwargs["aws_access_key_id"] = self._access_key
            kwargs["aws_secret_access_key"] = self._secret_key
        return kwargs

    def _client(self):
        """Return an async context manager for an S3 client."""
        return self._session.client("s3", **self._session_kwargs())

    # ── Bucket lifecycle ────────────────────────────────────────────

    async def create_bucket(self, bucket: str) -> None:
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=bucket)
            except s3.exceptions.ClientError:
                await s3.create_bucket(Bucket=bucket)

    async def delete_bucket(self, bucket: str) -> None:
        async with self._client() as s3:
            # Delete all objects first (S3 requires empty bucket for deletion).
            try:
                paginator = s3.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=bucket):
                    objects = page.get("Contents", [])
                    if objects:
                        await s3.delete_objects(
                            Bucket=bucket,
                            Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                        )
                await s3.delete_bucket(Bucket=bucket)
            except s3.exceptions.NoSuchBucket:
                pass
            except Exception:
                # ClientError for NoSuchBucket varies by provider.
                logger.warning("Failed to delete bucket %s", bucket, exc_info=True)

    async def bucket_exists(self, bucket: str) -> bool:
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=bucket)
                return True
            except Exception:
                return False

    # ── Object operations ───────────────────────────────────────────

    async def read(self, bucket: str, key: str) -> bytes:
        async with self._client() as s3:
            try:
                resp = await s3.get_object(Bucket=bucket, Key=key)
                return await resp["Body"].read()
            except s3.exceptions.NoSuchKey:
                raise KeyError(f"{bucket}/{key}")
            except Exception as exc:
                if "NoSuchKey" in str(exc) or "404" in str(exc):
                    raise KeyError(f"{bucket}/{key}") from exc
                raise

    async def read_text(self, bucket: str, key: str, encoding: str = "utf-8") -> str:
        data = await self.read(bucket, key)
        return data.decode(encoding)

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        async with self._client() as s3:
            await s3.put_object(Bucket=bucket, Key=key, Body=data)

    async def write_text(
        self, bucket: str, key: str, text: str, encoding: str = "utf-8",
    ) -> None:
        await self.write(bucket, key, text.encode(encoding))

    async def exists(self, bucket: str, key: str) -> bool:
        async with self._client() as s3:
            try:
                await s3.head_object(Bucket=bucket, Key=key)
                return True
            except Exception:
                return False

    async def delete(self, bucket: str, key: str) -> None:
        async with self._client() as s3:
            try:
                await s3.delete_object(Bucket=bucket, Key=key)
            except Exception:
                pass  # Idempotent — no error if key doesn't exist.

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        import aioboto3
        session = aioboto3.Session()
        keys: list[str] = []
        async with session.client("s3", **self._session_kwargs()) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            kwargs: dict[str, Any] = {"Bucket": bucket}
            if prefix:
                kwargs["Prefix"] = prefix
            async for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
        keys.sort()
        return keys

    async def stat(self, bucket: str, key: str) -> dict[str, Any]:
        async with self._client() as s3:
            try:
                resp = await s3.head_object(Bucket=bucket, Key=key)
                return {
                    "size": resp.get("ContentLength", 0),
                    "modified": resp.get("LastModified"),
                }
            except Exception as exc:
                if "404" in str(exc) or "NoSuchKey" in str(exc):
                    raise KeyError(f"{bucket}/{key}") from exc
                raise

    async def list_buckets(self, prefix: str = "") -> list[str]:
        async with self._client() as s3:
            resp = await s3.list_buckets()
            return sorted(
                b["Name"] for b in resp.get("Buckets", [])
                if b["Name"].startswith(prefix)
            )

    def resolve_bucket_path(self, bucket: str) -> str:
        # S3 buckets are mounted inside sandbox pods, not directly on the
        # API server filesystem.
        return "/workspace"

    def resolve_workspace_path(self, bucket: str, session_id: str) -> str:
        return "/workspace"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_backend(settings: Any) -> StorageBackend:
    """Create a ``StorageBackend`` from application settings.

    Reads ``settings.storage.backend`` to select the implementation:
    - ``"local"`` → ``LocalBackend``
    - ``"s3"`` → ``S3Backend``
    """
    storage = getattr(settings, "storage", None)
    if storage is None:
        # Fallback: no storage config → local backend with default path.
        return LocalBackend(base_path=getattr(settings, "tenant_assets_root", "/tmp/surogates/tenant-assets"))

    backend = getattr(storage, "backend", "local")
    if backend == "s3":
        return S3Backend(
            endpoint=storage.endpoint,
            access_key=storage.access_key,
            secret_key=storage.secret_key,
            region=getattr(storage, "region", ""),
        )

    if backend == "local":
        base = getattr(storage, "base_path", "") or getattr(settings, "tenant_assets_root", "/tmp/surogates/tenant-assets")
        return LocalBackend(base_path=base)

    raise ValueError(f"Unknown storage backend: '{backend}'. Use 'local' or 's3'.")


# ---------------------------------------------------------------------------
# Atomic write helpers
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write *data* to *path* using temp file + os.replace."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.tmp.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Atomically write *text* to *path* using temp file + os.replace."""
    _atomic_write_bytes(path, text.encode(encoding))
