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

import asyncio
import contextlib
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

#: How much of an object a streamed read holds at once, and the size of each part of a streamed write.
_CHUNK = 1 << 20
_PART = 8 << 20


class TooLarge(ValueError):
    """An object holds more than its reader said it would take."""


class Changed(Exception):
    """The object is no longer the one its writer saw: nothing was written."""


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

    async def mark(self, bucket: str, key: str) -> Any:
        """Write an empty object at *key* and return the date the store gives it, as :meth:`stat` would.

        For a caller that needs the store's own time, and writes where
        what is stored is not its own to trust: never through a link, at
        the key or at a folder above it, and over nothing but a plain
        file.  Raises ``ValueError`` for a key it will not write.
        """
        ...

    async def download(self, bucket: str, key: str, target: Path, *, limit: int | None = None) -> int:
        """Read an object into the file *target*, a piece at a time; its size.

        For an object that may be large: none of it is held whole.  Raises
        ``KeyError`` if not found, and :class:`TooLarge`, with *target*
        removed, once it holds more than *limit* bytes.
        """
        ...

    async def upload(
        self, bucket: str, key: str, source: Path, *, if_tag: str | None = None, if_absent: bool = False,
    ) -> None:
        """Write (or overwrite) an object from the file *source*, a piece at a time.

        For an object that may be large: none of it is held whole.  The
        object is the old one or the new one whole, never part of each.
        With *if_tag*, the ``etag`` :meth:`stat` gave the object, or with
        *if_absent*, the write is for the object its writer saw: one
        changed since, made or gone, raises :class:`Changed` and is left as
        it is, where the store can tell.  ``LocalBackend`` tells, up to the
        moment it puts the new file in place; ``S3Backend`` writes as asked
        whatever the object is now.
        """
        ...

    async def exists(self, bucket: str, key: str) -> bool:
        """Return True if the object exists."""
        ...

    async def delete(self, bucket: str, key: str) -> None:
        """Delete an object.  No-op if it doesn't exist."""
        ...

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        """Delete every object whose key starts with *prefix*.

        Bulk equivalent of ``list_keys`` + per-key ``delete``.  On S3 this
        uses ``delete_objects`` (up to 1000 keys per call); on local FS
        it rmtree's the prefix directory.  Returns the number of objects
        deleted.
        """
        ...

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        """List object keys under *prefix*.  Returns relative keys."""
        ...

    async def list_entries(self, bucket: str, prefix: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        """List objects under *prefix* with metadata.

        Each entry is ``{"key": str, "modified": datetime|float, "size": int}``,
        sorted by ``key``.  ``modified`` follows the same convention as
        :meth:`stat` (boto3 ``datetime`` on S3, POSIX float locally).

        With *limit* the listing stops at that many entries, whichever they
        are: for a caller that must not read a folder of any size, and asks
        for one more than it will take to learn that there are more.  A
        store with folders of its own counts each as an entry then, its key
        ending in ``/``, as a bucket counts a folder's marker.

        Backends should populate this from their native list response —
        ``list_objects_v2`` already returns ``LastModified``/``Size`` for
        every entry, so this avoids a per-key ``head_object`` round trip
        when callers need both keys and metadata.
        """
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

    async def mark(self, bucket: str, key: str) -> float:
        # A bucket has no links; a folder on a disk can hold one, put there by whoever writes
        # the folder.  So each step is opened by the handle of the one above and never through
        # a link, and nothing is resolved by name.
        *folders, name = key.split("/")
        if not name or any(part in ("", ".", "..") for part in (*folders, name)):
            raise ValueError(f"No mark at {key!r}: not a key inside the bucket")
        opened = [os.open(self._bucket_path(bucket), os.O_RDONLY | os.O_DIRECTORY)]
        try:
            for folder in folders:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(folder, dir_fd=opened[-1])
                opened.append(os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=opened[-1]))
            folder_fd = opened[-1]
            with contextlib.suppress(FileNotFoundError):
                if not stat.S_ISREG(os.stat(name, dir_fd=folder_fd, follow_symlinks=False).st_mode):
                    raise ValueError(f"No mark at {key!r}: what is there is not a plain file")
            # A new file put in its place, never the old one written: a plain file there can be
            # another file's second name, and writing it would empty both.
            staged = f".{name}.{os.urandom(4).hex()}.mark"
            opened.append(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=folder_fd))
            dated = os.fstat(opened[-1]).st_mtime
            try:
                os.replace(staged, name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(staged, dir_fd=folder_fd)
                raise
            return dated
        except OSError as exc:
            raise ValueError(f"No mark at {key!r}: {exc.strerror or exc}") from exc
        finally:
            for fd in reversed(opened):
                os.close(fd)

    async def download(self, bucket: str, key: str, target: Path, *, limit: int | None = None) -> int:
        path = self._resolve(bucket, key)
        if not path.is_file():
            raise KeyError(f"{bucket}/{key}")
        return await asyncio.to_thread(_copy, path, target, limit, f"{bucket}/{key}")

    async def upload(
        self, bucket: str, key: str, source: Path, *, if_tag: str | None = None, if_absent: bool = False,
    ) -> None:
        await asyncio.to_thread(_atomic_copy, source, self._resolve(bucket, key), if_tag, if_absent)

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

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")
        bucket_root = self._bucket_path(bucket)
        target = (bucket_root / prefix).resolve()
        if not target.is_relative_to(bucket_root):
            raise ValueError(f"Path traversal denied: {prefix}")
        if not target.is_dir():
            return 0
        deleted = sum(1 for p in target.rglob("*") if p.is_file())
        shutil.rmtree(target)
        # Match ``delete``: walk up empty parents to the bucket root.
        parent = target.parent
        while parent != bucket_root and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        return deleted

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        return [entry["key"] for entry in await self.list_entries(bucket, prefix)]

    async def list_entries(self, bucket: str, prefix: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        bucket_root = self._bucket_path(bucket)
        search_root = bucket_root / prefix if prefix else bucket_root
        if not search_root.is_dir():
            return []
        entries: list[dict[str, Any]] = []
        for path in search_root.rglob("*"):
            if limit is not None and len(entries) >= limit:
                break
            folder = not path.is_file()
            if folder and (limit is None or not path.is_dir()):
                continue
            st = path.stat()
            entries.append({
                # Bounded, a folder is an entry too, named as a bucket names a folder's marker: a
                # folder of folders is not walked whole, and the caller learns there is more.
                "key": str(path.relative_to(bucket_root)) + ("/" if folder else ""),
                "modified": st.st_mtime,
                "size": 0 if folder else st.st_size,
            })
        entries.sort(key=lambda e: e["key"])
        return entries

    async def stat(self, bucket: str, key: str) -> dict[str, Any]:
        path = self._resolve(bucket, key)
        if not path.is_file():
            raise KeyError(f"{bucket}/{key}")
        st = path.stat()
        return {"size": st.st_size, "modified": st.st_mtime, "etag": _tag(st)}

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

    async def mark(self, bucket: str, key: str) -> Any:
        # A bucket has no links and no folders: a key is an object or nothing.
        async with self._client() as s3:
            await s3.put_object(Bucket=bucket, Key=key, Body=b"")
            return (await s3.head_object(Bucket=bucket, Key=key)).get("LastModified")

    async def write_text(
        self, bucket: str, key: str, text: str, encoding: str = "utf-8",
    ) -> None:
        await self.write(bucket, key, text.encode(encoding))

    async def download(self, bucket: str, key: str, target: Path, *, limit: int | None = None) -> int:
        async with self._client() as s3:
            try:
                resp = await s3.get_object(Bucket=bucket, Key=key)
            except s3.exceptions.NoSuchKey:
                raise KeyError(f"{bucket}/{key}")
            except Exception as exc:
                if "NoSuchKey" in str(exc) or "404" in str(exc):
                    raise KeyError(f"{bucket}/{key}") from exc
                raise
            written, body = 0, resp["Body"]
            try:
                with open(target, "wb") as out:
                    async for chunk in body.iter_chunks(_CHUNK):
                        written += len(chunk)
                        if limit is not None and written > limit:
                            raise TooLarge(f"{bucket}/{key}")
                        await asyncio.to_thread(out.write, chunk)
            except BaseException:
                Path(target).unlink(missing_ok=True)
                raise
            finally:
                # Let go whatever ended the read: an object past its limit is not read to its end.
                body.close()
            return written

    async def upload(
        self, bucket: str, key: str, source: Path, *, if_tag: str | None = None, if_absent: bool = False,
    ) -> None:
        # Unconditional: the client's upload of parts passes no condition on, and whether a store honours
        # one on a write, Garage among them, is not known here.
        from boto3.s3.transfer import TransferConfig

        async with self._client() as s3:
            # In parts, each read from the file as the last are sent: two on their way and two waiting, at most.
            parts = TransferConfig(multipart_chunksize=_PART, max_concurrency=2, max_io_queue=2)
            await s3.upload_file(str(source), bucket, key, Config=parts)

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

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")
        deleted = 0
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            batch: list[dict[str, str]] = []
            async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    batch.append({"Key": obj["Key"]})
                    if len(batch) >= 1000:
                        await s3.delete_objects(
                            Bucket=bucket,
                            Delete={"Objects": batch, "Quiet": True},
                        )
                        deleted += len(batch)
                        batch = []
            if batch:
                await s3.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": batch, "Quiet": True},
                )
                deleted += len(batch)
        return deleted

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        return [entry["key"] for entry in await self.list_entries(bucket, prefix)]

    async def list_entries(self, bucket: str, prefix: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        async with self._client() as s3:
            paginator = s3.get_paginator("list_objects_v2")
            kwargs: dict[str, Any] = {"Bucket": bucket}
            if prefix:
                kwargs["Prefix"] = prefix
            async for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    entries.append({
                        "key": obj["Key"],
                        "modified": obj.get("LastModified"),
                        "size": obj.get("Size", 0),
                    })
                if limit is not None and len(entries) >= limit:
                    # No page is asked for past the limit.
                    break
        entries = entries if limit is None else entries[:limit]
        entries.sort(key=lambda e: e["key"])
        return entries

    async def stat(self, bucket: str, key: str) -> dict[str, Any]:
        async with self._client() as s3:
            try:
                resp = await s3.head_object(Bucket=bucket, Key=key)
                return {
                    "size": resp.get("ContentLength", 0),
                    "modified": resp.get("LastModified"),
                    # What the store names the object's contents by: two writes in one second differ by it.
                    "etag": resp.get("ETag"),
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


def _copy(source: Path, target: Path, limit: int | None, name: str) -> int:
    """Copy *source* to *target* a piece at a time; its size.  :class:`TooLarge`, with *target* removed, past *limit*."""
    written = 0
    try:
        with open(source, "rb") as src, open(target, "wb") as out:
            while chunk := src.read(_CHUNK):
                written += len(chunk)
                if limit is not None and written > limit:
                    raise TooLarge(name)
                out.write(chunk)
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise
    return written


def _tag(st: os.stat_result) -> str:
    """What tells one write of a file from another: a file written anew has another inode, and one written in place another time."""
    return f"{st.st_ino}-{st.st_size}-{st.st_mtime_ns}"


def _atomic_copy(source: Path, path: Path, if_tag: str | None = None, if_absent: bool = False) -> None:
    """Atomically make *path* a copy of *source*, a piece at a time, using temp file + os.replace.

    With *if_tag* or *if_absent*, only where *path* is still the file of
    that tag, or still no file, as it is looked at right before the
    rename: :class:`Changed` otherwise, and nothing written.
    """
    with open(source, "rb") as src:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.tmp.")
        try:
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
                out.flush()
                os.fsync(out.fileno())
            if if_tag is not None or if_absent:
                try:
                    now: str | None = _tag(os.stat(path))
                except FileNotFoundError:
                    now = None
                if now != if_tag:
                    raise Changed(str(path))
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
