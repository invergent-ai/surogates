"""StorageWorkspaceIO.write_if: a write that refuses to overwrite a changed file."""

from __future__ import annotations

import pytest

from surogates.storage.backend import LocalBackend
from surogates.tools.workspace_io import RevisionConflict, StorageWorkspaceIO


@pytest.fixture
def files(tmp_path):
    return StorageWorkspaceIO(LocalBackend(str(tmp_path)), bucket="b", prefix="s1/")


async def test_location_names_the_object(files):
    assert files.location("docs/a.docx") == "bucket:b/s1/docs/a.docx"


async def test_write_over_the_expected_version(files):
    await files.write("a.docx", b"one")
    tag = await files.etag("a.docx")
    await files.write_if("a.docx", b"two", expected=tag)
    assert await files.read("a.docx") == b"two"
    assert await files.etag("a.docx") != tag


async def test_changed_file_is_not_overwritten(files):
    await files.write("a.docx", b"one")
    tag = await files.etag("a.docx")
    await files.write("a.docx", b"someone else")
    with pytest.raises(RevisionConflict):
        await files.write_if("a.docx", b"mine", expected=tag)
    assert await files.read("a.docx") == b"someone else"


async def test_deleted_file_is_not_resurrected(files):
    await files.write("a.docx", b"one")
    tag = await files.etag("a.docx")
    await files.delete("a.docx")
    with pytest.raises(RevisionConflict):
        await files.write_if("a.docx", b"mine", expected=tag)
    assert await files.stat("a.docx") is None


async def test_new_file_only_where_nothing_is(files):
    await files.write_if("copy.docx", b"x", expected=None)
    with pytest.raises(RevisionConflict):
        await files.write_if("copy.docx", b"y", expected=None)
    assert await files.read("copy.docx") == b"x"


async def test_etag_of_a_missing_file(files):
    assert await files.etag("nope.docx") is None
