"""The harness's own files in a local folder never reach the user's git."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.artifacts.models import ArtifactKind
from surogates.artifacts.store import ArtifactStore
from surogates.devices.workspace import DeviceWorkspaceIO
from surogates.storage.skill_staging import stage_in_folder
from surogates.tools.builtin.browser import _screenshot_in_folder
from surogates.tools.builtin.terminal import _spill_full_output
from surogates.tools.utils.tool_result_storage import make_sandbox_writer
from surogates.tools.workspace_io import LocalWorkspaceIO
from tests.fake_laptop import InProcessRunner

pytestmark = pytest.mark.asyncio


async def an_artifact(files):
    root = str(uuid4())
    await ArtifactStore(files, session_id=uuid4(), root=root).create(
        name="notes", kind=ArtifactKind.MARKDOWN, spec={"content": "x"},
    )


async def a_staged_skill(files):
    async def fetch(path: str) -> bytes:
        return b"print('x')"

    await stage_in_folder(files, skill_name="deck", linked_files=["scripts/build.py"], owner="root", fetch=fetch)


async def a_screenshot(files):
    assert json.loads(await _screenshot_in_folder(b"\x89PNG", {}, files))["saved"] is True


async def a_terminal_spill(files):
    assert await _spill_full_output("x" * 10, files) is not None


async def a_result_spill(files):
    async def execute(owner, name, args):
        # The pool's write_file, run on the computer as a tool call's DeviceCall runs it.
        written = json.loads(args)
        await files.write(await files.resolve(written["path"]), written["content"].encode())
        return json.dumps({"status": "ok"})

    assert await make_sandbox_writer(SimpleNamespace(workspace_io=files, execute=execute), "root")(
        ".surogates-results/call_1.txt", "x",
    )


def git(folder: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("harness_write", [an_artifact, a_staged_skill, a_screenshot, a_terminal_spill, a_result_spill])
async def test_nothing_the_harness_keeps_in_a_folder_shows_in_its_git_status(tmp_path, harness_write):
    folder = tmp_path.resolve()
    git(folder, "init", "-q")
    (folder / "README.md").write_text("mine\n")

    await harness_write(DeviceWorkspaceIO(InProcessRunner(LocalWorkspaceIO(workspace_path=str(folder))), root=str(folder)))

    assert git(folder, "status", "--porcelain", "--untracked-files=all").splitlines() == ["?? README.md"]
    assert (folder / ".surogates-results" / ".gitignore").read_bytes() == b"*\n"
    # It lands: only the model's own writes there are refused.
    assert any(path.is_file() and path.name != ".gitignore" for path in (folder / ".surogates-results").rglob("*"))


async def test_a_folders_own_ignore_file_there_is_kept_and_asked_after_once_per_call(tmp_path):
    folder = tmp_path.resolve()
    (folder / ".surogates-results").mkdir()
    (folder / ".surogates-results" / ".gitignore").write_text("# the user's own\n*\n")
    runner = InProcessRunner(LocalWorkspaceIO(workspace_path=str(folder)))
    files = DeviceWorkspaceIO(runner, root=str(folder))
    await an_artifact(files)
    assert (folder / ".surogates-results" / ".gitignore").read_text() == "# the user's own\n*\n"
    assert runner.kinds.count("stat") == 1


async def test_a_cloud_spill_writes_no_ignore_file(tmp_path):
    workspace = tmp_path.resolve()
    assert await _spill_full_output("x" * 10, LocalWorkspaceIO(workspace_path=str(workspace))) is not None
    assert not (workspace / ".surogates-results" / ".gitignore").exists()
