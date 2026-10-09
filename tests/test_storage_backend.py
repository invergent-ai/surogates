"""Local storage object operations and workspace path protection."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from surogates.storage.backend import (
    LocalBackend,
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
