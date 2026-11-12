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
    for bad in ("../escape", "/etc/passwd", "a/../../b"):
        with pytest.raises(ValueError):
            await backend.read("b", bad)
