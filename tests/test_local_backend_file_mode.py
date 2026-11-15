"""A file the local backend stores is made mode 0640, less the umask: a sandbox user the storage root's default ACL
names reads it, and no other user does.

Every write went through a temporary file made 0600 (``tempfile.mkstemp``, or ``os.open(..., 0o600)`` in
``_replace_in``, the path on Linux), renamed over the object.  The docker sandbox's user (uid 1000, not root) works in
a session's workspace bind-mounted from a local bucket; where the storage root's default ACL names that user, a file
made 0600 gets the ACL mask ``---``, so the user could read none of a session's uploaded files (``[Errno 13]
Permission denied``).  Made 0640 the mask is ``r--``.  The mode is given at creation: a chmod after it would override
the folder's default ACL and could leave the file readable by every user.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from surogates.storage.backend import LocalBackend
from surogates.tools.workspace_io import local as workspace_local

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")

#: The docker sandbox's user (``sandbox``), as in the Kubernetes sandbox's ``_SANDBOX_UID``.
_SANDBOX_UID = 1000
#: Any other user of the host (``nobody``).
_OTHER_UID = 65534
_FILES = {"manual.md": b"written", "payments.csv": b"uploaded"}


@pytest.fixture(params=[True, False], ids=["by-folder", "by-path"])
def by_folder(request, monkeypatch):
    """Writes by folder descriptor (``_replace_in``: Linux), or by path (``_atomic_write_bytes``, ``_atomic_copy``)."""
    if request.param and not workspace_local._BY_FOLDER:
        pytest.skip("this system has no descriptor calls")
    monkeypatch.setattr(workspace_local, "_BY_FOLDER", request.param)
    return request.param


@pytest.fixture
def umask(request):
    """The process umask for the test (0o022 unless parametrized), restored after."""
    old = os.umask(getattr(request, "param", 0o022))
    try:
        yield getattr(request, "param", 0o022)
    finally:
        os.umask(old)


async def _store(base: Path) -> Path:
    """manual.md written and payments.csv uploaded through a LocalBackend at *base*: their folder."""
    backend = LocalBackend(str(base))
    await backend.create_bucket("agent")
    await backend.write("agent", "sessions/s1/data/manual.md", _FILES["manual.md"])
    source = base.parent / "upload.csv"
    source.write_bytes(_FILES["payments.csv"])
    await backend.upload("agent", "sessions/s1/data/payments.csv", source)
    return base / "agent" / "sessions" / "s1" / "data"


def _has_default_acl(path: Path) -> bool:
    try:
        os.getxattr(path, "system.posix_acl_default")
    except OSError:  # none, or no ACLs here
        return False
    return True


@pytest.mark.parametrize(
    "umask", [0o022, 0o077, 0o000], indirect=True, ids=["umask-022", "umask-077", "umask-000"],
)
async def test_with_no_acl_a_stored_file_is_0640_less_the_umask(by_folder, umask, tmp_path):
    if _has_default_acl(tmp_path):
        pytest.skip("the test's folder has a default ACL")
    folder = await _store(tmp_path / "store")
    for name in _FILES:
        mode = stat.S_IMODE((folder / name).stat().st_mode)
        assert mode == 0o640 & ~umask, f"{name}: {oct(mode)}"
        assert not mode & 0o007, f"{name}: {oct(mode)}"  # never other users', under umask 0 too


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="reads as other users: needs root")
async def test_a_default_acl_lets_the_sandbox_user_read_a_stored_file_and_no_other_user(by_folder, umask, tmp_path):
    setfacl = shutil.which("setfacl")
    if not setfacl or not shutil.which("cat"):
        pytest.skip("needs setfacl and cat")
    base = tmp_path / "store"
    base.mkdir()
    # The storage root as an operator grants it to the sandbox user: a default ACL that the folders and files made
    # under it inherit.  Other users may pass through a folder, so what keeps them out is the stored file's own mode.
    granted = subprocess.run(
        [setfacl, "-d", "-m", f"u:{_SANDBOX_UID}:rx,o::x", str(base)], capture_output=True, text=True, check=False,
    )
    if granted.returncode != 0 or not _has_default_acl(base):
        pytest.skip(f"no ACLs on this filesystem: {granted.stderr.strip()}")
    folder = await _store(base)
    for name, data in _FILES.items():
        sandbox = _read_as(_SANDBOX_UID, folder, name)
        assert (sandbox.returncode, sandbox.stdout) == (0, data), sandbox.stderr
        other = _read_as(_OTHER_UID, folder, name)
        assert other.returncode != 0, f"uid {_OTHER_UID} read {name}: {other.stdout!r}"
        mode = stat.S_IMODE((folder / name).stat().st_mode)
        assert not mode & 0o007, f"{name}: {oct(mode)}"


def _read_as(uid: int, folder: Path, name: str) -> subprocess.CompletedProcess:
    """``cat`` of the file *name* in *folder* as the user *uid* (its own group, no others).  It starts in the folder,
    as a bind mount of it would put it there, so the folders above it (the test's own temporary folders, 0700) are
    not in the way."""
    return subprocess.run(
        [shutil.which("cat"), name], cwd=folder, user=uid, group=uid, extra_groups=[],
        capture_output=True, timeout=30, check=False,
    )
