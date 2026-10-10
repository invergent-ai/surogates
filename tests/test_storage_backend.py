"""Local storage object operations and workspace path protection."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import time
import tracemalloc
from pathlib import Path

import pytest
from aioboto3.s3 import inject

from surogates.storage.backend import (
    Changed,
    LocalBackend,
    S3Backend,
    TooLarge,
)


class TestLocalBackendObjects:
    """Object read/write/delete/list operations."""

    @pytest.fixture()
    async def backend(self, tmp_path: Path) -> LocalBackend:
        b = LocalBackend(base_path=str(tmp_path))
        await b.create_bucket("bucket")
        return b

    async def test_write_and_read(self, backend: LocalBackend):
        await backend.write("bucket", "key.bin", b"\x00\x01\x02")
        data = await backend.read("bucket", "key.bin")
        assert data == b"\x00\x01\x02"

    async def test_write_text_and_read_text(self, backend: LocalBackend):
        await backend.write_text("bucket", "hello.txt", "world")
        text = await backend.read_text("bucket", "hello.txt")
        assert text == "world"

    async def test_read_nonexistent_raises(self, backend: LocalBackend):
        with pytest.raises(KeyError):
            await backend.read("bucket", "no-such-key")


    async def test_delete(self, backend: LocalBackend):
        await backend.write_text("bucket", "key", "val")
        await backend.delete("bucket", "key")
        assert not await backend.exists("bucket", "key")


    async def test_delete_cleans_empty_parents(self, backend: LocalBackend, tmp_path: Path):
        await backend.write_text("bucket", "a/b/c.txt", "data")
        await backend.delete("bucket", "a/b/c.txt")
        # Empty dirs should be cleaned up.
        assert not (tmp_path / "bucket" / "a" / "b").exists()
        assert not (tmp_path / "bucket" / "a").exists()

    async def test_delete_prefix_removes_subtree_and_returns_count(
        self, backend: LocalBackend, tmp_path: Path,
    ):
        await backend.write_text("bucket", "sessions/abc/file.txt", "1")
        await backend.write_text("bucket", "sessions/abc/sub/nested.txt", "2")
        await backend.write_text("bucket", "sessions/other/keep.txt", "3")

        deleted = await backend.delete_prefix("bucket", "sessions/abc/")

        assert deleted == 2
        assert not (tmp_path / "bucket" / "sessions" / "abc").exists()
        # Sibling prefix is untouched, and the shared parent is preserved.
        assert (tmp_path / "bucket" / "sessions" / "other" / "keep.txt").exists()

    async def test_delete_prefix_missing_is_noop(self, backend: LocalBackend):
        assert await backend.delete_prefix("bucket", "sessions/missing/") == 0

    async def test_delete_prefix_rejects_traversal(self, backend: LocalBackend):
        with pytest.raises(ValueError):
            await backend.delete_prefix("bucket", "../escape")

    async def test_delete_prefix_rejects_empty(self, backend: LocalBackend):
        with pytest.raises(ValueError):
            await backend.delete_prefix("bucket", "")

    async def test_download_goes_through_a_file(self, backend: LocalBackend, tmp_path: Path):
        data = b"\x00\x01" * 3 * 2**20  # more than one piece
        await backend.write("bucket", "deep/key.bin", data)
        target = tmp_path / "target.bin"
        assert await backend.download("bucket", "deep/key.bin", target) == 6 * 2**20
        assert target.read_bytes() == data
        with pytest.raises(KeyError):
            await backend.download("bucket", "nope.bin", target)

    async def test_download_past_its_limit_is_refused_and_leaves_no_file(self, backend: LocalBackend, tmp_path: Path):
        await backend.write("bucket", "key.bin", b"x" * 1025)
        target = tmp_path / "target.bin"
        with pytest.raises(TooLarge):
            await backend.download("bucket", "key.bin", target, limit=1024)
        assert not target.exists()
        assert await backend.download("bucket", "key.bin", target, limit=1025) == 1025

    async def test_upload_goes_through_a_file_and_replaces_the_object_whole(self, backend: LocalBackend, tmp_path: Path):
        source = tmp_path / "source.bin"
        source.write_bytes(b"\x00\x01" * 3 * 2**20)  # more than one piece
        await backend.write("bucket", "deep/key.bin", b"what the object held, and no part of the new file")
        await backend.upload("bucket", "deep/key.bin", source)
        assert await backend.read("bucket", "deep/key.bin") == source.read_bytes()
        # Under a folder the bucket did not have, too; and nothing of the upload is left beside the object.
        await backend.upload("bucket", "new/folder/key.bin", source)
        assert await backend.read("bucket", "new/folder/key.bin") == source.read_bytes()
        assert [p.name for p in (tmp_path / "bucket" / "deep").iterdir()] == ["key.bin"]

    async def test_an_object_is_told_by_a_tag_that_changes_with_each_write(self, backend: LocalBackend, tmp_path: Path):
        await backend.write("bucket", "key.bin", b"one")
        first = (await backend.stat("bucket", "key.bin"))["etag"]
        assert (await backend.stat("bucket", "key.bin"))["etag"] == first
        # Written anew, or written in place at the same size: another tag.
        await backend.write("bucket", "key.bin", b"two")
        second = (await backend.stat("bucket", "key.bin"))["etag"]
        with open(tmp_path / "bucket" / "key.bin", "r+b") as file:
            file.write(b"owt")
        assert len({first, second, (await backend.stat("bucket", "key.bin"))["etag"]}) == 3

    async def test_an_upload_is_made_only_where_the_object_is_the_one_its_writer_saw(self, backend: LocalBackend, tmp_path: Path):
        source = tmp_path / "source.bin"
        source.write_bytes(b"the version")
        await backend.write("bucket", "key.bin", b"what the writer saw")
        seen = (await backend.stat("bucket", "key.bin"))["etag"]
        # Saved meanwhile: nothing is written over the save, and nothing is left beside it.
        await backend.write("bucket", "key.bin", b"saved meanwhile")
        with pytest.raises(Changed):
            await backend.upload("bucket", "key.bin", source, if_tag=seen)
        assert await backend.read("bucket", "key.bin") == b"saved meanwhile"
        with pytest.raises(Changed):
            await backend.upload("bucket", "key.bin", source, if_absent=True)
        # Gone meanwhile is a change too.
        await backend.delete("bucket", "key.bin")
        with pytest.raises(Changed):
            await backend.upload("bucket", "key.bin", source, if_tag=seen)
        assert not await backend.exists("bucket", "key.bin")
        assert [p.name for p in (tmp_path / "bucket").iterdir()] == []
        # As it was seen, it is written.
        await backend.upload("bucket", "new.bin", source, if_absent=True)
        now = (await backend.stat("bucket", "new.bin"))["etag"]
        await backend.upload("bucket", "new.bin", source, if_tag=now)
        assert await backend.read("bucket", "new.bin") == b"the version"

    async def test_a_delete_takes_away_only_the_object_its_deleter_saw(self, backend: LocalBackend, tmp_path: Path):
        await backend.write("bucket", "deep/key.bin", b"what the deleter saw")
        seen = (await backend.stat("bucket", "deep/key.bin"))["etag"]
        # Saved meanwhile: the save is not taken away.
        await backend.write("bucket", "deep/key.bin", b"saved meanwhile")
        with pytest.raises(Changed):
            await backend.delete("bucket", "deep/key.bin", if_tag=seen)
        assert await backend.read("bucket", "deep/key.bin") == b"saved meanwhile"
        # As it was seen, it is taken away, its empty folders with it.
        now = (await backend.stat("bucket", "deep/key.bin"))["etag"]
        await backend.delete("bucket", "deep/key.bin", if_tag=now)
        assert not await backend.exists("bucket", "deep/key.bin") and not (tmp_path / "bucket" / "deep").exists()
        # Gone meanwhile is a change too; with no tag, a delete is as ever, a no-op on nothing.
        with pytest.raises(Changed):
            await backend.delete("bucket", "deep/key.bin", if_tag=now)
        await backend.delete("bucket", "deep/key.bin")

    async def test_an_upload_that_fails_leaves_the_object_as_it_was(self, backend: LocalBackend, tmp_path: Path):
        await backend.write("bucket", "deep/key.bin", b"what the object held")
        with pytest.raises(OSError):
            await backend.upload("bucket", "deep/key.bin", tmp_path / "no-such-file.bin")
        assert await backend.read("bucket", "deep/key.bin") == b"what the object held"
        assert [p.name for p in (tmp_path / "bucket" / "deep").iterdir()] == ["key.bin"]

    async def test_list_keys(self, backend: LocalBackend):
        await backend.write_text("bucket", "a.txt", "1")
        await backend.write_text("bucket", "b/c.txt", "2")
        await backend.write_text("bucket", "b/d.txt", "3")
        keys = await backend.list_keys("bucket")
        assert keys == ["a.txt", "b/c.txt", "b/d.txt"]

    async def test_list_keys_with_prefix(self, backend: LocalBackend):
        await backend.write_text("bucket", "a.txt", "1")
        await backend.write_text("bucket", "sub/b.txt", "2")
        keys = await backend.list_keys("bucket", prefix="sub")
        assert keys == ["sub/b.txt"]


    async def test_list_entries(self, backend: LocalBackend):
        await backend.write_text("bucket", "a.txt", "12")
        await backend.write_text("bucket", "b/c.txt", "12345")
        entries = await backend.list_entries("bucket")
        assert [e["key"] for e in entries] == ["a.txt", "b/c.txt"]
        sizes = {e["key"]: e["size"] for e in entries}
        assert sizes == {"a.txt": 2, "b/c.txt": 5}
        for entry in entries:
            assert entry["modified"] is not None

    async def test_list_entries_stops_at_a_limit(self, backend: LocalBackend):
        for n in range(20):
            await backend.write_text("bucket", f"many/{n:02}.txt", "x")
        # A caller that must not read a folder of any size asks for one more than it will take.
        assert len(await backend.list_entries("bucket", prefix="many", limit=5)) == 5
        assert len(await backend.list_entries("bucket", prefix="many", limit=50)) == 20
        assert len(await backend.list_entries("bucket", prefix="many")) == 20

    async def test_a_bounded_listing_counts_a_folder_as_the_bucket_counts_its_marker(self, backend: LocalBackend, tmp_path):
        await backend.create_bucket("bucket")
        await backend.write("bucket", "packs/a.pack", b"a")
        for n in range(40):
            (tmp_path / "bucket" / "packs" / f"junk-{n:02d}").mkdir()
        # A folder full of folders is not walked whole to find one file: each is an entry, named as a
        # bucket names a folder's marker, and the listing stops at the limit all the same.
        listed = await backend.list_entries("bucket", prefix="packs", limit=5)
        assert len(listed) == 5 and all(e["key"].endswith("/") for e in listed if "junk" in e["key"])
        # With no limit the listing is the files alone, as it was.
        assert [e["key"] for e in await backend.list_entries("bucket", prefix="packs")] == ["packs/a.pack"]

    async def test_list_entries_with_prefix(self, backend: LocalBackend):
        await backend.write_text("bucket", "a.txt", "x")
        await backend.write_text("bucket", "sub/b.txt", "yy")
        entries = await backend.list_entries("bucket", prefix="sub")
        assert [e["key"] for e in entries] == ["sub/b.txt"]
        assert entries[0]["size"] == 2


class TestLocalBackendSecurity:
    """Path traversal protection."""

    @pytest.fixture()
    async def backend(self, tmp_path: Path) -> LocalBackend:
        b = LocalBackend(base_path=str(tmp_path))
        await b.create_bucket("bucket")
        return b

    async def test_path_traversal_read(self, backend: LocalBackend):
        with pytest.raises(ValueError, match="traversal"):
            await backend.read("bucket", "../../../etc/passwd")

    async def test_path_traversal_write(self, backend: LocalBackend):
        with pytest.raises(ValueError, match="traversal"):
            await backend.write("bucket", "../escape.txt", b"bad")

    async def test_a_mark_is_an_empty_object_with_the_stores_own_date(self, backend: LocalBackend, tmp_path: Path):
        before = time.time()
        dated = await backend.mark("bucket", "proj/_history/pruning")
        assert (tmp_path / "bucket" / "proj" / "_history" / "pruning").read_bytes() == b"" and dated >= before - 1
        # Written again over itself, and dated again.
        os.utime(tmp_path / "bucket" / "proj" / "_history" / "pruning", (before - 3600, before - 3600))
        assert await backend.mark("bucket", "proj/_history/pruning") >= before - 1

    @pytest.mark.parametrize("planted", ["a link at the key", "a link to nothing at the key", "a link at a folder above", "a folder at the key"])
    async def test_a_mark_is_written_through_no_link_and_over_nothing_but_a_plain_file(
        self, backend: LocalBackend, tmp_path: Path, planted: str,
    ):
        # What a thread's commands can leave in a project's folder on a disk: a bucket has no links.
        await backend.write("bucket", "proj/Report.docx", b"the report")
        await backend.write("bucket", "proj/elsewhere/keep.txt", b"kept")
        history = tmp_path / "bucket" / "proj" / "_history"
        if planted == "a link at a folder above":
            history.symlink_to("elsewhere")
        else:
            history.mkdir()
            if planted == "a link at the key":
                (history / "pruning").symlink_to("../Report.docx")
            elif planted == "a link to nothing at the key":
                (history / "pruning").symlink_to("../made-by-the-mark.txt")
            else:
                (history / "pruning").mkdir()
        with pytest.raises(ValueError, match="mark"):
            await backend.mark("bucket", "proj/_history/pruning")
        # Nothing was emptied, and nothing made where the link points.
        assert (tmp_path / "bucket" / "proj" / "Report.docx").read_bytes() == b"the report"
        assert sorted(f.name for f in (tmp_path / "bucket" / "proj" / "elsewhere").iterdir()) == ["keep.txt"]
        assert not (tmp_path / "bucket" / "proj" / "made-by-the-mark.txt").exists()

    async def test_a_mark_over_a_files_second_name_leaves_the_file_its_bytes(self, backend: LocalBackend, tmp_path: Path):
        await backend.write("bucket", "proj/Report.docx", b"the report")
        history = tmp_path / "bucket" / "proj" / "_history"
        history.mkdir()
        os.link(tmp_path / "bucket" / "proj" / "Report.docx", history / "pruning")  # a plain file, and the report itself
        await backend.mark("bucket", "proj/_history/pruning")
        assert (tmp_path / "bucket" / "proj" / "Report.docx").read_bytes() == b"the report"
        assert (history / "pruning").read_bytes() == b"" and sorted(f.name for f in history.iterdir()) == ["pruning"]

    @pytest.mark.parametrize("key", ["../escape", "proj/../../escape", "proj/_history/", "", "proj//pruning", "proj/./pruning"])
    async def test_a_mark_outside_the_bucket_or_at_no_name_is_refused(self, backend: LocalBackend, key: str):
        with pytest.raises(ValueError):
            await backend.mark("bucket", key)


class _Bucket:
    """An S3 client as the upload sees one, which keeps no part: it counts what is on its way to it at once."""

    def __init__(self) -> None:
        self.parts: list[int] = []
        self.whole: int | None = None
        self.done = False

    async def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict:
        self.whole = len(Body)
        return {}

    async def create_multipart_upload(self, *, Bucket: str, Key: str) -> dict:
        return {"UploadId": "u1"}

    async def upload_part(self, *, Body: bytes, **part) -> dict:
        await asyncio.sleep(0.005)  # slower than a file is read: whatever may wait for it does
        self.parts.append(len(Body))
        return {"ETag": f"part-{part['PartNumber']}"}

    async def complete_multipart_upload(self, **whole) -> dict:
        self.done = True
        return {}

    async def abort_multipart_upload(self, **whole) -> dict:
        return {}


class _Objects:
    """An S3 client as a delete sees one: an object's ETag as the store answers it, and what was asked of it, in order."""

    def __init__(self, etag: str | None) -> None:
        self.etag, self.asked = etag, []

    async def head_object(self, *, Bucket: str, Key: str) -> dict:
        self.asked.append(("head", Key))
        if self.etag is None:
            raise KeyError(Key)
        return {"ETag": self.etag, "ContentLength": 1}

    async def delete_object(self, **asked) -> dict:
        self.asked.append(("delete", asked))
        return {}


class TestS3BackendDelete:
    """An object taken away only where it is the one its deleter saw: asked of right before the delete."""

    def backend(self, monkeypatch, client: _Objects) -> S3Backend:
        backend = S3Backend("http://s3.invalid")

        @contextlib.asynccontextmanager
        async def the_client():
            yield client

        monkeypatch.setattr(backend, "_client", the_client)
        return backend

    async def test_a_delete_asks_for_the_object_right_before_it_and_takes_away_only_the_one_its_deleter_saw(self, monkeypatch):
        client = _Objects('"tag-2"')
        backend = self.backend(monkeypatch, client)
        with pytest.raises(Changed):
            await backend.delete("bucket", "key.bin", if_tag='"tag-1"')
        assert client.asked == [("head", "key.bin")]
        await backend.delete("bucket", "key.bin", if_tag='"tag-2"')
        assert client.asked[1:] == [("head", "key.bin"), ("delete", {"Bucket": "bucket", "Key": "key.bin"})]
        # Gone meanwhile is a change; with no tag, nothing is asked first.
        gone = _Objects(None)
        with pytest.raises(Changed):
            await self.backend(monkeypatch, gone).delete("bucket", "key.bin", if_tag='"tag-2"')
        await self.backend(monkeypatch, gone).delete("bucket", "key.bin")
        assert gone.asked == [("head", "key.bin"), ("delete", {"Bucket": "bucket", "Key": "key.bin"})]


class TestS3BackendUpload:
    """An object written from a file through the S3 client's own upload, with a client that stores nothing."""

    @pytest.fixture()
    def bucket(self, monkeypatch) -> tuple[S3Backend, _Bucket]:
        backend, client = S3Backend("http://s3.invalid"), _Bucket()
        client.upload_file = functools.partial(inject.upload_file, client)

        @contextlib.asynccontextmanager
        async def the_client():
            yield client

        monkeypatch.setattr(backend, "_client", the_client)
        return backend, client

    async def test_a_large_file_goes_up_in_parts_a_few_held_at_once_whatever_its_size(self, bucket, tmp_path: Path):
        backend, client = bucket
        source = tmp_path / "source.bin"
        with open(source, "wb") as out:
            out.truncate(256 * 2**20)
        tracemalloc.start()
        await backend.upload("bucket", "deep/key.bin", source)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        # Every byte went up, in parts of 8 MiB; and of the 256 MiB the upload held a few parts at once, never the file.
        assert (client.done, sum(client.parts), max(client.parts)) == (True, 256 * 2**20, 8 * 2**20)
        assert peak < 64 * 2**20, peak

    async def test_a_small_file_goes_up_whole(self, bucket, tmp_path: Path):
        backend, client = bucket
        source = tmp_path / "source.bin"
        source.write_bytes(b"a small file")
        await backend.upload("bucket", "key.bin", source)
        assert (client.whole, client.parts) == (12, [])
