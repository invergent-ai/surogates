"""Writes on the computer guarded by the file's revision, as the office editor's saves need them."""

from __future__ import annotations

import pytest

from surogates.tools.workspace_io import RevisionConflict
from tests.device_computer import computer_io

FOLDER = "/home/me/notes"
KEY = f"{FOLDER}/a.docx"


async def test_etag_is_the_files_revision_and_none_where_nothing_is():
    io = computer_io(FOLDER, {"a.docx": b"one"})
    assert await io.etag(KEY) == io.runner.files[KEY][1]
    assert await io.etag(f"{FOLDER}/missing.docx") is None


async def test_write_if_writes_over_the_revision_it_expects():
    io = computer_io(FOLDER, {"a.docx": b"one"})
    revision = await io.etag(KEY)
    await io.write_if(KEY, b"two", expected=revision)
    assert io.runner.files[KEY][0] == b"two"
    assert ("write", {"key": KEY, "data": "dHdv", "expected_revision": revision}) in io.runner.calls


async def test_write_if_refuses_a_file_changed_since():
    io = computer_io(FOLDER, {"a.docx": b"one"})
    with pytest.raises(RevisionConflict):
        await io.write_if(KEY, b"two", expected="1:0:0:0:0")
    assert io.runner.files[KEY][0] == b"one"


async def test_write_if_without_an_expectation_only_makes_a_new_file():
    io = computer_io(FOLDER, {"a.docx": b"one"})
    await io.write_if(f"{FOLDER}/b.docx", b"new", expected=None)
    assert io.runner.files[f"{FOLDER}/b.docx"][0] == b"new"
    assert io.runner.calls[-1][1]["create"] is True
    with pytest.raises(RevisionConflict):
        await io.write_if(KEY, b"two", expected=None)
    assert io.runner.files[KEY][0] == b"one"


async def test_a_plain_write_asks_for_neither_a_revision_nor_a_new_file():
    io = computer_io(FOLDER)
    await io.write(KEY, b"x")
    assert io.runner.calls[-1] == ("write", {"key": KEY, "data": "eA=="})
