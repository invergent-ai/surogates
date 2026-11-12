"""Generated media and browser screenshots are written into the workspace, never through a planted link.

``_save_media_bytes`` wrote ``<file>.tmp`` by path and renamed it over the file, and the browser wrote its
screenshot by path: a link planted at ``<file>.tmp`` (or at the file, or a folder swapped for a link after the path
was checked) made the worker, which srt does not confine, overwrite a file outside the workspace.  Both now write as
``LocalWorkspaceIO.write`` does since #651: the folder reached with no link followed, a temporary file made new
(O_EXCL | O_NOFOLLOW), renamed over the file in that folder.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from surogates.tools.builtin.browser import _save_screenshot_to_workspace
from surogates.tools.builtin.media_gen import _save_media_bytes

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")

PNG = b"\x89PNG\r\n\x1a\nmedia"


@pytest.fixture
def places(tmp_path):
    ws, out = tmp_path / "ws", tmp_path / "outside"
    (ws / "images").mkdir(parents=True)
    out.mkdir()
    (out / "victim").write_text("not the workspace's")
    return ws, out


async def _save(ws, relative_path="images/cover.png"):
    return await _save_media_bytes(
        PNG, relative_path=relative_path, workspace_path=str(ws), storage=None,
        session_id=str(uuid4()), session_config={}, sandbox_pool=None, owner=None,
    )


async def test_a_link_planted_at_the_tmp_name_does_not_carry_the_media_outside(places):
    ws, out = places
    (ws / "images" / "cover.png.tmp").symlink_to(out / "victim")
    assert await _save(ws) is True
    assert (out / "victim").read_text() == "not the workspace's"
    assert (ws / "images" / "cover.png").read_bytes() == PNG and not (ws / "images" / "cover.png").is_symlink()


async def test_a_folder_swapped_after_the_check_does_not_carry_the_media_outside(places, monkeypatch):
    ws, out = places
    from surogates.tools.builtin import media_gen

    real_validate = media_gen.validate_path

    def validate_then_swap(root, path):
        key = real_validate(root, path)
        (ws / "images").rename(ws / "images.old")
        (ws / "images").symlink_to(out, target_is_directory=True)  # a command swaps it after the check
        return key

    monkeypatch.setattr(media_gen, "validate_path", validate_then_swap)
    assert await _save(ws) is False
    assert sorted(p.name for p in out.iterdir()) == ["victim"]


async def test_media_is_still_written_into_new_folders(places):
    ws, _ = places
    assert await _save(ws, "made/now/clip.png") is True
    assert (ws / "made" / "now" / "clip.png").read_bytes() == PNG


def test_a_link_planted_at_the_screenshots_name_does_not_carry_it_outside(places):
    ws, out = places
    (ws / "shots").mkdir()
    (ws / "shots" / "page.png").symlink_to(out / "victim")
    # The path check resolves the link out of the workspace: not saved, and nothing outside written.
    assert _save_screenshot_to_workspace(PNG, workspace_path=str(ws), relative_path="shots/page.png") is None
    assert (out / "victim").read_text() == "not the workspace's"


def test_a_screenshot_folder_that_is_a_link_out_is_refused(places):
    ws, out = places
    (ws / "shots").symlink_to(out, target_is_directory=True)
    assert _save_screenshot_to_workspace(PNG, workspace_path=str(ws), relative_path="shots/page.png") is None
    assert sorted(p.name for p in out.iterdir()) == ["victim"]
