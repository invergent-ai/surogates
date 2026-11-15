"""A file the local backend stores is made mode 0660, less the umask: a sandbox user the storage root's default ACL
names reads it and writes it in place, and no other user does either.

Every write went through a temporary file made 0600 (``tempfile.mkstemp``, or ``os.open(..., 0o600)`` in
``_replace_in``, the path on Linux), renamed over the object.  The docker sandbox's user (uid 1000, not root) works in
a session's workspace bind-mounted from a local bucket; where the storage root's default ACL names that user, a file
made 0600 gets the ACL mask ``---``, so the user could read none of a session's uploaded files (``[Errno 13]
Permission denied``).  Made 0660 the mask is ``rw-``: the user reads the file and changes it in place (``>>``, a
spreadsheet saved over an upload).  The mode is given at creation: a chmod after it would override the folder's
default ACL and could leave the file readable by every user.
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
async def test_with_no_acl_a_stored_file_is_0660_less_the_umask(by_folder, umask, tmp_path):
    if _has_default_acl(tmp_path):
        pytest.skip("the test's folder has a default ACL")
    folder = await _store(tmp_path / "store")
    for name in _FILES:
        mode = stat.S_IMODE((folder / name).stat().st_mode)
        assert mode == 0o660 & ~umask, f"{name}: {oct(mode)}"
        assert not mode & 0o007, f"{name}: {oct(mode)}"  # never other users', under umask 0 too


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="runs as other users: needs root")
async def test_a_default_acl_lets_the_sandbox_user_read_and_change_a_stored_file_and_no_other_user(
    by_folder, umask, tmp_path,
):
    setfacl = shutil.which("setfacl")
    if not setfacl or not shutil.which("sh"):
        pytest.skip("needs setfacl and sh")
    base = tmp_path / "store"
    base.mkdir()
    # The storage root as an operator grants it to the sandbox user: a default ACL that the folders and files made
    # under it inherit.  Other users may pass through a folder, so what keeps them out is the stored file's own mode.
    granted = subprocess.run(
        [setfacl, "-d", "-m", f"u:{_SANDBOX_UID}:rwx,o::x", str(base)], capture_output=True, text=True, check=False,
    )
    if granted.returncode != 0 or not _has_default_acl(base):
        pytest.skip(f"no ACLs on this filesystem: {granted.stderr.strip()}")
    folder = await _store(base)
    for name, data in _FILES.items():
        sandbox = _as(_SANDBOX_UID, folder, 'cat "$1"', name)
        assert (sandbox.returncode, sandbox.stdout) == (0, data), sandbox.stderr
        appended = _as(_SANDBOX_UID, folder, 'printf "+" >> "$1"', name)  # in place, as >>, a save over it, a .db
        assert appended.returncode == 0, appended.stderr
        other = _as(_OTHER_UID, folder, 'cat "$1"', name)
        assert other.returncode != 0, f"uid {_OTHER_UID} read {name}: {other.stdout!r}"
        other = _as(_OTHER_UID, folder, 'printf "!" >> "$1"', name)
        assert other.returncode != 0, f"uid {_OTHER_UID} appended to {name}"
        assert (folder / name).read_bytes() == data + b"+"
        mode = stat.S_IMODE((folder / name).stat().st_mode)
        assert not mode & 0o007, f"{name}: {oct(mode)}"


def _as(uid: int, folder: Path, script: str, name: str) -> subprocess.CompletedProcess:
    """The shell *script*, with the file *name* as ``$1``, run in *folder* as the user *uid* (its own group, no
    others).  It starts in the folder, as a bind mount of it would put it there, so the folders above it (the test's
    own temporary folders, 0700) are not in the way."""
    return subprocess.run(
        [shutil.which("sh"), "-c", script, "sh", name], cwd=folder, user=uid, group=uid, extra_groups=[],
        capture_output=True, timeout=30, check=False,
    )
