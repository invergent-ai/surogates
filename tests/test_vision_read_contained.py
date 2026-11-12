"""An image of this host's workspace is read through the no-follow walk, never by path after its check.

The last branch of ``_image_ref_to_data_url`` checked the path (``validate_path``), then took ``is_file``, ``stat``
and ``read_bytes`` by path: a folder swapped for a link in between made the worker (which srt does not confine) read
an image outside the workspace into the model's context, and its "too large" error told the size of any file there.
It now reads as ``LocalWorkspaceIO`` does since #655.
"""

from __future__ import annotations

import os

import pytest

from surogates.tools.builtin import vision
from surogates.tools.builtin.vision import _MAX_IMAGE_BYTES, _image_ref_to_data_url
from surogates.tools.utils.workspace_sandbox import WorkspaceSandboxError

pytestmark = pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="POSIX only")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def places(tmp_path):
    ws, out = tmp_path / "ws", tmp_path / "outside"
    (ws / "pics").mkdir(parents=True)
    (ws / "pics" / "chart.png").write_bytes(PNG)
    (out).mkdir()
    (out / "chart.png").write_bytes(PNG + b"OUTSIDE")
    with open(out / "big.bin", "wb") as fh:
        fh.truncate(_MAX_IMAGE_BYTES + 12345)
    return ws, out


def _swap_after_the_check(monkeypatch, ws, out):
    real = vision.validate_path

    def validate_then_swap(root, ref):
        key = real(root, ref)
        (ws / "pics").rename(ws / "pics.old")
        (ws / "pics").symlink_to(out, target_is_directory=True)  # a command swaps it after the check
        return key

    monkeypatch.setattr(vision, "validate_path", validate_then_swap)


async def test_an_image_in_the_workspace_is_read(places):
    ws, _ = places
    url, source = await _image_ref_to_data_url("pics/chart.png", workspace_path=str(ws))
    assert source == "workspace_file" and url.startswith("data:image/png;base64,")


async def test_a_folder_swapped_after_the_check_reads_no_image_outside(places, monkeypatch):
    ws, out = places
    _swap_after_the_check(monkeypatch, ws, out)
    with pytest.raises((ValueError, WorkspaceSandboxError)) as refused:
        await _image_ref_to_data_url("pics/chart.png", workspace_path=str(ws))
    assert "OUTSIDE" not in str(refused.value) and str(out) not in str(refused.value)


async def test_a_folder_swapped_after_the_check_tells_no_size_outside(places, monkeypatch):
    ws, out = places
    (ws / "pics" / "big.bin").write_bytes(b"small")
    _swap_after_the_check(monkeypatch, ws, out)
    with pytest.raises((ValueError, WorkspaceSandboxError)) as refused:
        await _image_ref_to_data_url("pics/big.bin", workspace_path=str(ws))
    assert str(_MAX_IMAGE_BYTES + 12345) not in str(refused.value)


async def test_without_a_workspace_or_out_of_it_it_is_refused_as_before(places):
    ws, out = places
    with pytest.raises(WorkspaceSandboxError):
        await _image_ref_to_data_url(str(out / "chart.png"), workspace_path=str(ws))
    with pytest.raises(WorkspaceSandboxError):
        await _image_ref_to_data_url("pics/chart.png", workspace_path=None)


async def test_a_folder_swapped_after_the_workspaces_own_check_is_refused_by_the_walk(places, monkeypatch):
    """The read itself is contained: swapped after LocalWorkspaceIO.resolve, the walk refuses it."""
    from surogates.tools.workspace_io.local import LocalWorkspaceIO

    ws, out = places
    real = LocalWorkspaceIO.resolve

    async def resolve_then_swap(self, path):
        key = await real(self, path)
        (ws / "pics").rename(ws / "pics.old")
        (ws / "pics").symlink_to(out, target_is_directory=True)
        return key

    monkeypatch.setattr(LocalWorkspaceIO, "resolve", resolve_then_swap)
    with pytest.raises(ValueError) as refused:  # the walk sees no file there: nothing outside is stat'ed or read
        await _image_ref_to_data_url("pics/chart.png", workspace_path=str(ws))
    assert "OUTSIDE" not in str(refused.value)


async def test_a_folder_swapped_between_the_stat_and_the_read_is_refused(places, monkeypatch):
    """The read walks again: swapped after the stat found the image, it reads nothing outside."""
    from surogates.tools.workspace_io.local import LocalWorkspaceIO

    ws, out = places
    real = LocalWorkspaceIO.stat

    async def stat_then_swap(self, key):
        found = await real(self, key)
        (ws / "pics").rename(ws / "pics.old")
        (ws / "pics").symlink_to(out, target_is_directory=True)
        return found

    monkeypatch.setattr(LocalWorkspaceIO, "stat", stat_then_swap)
    with pytest.raises(ValueError, match="Could not read image pics/chart.png: Refused") as refused:
        await _image_ref_to_data_url("pics/chart.png", workspace_path=str(ws))
    assert "OUTSIDE" not in str(refused.value) and str(ws) not in str(refused.value)
