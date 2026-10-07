"""The harness tools that reach a project's files through object storage use a thread's copy instead."""

from __future__ import annotations

import base64
import json
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.harness.loop import AgentHarness
from surogates.sandbox.base import SandboxSpec, SandboxUnavailableError
from surogates.sandbox.copy_files import read_copy, write_copy
from surogates.sandbox.pool import SandboxPool
from surogates.tools.builtin.browser import _browser_screenshot_handler
from surogates.tools.builtin.media_gen import _save_media_bytes
from surogates.tools.builtin.vision import _image_ref_to_data_url
from tests.test_browser_tools import FakeControlStore, FakePool, FakeSavePathScreenshotClient, FakeStorage
from tests.thread_pods import ThreadPods

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
CONFIG = {"storage_bucket": "agent-bucket"}


class RealFiles(FakeStorage):
    """Object storage holding the project's real files."""

    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__()
        self.files = files

    async def read(self, bucket: str, key: str) -> bytes:
        name = key.rsplit("/", 1)[-1]
        if name not in self.files:
            raise KeyError(key)
        return self.files[name]


@pytest.fixture()
async def thread(tmp_path):
    """A thread whose pod holds its copy: ``(pool, owner, pods)``."""
    pods = ThreadPods(tmp_path)
    (pods.project / "chart.png").write_bytes(PNG + b"real")
    pool = SandboxPool(pods)
    owner = str(uuid4())
    await pool.ensure(owner, SandboxSpec(env={"PROJECT_DIR": "/project", "HISTORY_THREAD": owner, "USER_ID": "u1"}))
    return pool, owner, pods


async def test_a_threads_generated_image_goes_to_its_copy(thread):
    pool, owner, pods = thread
    storage = FakeStorage()
    saved = await _save_media_bytes(
        PNG, relative_path="images/cover.png", workspace_path=None, storage=storage,
        session_id=owner, session_config=CONFIG, sandbox_pool=pool, owner=owner,
    )
    assert saved is True
    assert (pods.copies[owner] / "images" / "cover.png").read_bytes() == PNG
    # Not the real files: it reaches them when the turn lands.
    assert storage.writes == [] and not (pods.project / "images").exists()


async def test_a_threads_image_with_no_copy_to_go_to_is_not_saved(thread):
    pool, owner, pods = thread
    storage = FakeStorage()
    saved = await _save_media_bytes(
        PNG, relative_path="images/cover.png", workspace_path=None, storage=storage,
        session_id=owner, session_config={**CONFIG, "workstream_role": "thread"}, sandbox_pool=pool, owner=str(uuid4()),
    )
    # With its pod gone, the image would go around the landing, into the real files.
    assert saved is False and storage.writes == []


async def test_any_other_sessions_generated_image_goes_to_storage_as_before():
    storage = FakeStorage()
    saved = await _save_media_bytes(
        PNG, relative_path="images/cover.png", workspace_path=None, storage=storage,
        session_id=str(uuid4()), session_config=CONFIG, sandbox_pool=None, owner=None,
    )
    assert saved is True and [w[2] for w in storage.writes] == [PNG]


async def test_a_thread_with_no_storage_keeps_its_images_in_its_workspace_as_before(tmp_path):
    # With no storage bucket a thread has no copy: its workspace is the project's files.
    saved = await _save_media_bytes(
        PNG, relative_path="images/cover.png", workspace_path=str(tmp_path), storage=None,
        session_id=str(uuid4()), session_config={"workstream_role": "thread"}, sandbox_pool=None, owner=None,
    )
    assert saved is True and (tmp_path / "images" / "cover.png").read_bytes() == PNG


async def test_a_threads_screenshot_goes_to_its_copy_and_the_browser_writes_no_real_file(thread):
    pool, owner, pods = thread
    storage, client = FakeStorage(), FakeSavePathScreenshotClient()
    body = json.loads(await _browser_screenshot_handler(
        {}, tenant=SimpleNamespace(org_id=uuid4(), user_id=uuid4()), session_id=owner,
        browser_pool=FakePool(), browser_control=FakeControlStore(), workspace_path="/workspace",
        session_config=CONFIG, storage=storage, sandbox_pool=pool, task_id=owner,
        _client_factory=lambda endpoint: client,
    ))
    assert body["saved"] is True, body
    assert client.captured[0]["save_path"] is None
    assert storage.writes == []
    assert (pods.copies[owner] / body["relative_path"]).read_bytes().startswith(b"\x89PNG")


async def test_a_thread_sees_an_image_as_its_copy_has_it(thread):
    pool, owner, pods = thread
    (pods.copies[owner] / "chart.png").write_bytes(PNG + b"redrawn")
    real = RealFiles({"chart.png": PNG + b"real"})
    url, _ = await _image_ref_to_data_url(
        "chart.png", workspace_path="/workspace", storage=real, session_id=owner,
        session_config=CONFIG, sandbox_pool=pool, owner=owner,
    )
    assert base64.b64decode(url.split(",", 1)[1]) == PNG + b"redrawn"
    # A thread with no pod yet has changed nothing: the real file is its copy.
    url, _ = await _image_ref_to_data_url(
        "chart.png", workspace_path="/workspace", storage=real, session_id=owner,
        session_config=CONFIG, sandbox_pool=pool, owner=str(uuid4()),
    )
    assert base64.b64decode(url.split(",", 1)[1]) == PNG + b"real"


@pytest.mark.parametrize("answer", [
    # A child killed at its timeout, and a daemon's HTTP failure: neither carries an error.
    {"exit_code": -1, "stdout": "", "stderr": "Execution timed out", "truncated": False, "timed_out": True},
    {"exit_code": -1, "stdout": "", "stderr": "Executor daemon error (HTTP 500)", "truncated": False, "timed_out": False},
    SandboxUnavailableError("Sandbox daemon unreachable"),
])
async def test_a_pods_answer_that_is_not_the_file_is_an_error(thread, monkeypatch, answer):
    pool, owner, pods = thread

    async def answering(*_: object) -> str:
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer)

    monkeypatch.setattr(pool._backend, "execute", answering)
    said = re.escape(str(answer) if isinstance(answer, Exception) else answer["stderr"])
    with pytest.raises(ValueError, match=said):
        await read_copy(pool, owner, "chart.png")
    with pytest.raises(ValueError, match=said):
        await write_copy(pool, owner, "images/cover.png", PNG)


async def test_an_image_injected_after_a_tool_is_read_from_the_copy(thread):
    pool, owner, pods = thread
    (pods.copies[owner] / "chart.png").write_bytes(PNG + b"redrawn")
    harness = AgentHarness.__new__(AgentHarness)
    harness._sandbox_pool = pool
    harness._storage = RealFiles({"chart.png": PNG + b"real"})
    session = SimpleNamespace(id=uuid4(), parent_id=None, config={**CONFIG, "sandbox_root_session_id": owner})
    assert await harness._read_workspace_image(session, "chart.png") == PNG + b"redrawn"
