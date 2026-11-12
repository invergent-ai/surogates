"""A local bucket has no links: no operation follows one, so one session cannot reach another's objects.

``LocalBackend._resolve`` resolved a key through links and only checked that the result stayed in the bucket, and
writes, deletes, lists and stats went by path.  With local storage a session's workspace is a folder of the agent's
bucket (``sessions/<id>``) that its commands write to: a link there to ``../<other>/...`` read, overwrote, listed or
deleted another session's objects, and ``delete_prefix`` of a session whose folder was swapped for a link removed
another session's whole folder.  Now every operation reaches the key from the bucket's root one folder at a time with
no link followed; a link is no object.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from surogates.storage.backend import LocalBackend

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")


@pytest.fixture
async def bucket(tmp_path):
    backend = LocalBackend(str(tmp_path))
    await backend.create_bucket("agent")
    await backend.write("agent", "sessions/victim/secret.txt", b"SECRET")
    await backend.write("agent", "sessions/attacker/mine.txt", b"mine")
    return backend, tmp_path / "agent" / "sessions"


def _victim(sessions: Path) -> bytes:
    return (sessions / "victim" / "secret.txt").read_bytes()


async def test_a_link_to_another_sessions_object_reads_nothing(bucket):
    backend, sessions = bucket
    (sessions / "attacker" / "link.txt").symlink_to("../victim/secret.txt")
    (sessions / "attacker" / "dir").symlink_to("../victim", target_is_directory=True)
    for key in ("sessions/attacker/link.txt", "sessions/attacker/dir/secret.txt"):
        with pytest.raises(KeyError):
            await backend.read("agent", key)
        with pytest.raises(KeyError):
            await backend.read_text("agent", key)
        with pytest.raises(KeyError):
            await backend.stat("agent", key)
        assert await backend.exists("agent", key) is False
        with pytest.raises(KeyError):
            await backend.download("agent", key, sessions.parent.parent / "copy")


async def test_a_write_through_a_link_never_reaches_another_session(bucket, tmp_path):
    backend, sessions = bucket
    (sessions / "attacker" / "link.txt").symlink_to("../victim/secret.txt")
    await backend.write("agent", "sessions/attacker/link.txt", b"overwritten")
    assert _victim(sessions) == b"SECRET"
    assert not (sessions / "attacker" / "link.txt").is_symlink()  # the link itself was replaced
    (sessions / "attacker" / "dir").symlink_to("../victim", target_is_directory=True)
    with pytest.raises(PermissionError):
        await backend.write("agent", "sessions/attacker/dir/secret.txt", b"overwritten")
    source = tmp_path / "upload.bin"
    source.write_bytes(b"uploaded")
    with pytest.raises(PermissionError):
        await backend.upload("agent", "sessions/attacker/dir/new.txt", source)
    assert _victim(sessions) == b"SECRET" and sorted(p.name for p in (sessions / "victim").iterdir()) == ["secret.txt"]


async def test_a_delete_through_a_link_removes_nothing_of_another_session(bucket):
    backend, sessions = bucket
    (sessions / "attacker" / "link.txt").symlink_to("../victim/secret.txt")
    (sessions / "attacker" / "dir").symlink_to("../victim", target_is_directory=True)
    await backend.delete("agent", "sessions/attacker/dir/secret.txt")
    await backend.delete("agent", "sessions/attacker/link.txt")
    assert _victim(sessions) == b"SECRET"


async def test_deleting_a_session_whose_folder_became_a_link_removes_no_other_session(bucket):
    backend, sessions = bucket
    (sessions / "attacker").rename(sessions / "attacker.old")
    (sessions / "attacker").symlink_to("victim", target_is_directory=True)
    assert await backend.delete_prefix("agent", "sessions/attacker") == 0
    assert _victim(sessions) == b"SECRET"


async def test_a_listing_shows_no_link_and_walks_into_none(bucket):
    backend, sessions = bucket
    (sessions / "attacker" / "link.txt").symlink_to("../victim/secret.txt")
    (sessions / "attacker" / "dir").symlink_to("../victim", target_is_directory=True)
    keys = await backend.list_keys("agent", "sessions/attacker")
    assert keys == ["sessions/attacker/mine.txt"]
    bounded = await backend.list_entries("agent", "sessions/attacker", limit=10)
    assert [e["key"] for e in bounded] == ["sessions/attacker/mine.txt"]


async def test_the_bucket_still_works_as_before(tmp_path):
    backend = LocalBackend(str(tmp_path))
    await backend.create_bucket("b")
    await backend.write("b", "x/y/z.txt", b"one")
    await backend.write_text("b", "x/w.txt", "two")
    assert await backend.read("b", "x/y/z.txt") == b"one"
    assert await backend.exists("b", "x/w.txt") and not await backend.exists("b", "x/none")
    st = await backend.stat("b", "x/y/z.txt")
    assert st["size"] == 3 and st["etag"]
    assert await backend.list_keys("b", "x") == ["x/w.txt", "x/y/z.txt"]
    assert [e["key"] for e in await backend.list_entries("b", "", limit=10)] == ["x/", "x/w.txt", "x/y/", "x/y/z.txt"]
    await backend.write("b", "x/y/z.txt", b"three")
    assert await backend.read("b", "x/y/z.txt") == b"three"
    await backend.delete("b", "x/y/z.txt")
    assert not (tmp_path / "b" / "x" / "y").exists()  # empty parents removed, up to the bucket
    assert await backend.delete_prefix("b", "x") == 1
    assert not (tmp_path / "b" / "x").exists()
    for bad in ("../escape", "/etc/passwd"):
        with pytest.raises(ValueError):
            await backend.read("b", bad)
    # New: a key through .. is refused even where it would stay in the bucket (it was resolved before).
    with pytest.raises(ValueError):
        await backend.read("b", "a/../x/w.txt")


async def test_a_listing_of_a_very_deep_tree_needs_no_recursion(tmp_path):
    """A session can make a folder a thousand levels deep: listing it must not fail (the orphan sweep lists buckets)."""
    backend = LocalBackend(str(tmp_path))
    await backend.create_bucket("b")
    (tmp_path / "b" / "s").mkdir(parents=True)
    fd = os.open(tmp_path / "b" / "s", os.O_RDONLY)
    for _ in range(1100):  # made one level at a time, as a loop of mkdir d; cd d does
        os.mkdir("d", dir_fd=fd)
        child = os.open("d", os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = child
    bottom = os.open("bottom.txt", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
    os.write(bottom, b"x")
    os.close(bottom)
    os.close(fd)
    keys = await backend.list_keys("b")
    assert keys == ["s/" + "d/" * 1100 + "bottom.txt"]


async def test_a_bounded_listing_keeps_the_shallowest_entries(tmp_path):
    backend = LocalBackend(str(tmp_path))
    for n in range(5):
        await backend.write("b", f"deep/a/b/c/f{n}.txt", b"x")
    await backend.write("b", "top.txt", b"x")
    await backend.write("b", "deep/mid.txt", b"x")
    keys = [e["key"] for e in await backend.list_entries("b", "", limit=4)]
    assert {"top.txt", "deep/", "deep/mid.txt"} <= set(keys)


async def test_a_file_gone_while_a_prefix_is_deleted_does_not_stop_the_delete(tmp_path, monkeypatch):
    backend = LocalBackend(str(tmp_path))
    for n in range(3):
        await backend.write("b", f"s/x/f{n}.txt", b"x")
    real_fwalk = os.fwalk

    def fwalk(*args, **kwargs):
        for folder, dirs, files, fd in real_fwalk(*args, **kwargs):
            if files:
                os.unlink(files[0], dir_fd=fd)  # removed by someone else after the listing
            yield folder, dirs, files, fd

    monkeypatch.setattr(os, "fwalk", fwalk)
    assert await backend.delete_prefix("b", "s") == 2
    assert not (tmp_path / "b" / "s").exists()


async def test_an_error_other_than_no_object_is_raised(tmp_path, monkeypatch):
    backend = LocalBackend(str(tmp_path))
    await backend.write("b", "s/f.txt", b"x")
    real_stat = os.stat

    def stat(path, *args, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None and path == "f.txt":
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", stat)
    with pytest.raises(PermissionError):
        await backend.exists("b", "s/f.txt")
    with pytest.raises(PermissionError):
        await backend.stat("b", "s/f.txt")


async def test_conditional_writes_and_deletes_meet_a_link_as_a_change(bucket, tmp_path):
    from surogates.storage.backend import Changed

    backend, sessions = bucket
    (sessions / "attacker" / "link.txt").symlink_to("../victim/secret.txt")
    source = tmp_path / "upload.bin"
    source.write_bytes(b"uploaded")
    with pytest.raises(Changed):
        await backend.upload("agent", "sessions/attacker/link.txt", source, if_absent=True)
    with pytest.raises(Changed):
        await backend.delete("agent", "sessions/attacker/link.txt", if_tag="1-6-0")
    await backend.upload("agent", "sessions/attacker/link.txt", source)  # unconditional: the link is replaced
    assert _victim(sessions) == b"SECRET"
    assert (sessions / "attacker" / "link.txt").read_bytes() == b"uploaded"


async def test_deleting_a_prefix_that_holds_a_link_out_removes_only_the_link(bucket):
    backend, sessions = bucket
    (sessions / "attacker" / "out").symlink_to("../victim", target_is_directory=True)
    (sessions / "attacker" / "file-link").symlink_to("../victim/secret.txt")
    assert await backend.delete_prefix("agent", "sessions/attacker") == 1  # mine.txt only
    assert _victim(sessions) == b"SECRET"


async def test_a_link_on_a_write_path_is_named_as_such(bucket):
    backend, sessions = bucket
    (sessions / "attacker" / "dir").symlink_to("../victim", target_is_directory=True)
    with pytest.raises(PermissionError, match="a bucket has no links"):
        await backend.write("agent", "sessions/attacker/dir/new.txt", b"x")
