"""A thread's pod: the executor daemon over the project's real files and the thread's copy."""

from __future__ import annotations

import base64
import json
import subprocess

import httpx
import pytest

from surogates.sandbox import executor_server
from surogates.sandbox.base import SandboxSpec
from surogates.sandbox.history import History, HistoryError
from surogates.tools.utils.checkpoint_manager import _shadow_repo_path
from tests.thread_pods import ThreadPods

AUTH = {"Authorization": "Bearer t"}
THREAD = {"name": "Draft A", "email": "thread:t1@surogate"}


@pytest.fixture()
def pods(tmp_path) -> ThreadPods:
    pods = ThreadPods(tmp_path)
    (pods.project / "Report.docx").write_bytes(b"report v1")
    return pods


async def a_pod(pods: ThreadPods) -> str:
    return await pods.provision(SandboxSpec(env={"HISTORY_THREAD": "t1", "USER_ID": "u1"}))


async def call(pods, pod, name, **args) -> dict:
    return json.loads(await pods.execute(pod, name, json.dumps(args)))


async def test_a_thread_pod_is_ready_once_the_real_files_are_mounted_and_its_copy_is_made(tmp_path):
    project, copy = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    copy.mkdir()
    (project / "Report.docx").write_bytes(b"report v1")
    mounts = tmp_path / "mounts"
    history = History(
        repo=_shadow_repo_path(str(project), base=tmp_path / "home"),
        project=project, copy=copy, thread="t1", user="u1",
    )
    app = executor_server.create_app(token="t", workspace=str(copy), mounts_path=str(mounts), history=history)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod") as client:
        # /workspace is the copy, on the pod's disk: only /project is mounted.
        mounts.write_text(f"geesefs {copy} fuse.geesefs rw 0 0\n")
        assert (await client.get("/healthz")).status_code == 503
        assert not (copy / "Report.docx").exists()
        mounts.write_text(f"geesefs {project} fuse.geesefs rw 0 0\n")
        assert (await client.get("/healthz")).status_code == 200
    assert (copy / "Report.docx").read_bytes() == b"report v1"
    assert not (copy / ".git").exists()


async def test_a_thread_pods_tools_work_on_its_copy_and_its_checkpoints_restore_it(pods):
    pod = await a_pod(pods)
    copy = pods.copies["t1"]
    taken = await call(pods, pod, "_checkpoint", action="take", reason="before write_file")
    assert taken["success"] and taken["hash"]
    written = await call(pods, pod, "write_file", path="threads/Draft A/notes.md", content="notes")
    assert "error" not in written, written
    assert (copy / "threads" / "Draft A" / "notes.md").read_text() == "notes"
    assert not (pods.project / "threads").exists()

    restored = await call(pods, pod, "_checkpoint", action="restore", hash=taken["hash"])
    assert restored["success"], restored
    assert not (copy / "threads" / "Draft A" / "notes.md").exists()
    assert (await call(pods, pod, "_checkpoint", action="restore", hash="0" * 40))["success"] is False


async def test_a_thread_pod_lands_through_its_history_steps(pods):
    pod = await a_pod(pods)
    (pods.copies["t1"] / "Report.docx").write_bytes(b"report v2")
    turn = await call(pods, pod, "_history", action="commit", author=THREAD, trailers=[["Surogate-Kind", "turn"]])
    [change] = turn["changes"]
    applied = await call(pods, pod, "_history", action="apply", **change)
    assert (pods.project / "Report.docx").read_bytes() == b"report v2"
    record = await call(
        pods, pod, "_history", action="record",
        turn=turn["commit"], applied=[applied], author=THREAD, trailers=[["Surogate-Kind", "landing"]],
    )
    assert len(record["commit"]) == 40
    # A conflict is an error result, so the landing saga fails the step.
    (pods.project / "Report.docx").write_bytes(b"saved by you")
    again = await call(pods, pod, "_history", action="apply", **change)
    assert "changed since the thread started" in again["error"]
    assert "error" in await call(pods, pod, "_history", action="rewrite")


async def test_a_pod_writes_and_reads_a_file_of_its_copy_for_the_harness(pods, monkeypatch):
    pod = await a_pod(pods)
    copy = pods.copies["t1"]
    png = b"\x89PNG\r\n\x1a\n fake"
    written = await call(pods, pod, "_file", action="write", path="images/chart.png", content_b64=base64.b64encode(png).decode())
    assert written == {"ok": True, "bytes": len(png)}
    assert (copy / "images" / "chart.png").read_bytes() == png
    assert not (pods.project / "images").exists()
    read = await call(pods, pod, "_file", action="read", path=str(copy / "images" / "chart.png"))
    assert base64.b64decode(read["content_b64"]) == png

    assert "outside" in (await call(pods, pod, "_file", action="read", path="../project/Report.docx"))["error"]
    monkeypatch.setattr(executor_server, "_MAX_FILE_BYTES", 4)
    assert "50 MiB" in (await call(pods, pod, "_file", action="read", path="images/chart.png"))["error"]


async def test_a_pod_without_a_copy_refuses_history_steps(tmp_path):
    app = executor_server.create_app(token="t", workspace=str(tmp_path), require_fuse=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod") as client:
        response = await client.post("/execute", json={"name": "_history", "args": {"action": "commit"}}, headers=AUTH)
    assert "no copy" in response.json()["error"]


def a_thread_app(tmp_path):
    project, copy = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    copy.mkdir()
    (project / "Report.docx").write_bytes(b"report v1")
    history = History(
        repo=_shadow_repo_path(str(project), base=tmp_path / "home"),
        project=project, copy=copy, thread="t1", user="u1",
    )
    app = executor_server.create_app(token="t", workspace=str(copy), require_fuse=False, history=history)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod"), copy


async def test_a_thread_pod_makes_its_copy_again_after_a_failed_read_of_the_real_files(tmp_path, monkeypatch):
    client, copy = a_thread_app(tmp_path)
    git = History._git

    def unreadable(self, args, **kwargs):
        if args[0] == "add":
            raise HistoryError("git add failed: Input/output error")
        return git(self, args, **kwargs)

    monkeypatch.setattr(History, "_git", unreadable)
    async with client:
        assert (await client.get("/healthz")).status_code == 503
        monkeypatch.setattr(History, "_git", git)
        assert (await client.get("/healthz")).status_code == 200
    assert (copy / "Report.docx").read_bytes() == b"report v1"


async def test_a_thread_pod_whose_copy_was_half_made_stays_not_ready(tmp_path, monkeypatch):
    client, _ = a_thread_app(tmp_path)
    git = History._git

    def cut_short(self, args, **kwargs):
        out = git(self, args, **kwargs)
        if args[0] == "worktree":
            raise HistoryError("git worktree timed out after 120s")
        return out

    monkeypatch.setattr(History, "_git", cut_short)
    async with client:
        first = await client.get("/healthz")
        monkeypatch.setattr(History, "_git", git)
        again = await client.get("/healthz")
    # Not made again over the half-made copy: the pod is replaced.
    assert (first.status_code, again.status_code) == (503, 503)
    assert again.text == first.text == "copy not made: git worktree timed out after 120s"


async def test_a_history_step_that_times_out_answers_an_error(pods, monkeypatch):
    pod = await a_pod(pods)
    (pods.copies["t1"] / "Report.docx").write_bytes(b"report v2")
    turn = await call(pods, pod, "_history", action="commit", author=THREAD, trailers=[["Surogate-Kind", "turn"]])

    def timed_out(self, path, blob):
        raise subprocess.TimeoutExpired(["git", "cat-file", "blob", blob], 120)

    monkeypatch.setattr(History, "_put", timed_out)
    answer = await call(pods, pod, "_history", action="apply", **turn["changes"][0])
    assert list(answer) == ["error"] and "timed out after 120 seconds" in answer["error"]
    assert (await pods.pods[pod].get("/healthz")).status_code == 200
