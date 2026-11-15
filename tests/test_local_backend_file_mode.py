"""A file the local backend stores is readable by a sandbox's non-root user.

Every write went through a temporary file made mode 0600 (``tempfile.mkstemp``, or ``os.open(..., 0o600)`` in
``_replace_in``, the path on Linux), renamed over the object with that mode.  So every stored file was readable only by
the runtime's own user: the docker sandbox's user (uid 1000, not root) that bind-mounts a session's workspace from a
local bucket could read none of a session's uploaded files (``[Errno 13] Permission denied``).  Now a stored file has
the mode ``open()`` gives a new file under the process's umask.
"""

from __future__ import annotations

import os
import stat
import warnings

import pytest

from surogates.storage.backend import LocalBackend
from surogates.tools.workspace_io import local as workspace_local

pytestmark = pytest.mark.skipif(not hasattr(os, "fchmod"), reason="POSIX only")

#: The docker sandbox's user (``sandbox``), as in the Kubernetes sandbox's ``_SANDBOX_UID``.
_SANDBOX_UID = 1000


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


#: The umask the backend was imported under: it reads it once, then (os.umask is process-wide).
_IMPORT_UMASK = _umask()


@pytest.fixture
def umask_022():
    """The process umask 0o022 for the test (the folders the backend makes: 0755), restored after."""
    old = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(old)


@pytest.mark.skipif(bool(_IMPORT_UMASK & 0o004), reason="the process umask keeps new files from others")
# By folder descriptor (``_replace_in``: Linux), and by path (``_atomic_write_bytes``, ``_atomic_copy``).
@pytest.mark.parametrize("by_folder", [True, False], ids=["by-folder", "by-path"])
async def test_a_written_or_uploaded_file_is_readable_by_the_sandbox_user(by_folder, umask_022, tmp_path, monkeypatch):
    if by_folder and not workspace_local._BY_FOLDER:
        pytest.skip("this system has no descriptor calls")
    monkeypatch.setattr(workspace_local, "_BY_FOLDER", by_folder)
    base = tmp_path / "store"
    backend = LocalBackend(str(base))
    await backend.create_bucket("agent")
    await backend.write("agent", "sessions/s1/data/manual.md", b"written")
    source = tmp_path / "upload.csv"
    source.write_bytes(b"uploaded")
    await backend.upload("agent", "sessions/s1/data/payments.csv", source)
    folder = base / "agent" / "sessions" / "s1" / "data"
    for name, data in (("manual.md", b"written"), ("payments.csv", b"uploaded")):
        if os.geteuid() == 0:
            assert _read_as(_SANDBOX_UID, folder, name) == data
        mode = stat.S_IMODE((folder / name).stat().st_mode)
        assert mode & 0o004, f"{name}: {oct(mode)}"
        assert mode == 0o666 & ~_IMPORT_UMASK, f"{name}: {oct(mode)}"


def _read_as(uid: int, folder, name: str) -> bytes:
    """The file *name* in *folder* as the user *uid* (no groups) reads it: by a handle of the folder, as a bind mount
    of it would reach it, so the folders above it (the test's own temporary folder, 0700) are not in the way."""
    folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    reader, writer = os.pipe()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork() in a process with threads
            pid = os.fork()
        if pid == 0:  # the child: only system calls, then out
            code = 1
            try:
                os.close(reader)
                os.setgroups([])
                os.setgid(uid)
                os.setuid(uid)
                fd = os.open(name, os.O_RDONLY, dir_fd=folder_fd)
                os.write(writer, os.read(fd, 1 << 16))
                code = 0
            except OSError as exc:
                os.write(writer, f"{name}: {os.strerror(exc.errno)} (errno {exc.errno})".encode())
            finally:
                os._exit(code)
        os.close(writer)
        writer = -1
        chunks = []
        while chunk := os.read(reader, 1 << 16):
            chunks.append(chunk)
        _, status = os.waitpid(pid, 0)
        got = b"".join(chunks)
        assert os.waitstatus_to_exitcode(status) == 0, got.decode(errors="replace")
        return got
    finally:
        os.close(folder_fd)
        os.close(reader)
        if writer != -1:
            os.close(writer)
