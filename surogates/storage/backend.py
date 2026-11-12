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
import collections
import contextlib
import errno
import io
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

#: How much of an object a streamed read holds at once, and the size of each part of a streamed write.
_CHUNK = 1 << 20
_PART = 8 << 20
#: What an S3 store's answer to a head is coded when there is no such object.
_ABSENT = ("404", "NoSuchKey", "NotFound")


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
        moment it puts the new file in place; ``S3Backend`` asks for the
        object once every part is sent, right before it puts them together,
        so one changed in that call itself is written over all the same.
        """
        ...

    async def exists(self, bucket: str, key: str) -> bool:
        """Return True if the object exists."""
        ...

    async def delete(self, bucket: str, key: str, *, if_tag: str | None = None) -> None:
        """Delete an object.  No-op if it doesn't exist.

        With *if_tag*, the ``etag`` :meth:`stat` gave the object, only that
        object is taken away: one changed since, or gone, raises
        :class:`Changed` and is left as it is, where the store can tell.
        ``LocalBackend`` tells up to the moment it removes the file;
        ``S3Backend`` asks for the object right before the delete, so one
        changed in the delete's own call is taken away all the same.
        """
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
        """The object's path under the bucket's root, as its key names it.

        A bucket has no links: a folder on a disk can hold one, put there by
        whoever writes the folder (a session's commands, in its workspace
        under ``sessions/``), and following it reached another session's
        objects, or anything on the host.  So the key is never resolved
        through one: its path is lexical, and every operation reaches it
        from the bucket's root one folder at a time with no link followed.
        Raises ValueError for a key that leaves the bucket (absolute, ``..``).
        """
        if not _walk()._BY_FOLDER:
            # No descriptor calls (Windows): resolved and checked by path, as before.
            path = (self._base / bucket / key).resolve()
            if not path.is_relative_to(self._bucket_path(bucket)):
                raise ValueError(f"Path traversal denied: {key}")
            return path
        return self._bucket_path(bucket).joinpath(*_key_parts(key))

    def _folder_of(self, bucket: str, key: str, *, create: bool = False) -> tuple[int, str]:
        """A descriptor of the folder of the object *key* and the object's
        name, reached from the bucket's root with no link followed (missing
        folders made with *create*).  The caller closes the descriptor.
        Raises ValueError for a key that names no object, OSError where the
        folder cannot be reached (a link on the way: PermissionError)."""
        parts = _key_parts(key)
        if not parts:
            raise ValueError(f"No object at {key!r}")
        root = self._bucket_path(bucket)
        if create:
            root.mkdir(parents=True, exist_ok=True)  # as a write by path made it
        return _walk()._open_folder(str(root), parts[:-1], create=create), parts[-1]

    def _plain_file(self, bucket: str, key: str) -> os.stat_result | None:
        """The object *key*'s own stat, where it is a plain file reached with
        no link followed; None for no object (missing, a folder, a link, or a
        link on the way)."""
        try:
            folder, name = self._folder_of(bucket, key)
        except ValueError:
            return None
        except OSError as exc:
            if _no_object(exc):
                return None
            raise  # any other error is raised, never read as no object
        try:
            st = os.stat(name, dir_fd=folder, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return None
        finally:
            os.close(folder)
        return st if stat.S_ISREG(st.st_mode) else None

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
        return self._read_resolved(bucket, key, path)

    async def read_text(self, bucket: str, key: str, encoding: str = "utf-8") -> str:
        path = self._resolve(bucket, key)
        # As Path.read_text did: universal newlines.
        return self._read_resolved(bucket, key, path).decode(encoding).replace("\r\n", "\n").replace("\r", "\n")

    def _read_resolved(self, bucket: str, key: str, path: Path) -> bytes:
        """The object at *path* (``_resolve`` checked it is in the bucket),
        reached from the bucket's root one folder at a time with no link
        followed: a folder swapped for a link after the check (a bucket that
        holds workspaces commands write to) reads nothing outside it.
        KeyError where no regular file is."""
        if not _walk()._BY_FOLDER:
            if not path.is_file():
                raise KeyError(f"{bucket}/{key}")
            return path.read_bytes()
        with self._read_resolved_open(bucket, key, path) as fh:
            return fh.read()

    async def write(self, bucket: str, key: str, data: bytes) -> None:
        path = self._resolve(bucket, key)
        if not _walk()._BY_FOLDER:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(path, data)
            return
        folder, name = self._writable_folder_of(bucket, key)
        try:
            _replace_in(folder, name, io.BytesIO(data), None, False)
        finally:
            os.close(folder)

    def _writable_folder_of(self, bucket: str, key: str) -> tuple[int, str]:
        """:meth:`_folder_of` with missing folders made; a link on the key's
        path is refused as such (a bucket has no links)."""
        try:
            return self._folder_of(bucket, key, create=True)
        except OSError as exc:
            if _is_link_refusal(exc):
                raise PermissionError(errno.EACCES, f"A link on the path of {key!r}: a bucket has no links") from None
            raise

    async def write_text(
        self, bucket: str, key: str, text: str, encoding: str = "utf-8",
    ) -> None:
        await self.write(bucket, key, text.encode(encoding))

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
        if not _walk()._BY_FOLDER:
            if not path.is_file():
                raise KeyError(f"{bucket}/{key}")
            return await asyncio.to_thread(_copy, path, target, limit, f"{bucket}/{key}")
        if self._plain_file(bucket, key) is None:
            raise KeyError(f"{bucket}/{key}")
        return await asyncio.to_thread(
            _copy_from, lambda: self._read_resolved_open(bucket, key, path), target, limit, f"{bucket}/{key}",
        )

    def _read_resolved_open(self, bucket: str, key: str, path: Path) -> Any:
        """The object at *path* open to read, reached from the bucket's root
        with no link followed.  KeyError where no regular file is (missing, a
        folder, a link at the key or on the way to it: a bucket has no
        links); any other error is raised."""
        try:
            return _walk()._open_to_read(str(self._bucket_path(bucket)), str(path))
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            raise KeyError(f"{bucket}/{key}") from None
        except OSError as exc:
            if exc.errno == errno.EINVAL or _is_link_refusal(exc):
                raise KeyError(f"{bucket}/{key}") from None
            raise

    async def upload(
        self, bucket: str, key: str, source: Path, *, if_tag: str | None = None, if_absent: bool = False,
    ) -> None:
        path = self._resolve(bucket, key)
        if not _walk()._BY_FOLDER:
            await asyncio.to_thread(_atomic_copy, source, path, if_tag, if_absent)
            return

        def put() -> None:
            with open(source, "rb") as src:
                folder, name = self._writable_folder_of(bucket, key)
                try:
                    _replace_in(folder, name, src, if_tag, if_absent)
                finally:
                    os.close(folder)

        await asyncio.to_thread(put)

    async def exists(self, bucket: str, key: str) -> bool:
        path = self._resolve(bucket, key)
        if not _walk()._BY_FOLDER:
            return path.is_file()
        return self._plain_file(bucket, key) is not None

    async def delete(self, bucket: str, key: str, *, if_tag: str | None = None) -> None:
        path = self._resolve(bucket, key)
        if not _walk()._BY_FOLDER:
            if if_tag is not None:
                await asyncio.to_thread(_unlink_if, path, if_tag)
            elif path.is_file():
                path.unlink()
            else:
                return
            _remove_empty_parents_by_path(self._bucket_path(bucket), path)
            return
        try:
            folder, name = self._folder_of(bucket, key)
        except (OSError, ValueError) as exc:
            if isinstance(exc, OSError) and not _no_object(exc):
                raise
            if if_tag is not None:
                raise Changed(f"{bucket}/{key}") from None
            return
        try:
            try:
                st = os.stat(name, dir_fd=folder, follow_symlinks=False)
            except FileNotFoundError:
                st = None
            if if_tag is not None:
                # Looked at right before it is removed: a save since is not taken away.
                if st is None or not stat.S_ISREG(st.st_mode) or _tag(st) != if_tag:
                    raise Changed(f"{bucket}/{key}")
            elif st is None or not stat.S_ISREG(st.st_mode):
                return  # no object: a bucket has no links, and a folder is no object
            os.unlink(name, dir_fd=folder)
        finally:
            os.close(folder)
        # Clean up empty parent directories up to the bucket root.
        self._remove_empty_folders(bucket, _key_parts(key)[:-1])

    def _remove_empty_folders(self, bucket: str, folders: list[str]) -> None:
        """Remove the folders *folders* (a path below the bucket's root),
        deepest first, while each is empty; each reached with no link
        followed, and a link is never removed in a folder's place."""
        root = str(self._bucket_path(bucket))
        for depth in range(len(folders), 0, -1):
            try:
                parent = _walk()._open_folder(root, folders[:depth - 1], create=False)
            except OSError:
                return
            try:
                os.rmdir(folders[depth - 1], dir_fd=parent)
            except OSError:
                return  # not empty, gone, or not a folder: stop here
            finally:
                os.close(parent)

    async def delete_prefix(self, bucket: str, prefix: str) -> int:
        if not prefix:
            raise ValueError("delete_prefix requires a non-empty prefix")
        bucket_root = self._bucket_path(bucket)
        if not _walk()._BY_FOLDER:
            target = (bucket_root / prefix).resolve()
            if not target.is_relative_to(bucket_root):
                raise ValueError(f"Path traversal denied: {prefix}")
            if not target.is_dir():
                return 0
            deleted = sum(1 for p in target.rglob("*") if p.is_file())
            shutil.rmtree(target)
            _remove_empty_parents_by_path(bucket_root, target)
            return deleted
        parts = _key_parts(prefix)
        if not parts:
            raise ValueError("delete_prefix requires a non-empty prefix")
        # The prefix's folder, reached with no link followed: a link there (a
        # session's own folder swapped for one to another's) deletes nothing.
        try:
            parent = _walk()._open_folder(str(bucket_root), parts[:-1], create=False)
        except OSError:
            return 0
        try:
            try:
                if not stat.S_ISDIR(os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode):
                    return 0
            except FileNotFoundError:
                return 0
            deleted = 0
            for _folder, _dirs, files, folder_fd in os.fwalk(parts[-1], dir_fd=parent, follow_symlinks=False):
                for f in files:
                    try:
                        deleted += stat.S_ISREG(os.stat(f, dir_fd=folder_fd, follow_symlinks=False).st_mode)
                    except FileNotFoundError:
                        continue  # gone meanwhile
            # By descriptor: a link met inside is removed, never followed.
            if not shutil.rmtree.avoids_symlink_attacks:
                raise OSError(errno.ENOTSUP, "No symlink-safe rmtree on this system")
            shutil.rmtree(parts[-1], dir_fd=parent)
        finally:
            os.close(parent)
        # Match ``delete``: walk up empty parents to the bucket root.
        self._remove_empty_folders(bucket, parts[:-1])
        return deleted

    async def list_keys(self, bucket: str, prefix: str = "") -> list[str]:
        return [entry["key"] for entry in await self.list_entries(bucket, prefix)]

    async def list_entries(self, bucket: str, prefix: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        bucket_root = self._bucket_path(bucket)
        if _walk()._BY_FOLDER:
            return self._list_entries_by_folder(bucket, prefix, limit)
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

    def _list_entries_by_folder(self, bucket: str, prefix: str, limit: int | None) -> list[dict[str, Any]]:
        """:meth:`list_entries`, walked from the bucket's root with no link
        followed: a link is no entry, and a folder link is not walked into.
        Breadth first (all of a folder's entries before any deeper folder's),
        one folder open at a time, so a deep tree costs neither recursion nor
        descriptors, and no deeper than S3's longest key; with *limit*, the
        shallowest entries are the ones kept."""
        parts = _key_parts(prefix)
        root = str(self._bucket_path(bucket))
        entries: list[dict[str, Any]] = []
        queue: collections.deque[list[str]] = collections.deque([parts])
        directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        while queue:
            if limit is not None and len(entries) >= limit:
                break
            folder_parts = queue.popleft()
            try:
                walked = _walk()._open_folder(root, folder_parts, create=False)
            except OSError:
                continue  # gone, or a link put in its place: nothing to list
            try:
                listing_fd = os.open(".", directory, dir_fd=walked)
            except OSError:
                continue
            finally:
                os.close(walked)
            found: list[tuple[str, os.stat_result]] = []
            try:
                # Each entry's own stat, taken while its folder is open.
                with os.scandir(listing_fd) as listing:
                    for entry in listing:
                        try:
                            found.append((entry.name, entry.stat(follow_symlinks=False)))
                        except OSError:
                            continue
            finally:
                os.close(listing_fd)
            rel = "/".join(folder_parts)
            for name, st in found:
                if limit is not None and len(entries) >= limit:
                    break
                is_dir = stat.S_ISDIR(st.st_mode)
                # A folder whose keys would pass S3's 1,024-byte limit is not
                # walked into: no bucket holds such a key, and each folder is
                # reached from the root, so depth bounds what one costs.
                if is_dir and len(f"{rel}/{name}/x".lstrip("/").encode()) <= _MAX_KEY_BYTES:
                    queue.append([*folder_parts, name])
                if not stat.S_ISREG(st.st_mode) and not (is_dir and limit is not None):
                    continue  # a link, or a folder where no limit asks for folders
                entries.append({
                    # Bounded, a folder is an entry too, named as a bucket names a folder's marker: a
                    # folder of folders is not walked whole, and the caller learns there is more.
                    "key": f"{rel}/{name}".lstrip("/") + ("/" if is_dir else ""),
                    "modified": st.st_mtime,
                    "size": 0 if is_dir else st.st_size,
                })
        entries.sort(key=lambda e: e["key"])
        return entries

    async def stat(self, bucket: str, key: str) -> dict[str, Any]:
        path = self._resolve(bucket, key)
        if not _walk()._BY_FOLDER:
            if not path.is_file():
                raise KeyError(f"{bucket}/{key}")
            st = path.stat()
        else:
            st = self._plain_file(bucket, key)
            if st is None:
                raise KeyError(f"{bucket}/{key}")
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
            except ClientError as exc:
                if _absent(exc):
                    raise KeyError(f"{bucket}/{key}") from exc
                raise
            return await resp["Body"].read()

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
            except ClientError as exc:
                if _absent(exc):
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
        from boto3.s3.transfer import TransferConfig

        async with self._client() as s3:
            if if_tag is not None or if_absent:
                await _upload_if(s3, bucket, key, source, if_tag)
                return
            # In parts, each read from the file as the last are sent: two on their way and two waiting, at most.
            parts = TransferConfig(multipart_chunksize=_PART, max_concurrency=2, max_io_queue=2)
            await s3.upload_file(str(source), bucket, key, Config=parts)

    async def exists(self, bucket: str, key: str) -> bool:
        # Absent only where the store answered so: any other error is no answer, and is raised.
        try:
            await self.stat(bucket, key)
        except KeyError:
            return False
        return True

    async def delete(self, bucket: str, key: str, *, if_tag: str | None = None) -> None:
        async with self._client() as s3:
            if if_tag is not None:
                # Asked for right before the delete, which is then the whole window left: the client takes a
                # condition on a delete (If-Match), but whether a store honours one, Garage among them, is not
                # known here, and one that refused it would refuse every delete.
                try:
                    now = (await s3.head_object(Bucket=bucket, Key=key)).get("ETag")
                except Exception as exc:
                    raise Changed(f"{bucket}/{key}") from exc
                if now != if_tag:
                    raise Changed(f"{bucket}/{key}")
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
            except ClientError as exc:
                if _absent(exc):
                    raise KeyError(f"{bucket}/{key}") from exc
                raise
            return {
                "size": resp.get("ContentLength", 0),
                "modified": resp.get("LastModified"),
                # What the store names the object's contents by: two writes in one second differ by it.
                "etag": resp.get("ETag"),
            }

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


def _walk() -> Any:
    """The no-follow walk of ``surogates.tools.workspace_io.local`` (imported
    when first used: the tools package imports storage)."""
    from surogates.tools.workspace_io import local

    return local


#: S3's longest key, in bytes: a listing walks no folder deeper.
_MAX_KEY_BYTES = 1024


def _key_parts(key: str) -> list[str]:
    """A key's names below the bucket's root (empty for the root itself).
    ValueError for one that leaves it: absolute, or through ``..``."""
    if key.startswith("/") or "\x00" in key:
        raise ValueError(f"Path traversal denied: {key}")
    parts = [part for part in key.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"Path traversal denied: {key}")
    return parts


def _is_link_refusal(exc: OSError) -> bool:
    """Whether *exc* is the walk's refusal of a link at a key or on the way to it."""
    return isinstance(exc, _walk().LinkOnPath)


def _no_object(exc: OSError) -> bool:
    """Whether *exc*, met reaching a key, means there is no object there:
    missing, a file where a folder is, or a link (a bucket has no links)."""
    return isinstance(exc, (FileNotFoundError, NotADirectoryError)) or _is_link_refusal(exc)


def _replace_in(folder: int, name: str, src: Any, if_tag: str | None, if_absent: bool) -> None:
    """Make *name* in the folder *folder* a copy of the stream *src*: a new
    temporary file beside it (O_EXCL | O_NOFOLLOW, mode 0600), synced, then
    renamed over it; a link at the name is replaced, never followed.  With
    *if_tag* or *if_absent*, only where *name* is still the plain file of
    that tag, or still none, as it is looked at right before the rename:
    :class:`Changed` otherwise, and nothing written."""
    tmp = f".{name}.tmp.{os.urandom(6).hex()}"
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600,
        dir_fd=folder,
    )
    try:
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(src, out, _CHUNK)
            out.flush()
            os.fsync(out.fileno())
        if if_tag is not None or if_absent:
            try:
                st = os.stat(name, dir_fd=folder, follow_symlinks=False)
                now: str | None = _tag(st) if stat.S_ISREG(st.st_mode) else "not a file"
            except FileNotFoundError:
                now = None
            if now != if_tag:
                raise Changed(name)
        os.replace(tmp, name, src_dir_fd=folder, dst_dir_fd=folder)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=folder)
        raise


def _copy_from(opener: Any, target: Path, limit: int | None, name: str) -> int:
    """:func:`_copy` from the file *opener* opens."""
    written = 0
    try:
        with opener() as src, open(target, "wb") as out:
            while chunk := src.read(_CHUNK):
                written += len(chunk)
                if limit is not None and written > limit:
                    raise TooLarge(name)
                out.write(chunk)
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise
    return written


def _remove_empty_parents_by_path(bucket_root: Path, path: Path) -> None:
    """Remove *path*'s empty parent folders up to *bucket_root*, by path (where no descriptor calls exist)."""
    parent = path.parent
    while parent != bucket_root and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


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


def _unlink_if(path: Path, tag: str) -> None:
    """Remove the file *path* where it is still the file of *tag*, as it is looked at right before: :class:`Changed` otherwise."""
    try:
        now = _tag(os.stat(path))
    except FileNotFoundError:
        raise Changed(str(path)) from None
    if now != tag:
        raise Changed(str(path))
    path.unlink()


async def _upload_if(s3: Any, bucket: str, key: str, source: Path, tag: str | None) -> None:
    """Write the object *key* from the file *source*, where it is still of the ETag *tag* (None: still no object).

    The object is asked for right before the call that makes it the new
    one.  A file of one part is written whole after the ask.  A larger one
    goes up in parts, one at a time, and is asked for once they are sent:
    the parts are the write's whole time, and none of them is the object
    until they are put together.  So a save while they go up is seen, the
    parts let go and :class:`Changed` raised, and the window left is the
    call that writes or puts together.  The client's own upload passes no
    condition on, and whether a store honours one, Garage among them, is
    not known here: one that refused it would refuse every write.
    """
    with open(source, "rb") as file:
        body = await asyncio.to_thread(file.read, _PART)
        if len(body) < _PART:
            await _still(s3, bucket, key, tag)
            await s3.put_object(Bucket=bucket, Key=key, Body=body)
            return
        upload = (await s3.create_multipart_upload(Bucket=bucket, Key=key))["UploadId"]
        try:
            parts: list[dict[str, Any]] = []
            while body:
                number = len(parts) + 1
                sent = await s3.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=number, Body=body)
                parts.append({"PartNumber": number, "ETag": sent["ETag"]})
                body = await asyncio.to_thread(file.read, _PART)
            await _still(s3, bucket, key, tag)
            await s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload, MultipartUpload={"Parts": parts})
        except BaseException:
            with contextlib.suppress(Exception):
                await s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
            raise


async def _still(s3: Any, bucket: str, key: str, tag: str | None) -> None:
    """:class:`Changed` unless the object *key* is still of the ETag *tag*, or still none for None, as the store answers now."""
    try:
        now = (await s3.head_object(Bucket=bucket, Key=key)).get("ETag")
    except ClientError as exc:
        if not _absent(exc):
            raise
        now = None
    if now != tag:
        raise Changed(f"{bucket}/{key}")


def _absent(exc: ClientError) -> bool:
    """Whether *exc* is the store's answer that there is no such object.

    By its code, or its status where it gives none.  Never by the words: a
    client's own error names the request's address, and a key may hold "404".
    """
    answer = exc.response
    return (answer.get("Error", {}).get("Code") or str(answer.get("ResponseMetadata", {}).get("HTTPStatusCode"))) in _ABSENT


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
