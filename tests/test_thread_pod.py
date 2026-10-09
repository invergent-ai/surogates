"""A thread's pod: the executor daemon over the project's real files and the thread's copy."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import subprocess

import httpx
import pytest

from surogates.governance.saga.compensator import compensate_history
from surogates.governance.saga.state_machine import SagaStep
from surogates.harness import landing
from surogates.sandbox import executor_server
from surogates.sandbox.base import SandboxSpec
from surogates.sandbox.pool import SandboxPool
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
    turn = await call(pods, pod, "_history", action="commit", author=THREAD, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])
    [change] = turn["changes"]
    applied = await call(pods, pod, "_history", action="apply", **change)
    assert (pods.project / "Report.docx").read_bytes() == b"report v2"
    record = await call(
        pods, pod, "_history", action="record",
        turn=turn["commit"], applied=[applied], author=THREAD, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "landing"]], main=None,
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
    monkeypatch.setattr(executor_server, "MAX_FILE_BYTES", 4)
    assert "50 MiB" in (await call(pods, pod, "_file", action="read", path="images/chart.png"))["error"]


async def test_a_pod_without_a_copy_refuses_history_steps(tmp_path):
    app = executor_server.create_app(token="t", workspace=str(tmp_path), require_fuse=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod") as client:
        response = await client.post("/execute", json={"name": "_history", "args": {"action": "commit"}}, headers=AUTH)
    assert "no copy" in response.json()["error"]


def a_thread_app(tmp_path, **kwargs):
    project, copy = tmp_path / "project", tmp_path / "workspace"
    project.mkdir()
    copy.mkdir()
    (project / "Report.docx").write_bytes(b"report v1")
    history = History(
        repo=_shadow_repo_path(str(project), base=tmp_path / "home"),
        project=project, copy=copy, thread="t1", user="u1",
    )
    app = executor_server.create_app(token="t", workspace=str(copy), require_fuse=False, history=history, **kwargs)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod"), copy


async def test_a_thread_pod_makes_its_copy_again_after_a_failed_read_of_the_real_files(tmp_path, monkeypatch):
    client, copy = a_thread_app(tmp_path)
    git = History._git

    def unreadable(self, args, **kwargs):
        # Whichever git reads the real files: the readers, and the one that reads alone after them.
        if args[0] in ("update-index", "add") and kwargs["env"].get("GIT_WORK_TREE") == str(self.project):
            raise HistoryError(f"git {args[0]} failed: Input/output error")
        return git(self, args, **kwargs)

    monkeypatch.setattr(History, "_git", unreadable)
    async with client:
        assert (await client.get("/healthz")).status_code == 503
        monkeypatch.setattr(History, "_git", git)
        assert (await client.get("/healthz")).status_code == 200
    assert (copy / "Report.docx").read_bytes() == b"report v1"


async def test_a_thread_pod_whose_copy_was_half_made_gives_up_with_its_reason(tmp_path, monkeypatch):
    reasons: list[str] = []
    client, _ = a_thread_app(tmp_path, give_up=reasons.append)
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
    # It gives up at once, with its reason, rather than waiting out the ready timeout.
    assert reasons == ["copy not made: git worktree timed out after 120s"]


def test_a_daemon_that_gives_up_leaves_its_reason_as_the_pods_termination_message(tmp_path, monkeypatch):
    exits: list[int] = []
    monkeypatch.setattr(executor_server.os, "_exit", exits.append)
    executor_server._give_up("copy not made: no space left", log=tmp_path / "termination-log")
    assert ((tmp_path / "termination-log").read_text(), exits) == ("copy not made: no space left", [1])


def test_a_thread_pod_without_its_user_refuses_to_start(tmp_path, monkeypatch, caplog):
    env = {"TOOL_EXECUTOR_TOKEN": "t", "PROJECT_DIR": str(tmp_path), "HISTORY_THREAD": "t1", "WORKSPACE_DIR": str(tmp_path / "w")}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("USER_ID", raising=False)
    monkeypatch.setattr(executor_server, "init_registry", lambda: pytest.fail("started without a user"))
    with pytest.raises(SystemExit) as exited:
        executor_server.main()
    assert exited.value.code == 1 and "USER_ID" in caplog.text


async def test_a_history_step_that_times_out_answers_an_error(pods, monkeypatch):
    pod = await a_pod(pods)
    (pods.copies["t1"] / "Report.docx").write_bytes(b"report v2")
    turn = await call(pods, pod, "_history", action="commit", author=THREAD, trailers=[["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]])

    def timed_out(self, path, blob):
        raise subprocess.TimeoutExpired(["git", "cat-file", "blob", blob], 120)

    monkeypatch.setattr(History, "_put", timed_out)
    answer = await call(pods, pod, "_history", action="apply", **turn["changes"][0])
    assert list(answer) == ["error"] and "timed out after 120 seconds" in answer["error"]
    assert (await pods.pods[pod].get("/healthz")).status_code == 200


async def test_every_file_failure_answers_an_error_and_a_write_is_whole_or_not_at_all(pods, monkeypatch):
    pod = await a_pod(pods)
    copy = pods.copies["t1"]
    (copy / "folder").mkdir()
    (copy / "notes.md").write_text("notes v1")
    x = base64.b64encode(b"x").decode()
    for args in (
        {"action": "write", "path": "notes.md", "content_b64": "not base64"},
        {"action": "write", "path": "folder", "content_b64": x},
        {"action": "write", "path": "Report.docx/x", "content_b64": x},
        {"action": "read", "path": "a\0b"},
        {"action": "write", "path": "../project/x", "content_b64": x},
    ):
        answer = await call(pods, pod, "_file", **args)
        assert list(answer) == ["error"], (args, answer)
    assert not (pods.project / "x").exists()

    # A write that cannot finish leaves the file as it was, and nothing beside it.
    with monkeypatch.context() as patch:
        patch.setattr(executor_server.os, "replace", lambda *_: (_ for _ in ()).throw(OSError(28, "No space left")))
        answer = await call(pods, pod, "_file", action="write", path="notes.md", content_b64=x)
    assert "No space left" in answer["error"]
    assert (copy / "notes.md").read_text() == "notes v1"
    assert not list(copy.glob("*.file~"))

    # Over the cap, a write is refused before it is decoded.
    monkeypatch.setattr(executor_server, "MAX_FILE_BYTES", 4)
    monkeypatch.setattr(executor_server.base64, "b64decode", lambda *_: (_ for _ in ()).throw(ValueError("decoded first")))
    big = await call(pods, pod, "_file", action="write", path="big.bin", content_b64=base64.b64encode(b"1234567").decode())
    assert "50 MiB" in big["error"] and not (copy / "big.bin").exists()


async def test_a_pod_writes_a_file_whose_name_is_near_the_limit(pods):
    pod = await a_pod(pods)
    name = "Contrat " + "é" * 119 + ".docx"
    written = await call(pods, pod, "_file", action="write", path=name, content_b64=base64.b64encode(b"contrat").decode())
    assert written == {"ok": True, "bytes": 7}
    assert (pods.copies["t1"] / name).read_bytes() == b"contrat"


async def test_a_pod_whose_real_files_came_unmounted_lands_nothing(tmp_path):
    project, copy, mounts = tmp_path / "project", tmp_path / "workspace", tmp_path / "mounts"
    project.mkdir()
    copy.mkdir()
    (project / "Report.docx").write_bytes(b"report v1")
    history = History(
        repo=_shadow_repo_path(str(project), base=tmp_path / "home"), project=project, copy=copy, thread="t1", user="u1",
    )
    app = executor_server.create_app(token="t", workspace=str(copy), mounts_path=str(mounts), history=history)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pod") as client:
        mounts.write_text(f"geesefs {project} fuse.geesefs rw 0 0\n")
        assert (await client.get("/healthz")).status_code == 200
        step = {"action": "commit", "author": THREAD, "trailers": [["Surogate-Saga", "saga:1"], ["Surogate-Kind", "turn"]]}
        # Mounted, the real files answer a landing's step: the copy, on the pod's disk, is never a mount.
        (copy / "new.md").write_text("new")
        mounted = (await client.post("/execute", json={"name": "_history", "args": step}, headers=AUTH)).json()
        assert [c["path"] for c in mounted.get("changes", [])] == ["new.md"], mounted
        # The sidecar went and /project is an empty folder: nothing written there would reach the bucket.
        mounts.write_text("")
        (copy / "newer.md").write_text("newer")
        answer = (await client.post("/execute", json={"name": "_history", "args": step}, headers=AUTH)).json()
    assert list(answer) == ["error"] and "not mounted" in answer["error"]


async def test_a_pool_says_once_that_it_made_a_copy_and_never_after_the_turn(pods):
    pool = SandboxPool(pods)
    spec = SandboxSpec(env={"PROJECT_DIR": "/project", "HISTORY_THREAD": "t1", "USER_ID": "u1"})
    await pool.ensure("t1", spec)
    assert (pool.copy_fresh("t1"), pool.copy_fresh("t1")) == (True, False)
    await pods.destroy(next(iter(pods.pods)))  # its pod stops
    await pool.ensure("t1", spec)  # and its copy is made again
    await pool.release_for_session("t1")  # the turn ends untold
    # A later turn on this worker hears nothing of an earlier turn's copy.
    assert pool.copy_fresh("t1") is False


async def test_a_slow_put_back_lets_go_only_the_pod_it_waited_on(pods, monkeypatch):
    import asyncio

    from surogates.harness import landing

    monkeypatch.setattr(landing, "_PUT_BACK_BOUND", 0.05)
    pool = SandboxPool(pods)
    spec = SandboxSpec(env={"PROJECT_DIR": "/project", "HISTORY_THREAD": "t1", "USER_ID": "u1"})
    await pool.ensure("t1", spec)
    put_back = asyncio.get_running_loop().create_future()
    monkeypatch.setitem(landing._PUTTING_BACK, "t1", put_back)
    await landing._after_cancel(put_back, pool, "t1")  # not done in time: its pod waits for it
    # A later turn of the thread, here, has a pod of its own by the time the put-back ends.
    await pool.destroy_for_session("t1")
    await pool.ensure("t1", spec)
    later = set(pods.pods)
    put_back.set_result([])
    async with asyncio.timeout(5):
        while landing._TEARDOWNS or not put_back.done():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    assert set(pods.pods) == later and pool.holds_copy("t1")


async def test_a_slow_put_back_that_found_no_pod_lets_go_of_none(pods, monkeypatch):
    import asyncio

    from surogates.harness import landing

    monkeypatch.setattr(landing, "_PUT_BACK_BOUND", 0.05)
    pool = SandboxPool(pods)
    put_back = asyncio.get_running_loop().create_future()
    monkeypatch.setitem(landing._PUTTING_BACK, "t1", put_back)
    await landing._after_cancel(put_back, pool, "t1")  # deferred with no pod mapped
    await pool.ensure("t1", SandboxSpec(env={"PROJECT_DIR": "/project", "HISTORY_THREAD": "t1", "USER_ID": "u1"}))
    later = set(pods.pods)
    put_back.set_result([])
    async with asyncio.timeout(5):
        while landing._TEARDOWNS or not put_back.done():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
    # The pod mapped since is a later turn's: it stays.
    assert set(pods.pods) == later and pool.holds_copy("t1")


#: What a pod answers when it did not run a step to its end, and what no step answers.
NO_RESULT = {
    "its error": json.dumps({"error": "a.md changed after the landing wrote it"}),
    "cut off at the pod's own timeout": executor_server._timed_out_result(),
    "a daemon error": json.dumps({
        "exit_code": -1, "stdout": "", "stderr": "Executor daemon error (HTTP 500)", "truncated": False, "timed_out": False,
    }),
    "a child that died": json.dumps({"exit_code": 1, "output": "", "error": "Tool process died unexpectedly (exit code -9)"}),
    "no object": "null",
    "no JSON": "<html>502 Bad Gateway</html>",
}


class Answers:
    """A pool whose pod answers every call with *answer*."""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def execute(self, session_id, name, input, **_) -> str:
        return self.answer

    execute_released = execute


@pytest.mark.parametrize("answer", list(NO_RESULT))
async def test_a_put_back_the_pod_did_not_finish_is_never_read_as_done(answer):
    step = SagaStep(
        step_id="s", tool_name="history.apply", tool_call_id="",
        arguments={"path": "a.md", "before": None, "after": "b" * 40},
    )
    # Read as done, its landing's row would say the files are as they were.
    with pytest.raises(landing.LandingStepError):
        await compensate_history(step, Answers(NO_RESULT[answer]), "t1")


@pytest.mark.parametrize("answer", list(NO_RESULT))
async def test_a_step_the_pod_did_not_finish_is_never_read_as_its_result(answer):
    with pytest.raises(landing.LandingStepError):
        await landing._call(Answers(NO_RESULT[answer]), "t1", "apply", path="a.md", before=None, after="b" * 40)


async def test_a_steps_own_result_is_read_as_it_is():
    result = {"path": "a.md", "before": None, "after": "b" * 40, "made": []}
    assert await landing._call(Answers(json.dumps(result)), "t1", "apply", path="a.md", before=None, after="b" * 40) == result


@pytest.mark.parametrize("answer", list(NO_RESULT))
async def test_a_pruning_the_pod_did_not_finish_is_logged_and_the_landing_stands(answer, monkeypatch, caplog):
    async def kept(*_):
        return []

    @contextlib.asynccontextmanager
    async def the_lock(*_):
        async def held():
            return None

        yield held

    monkeypatch.setattr(landing, "kept_refs", kept)
    monkeypatch.setattr(landing, "running_landings", kept)  # none running: the pruning is asked for
    monkeypatch.setattr(landing, "project_lock", the_lock)
    with caplog.at_level(logging.WARNING, logger=landing.__name__):
        await landing.prune_after(
            session_factory=None, sandbox_pool=Answers(NO_RESULT[answer]), sandbox_id="pod-1", workstream="w1", packs=0,
            saga_settings=None,
        )
    assert "Could not prune the history of project w1" in caplog.text


async def test_a_pruning_asks_the_projects_lock_before_it_asks_the_pod(monkeypatch, caplog):
    asked = []

    async def none(*_):
        return []

    class Pod:
        async def execute_released(self, *args, **kwargs):
            asked.append(args)
            return json.dumps({"pruned": True})

    @contextlib.asynccontextmanager
    async def a_lock_lost_unseen(*_):
        async def held():
            raise ConnectionError("the lock's connection is gone")

        yield held

    monkeypatch.setattr(landing, "kept_refs", none)
    monkeypatch.setattr(landing, "running_landings", none)
    monkeypatch.setattr(landing, "project_lock", a_lock_lost_unseen)
    with caplog.at_level(logging.WARNING, logger=landing.__name__):
        await landing.prune_after(
            session_factory=None, sandbox_pool=Pod(), sandbox_id="pod-1", workstream="w1", packs=0, saga_settings=None,
        )
    # A pruning rewrites the history's refs and deletes its packs: never without the lock.
    assert asked == [] and "Could not prune the history of project w1" in caplog.text


async def test_a_landings_row_is_behind_by_the_larger_of_its_interval_and_twenty_times_a_write(monkeypatch):
    saves, marks = [], []

    async def a_slow_write(session_factory, row, saga, **values):
        await asyncio.sleep(0.05)
        saves.append(values)

    async def a_mark(session_factory, row):
        marks.append(row)

    monkeypatch.setattr(landing, "save_landing", a_slow_write)
    monkeypatch.setattr(landing, "touch_landing", a_mark)
    monkeypatch.setattr(landing, "_ROW_EVERY", 0.01)
    row = landing._Row(None, 1, None)
    await row.write()  # took a twentieth of a second: the steps are due again a second later, twenty times that
    await asyncio.sleep(0.2)  # long past the interval, well short of twenty writes
    await row.alive()
    assert (len(saves), len(marks)) == (1, 1)  # marked alive, its steps not written
    await asyncio.sleep(1.0)
    await row.alive()
    assert (len(saves), len(marks)) == (2, 1)


async def test_a_landings_row_whose_writes_are_quick_is_behind_by_its_interval(monkeypatch):
    saves, marks = [], []

    async def a_write(session_factory, row, saga, **values):
        saves.append(values)

    async def a_mark(session_factory, row):
        marks.append(row)

    monkeypatch.setattr(landing, "save_landing", a_write)
    monkeypatch.setattr(landing, "touch_landing", a_mark)
    monkeypatch.setattr(landing, "_ROW_EVERY", 0.3)
    row = landing._Row(None, 1, None)
    await row.write()
    await asyncio.sleep(0.05)  # long past twenty times a write that took no time
    await row.alive()
    assert (len(saves), len(marks)) == (1, 1)
    await asyncio.sleep(0.3)
    await row.alive()
    assert (len(saves), len(marks)) == (2, 1)


class PrunesNever:
    """A pool whose pod is asked nothing, and records how it is let go."""

    def __init__(self, *, goes: bool = True) -> None:
        self.goes, self.let_go = goes, []

    async def execute_released(self, *args, **kwargs) -> str:
        raise AssertionError("the pod is not asked to prune")

    async def destroy_released(self, sandbox_id, session_id, *, alone=False) -> None:
        self.let_go.append((sandbox_id, session_id, alone))
        if not self.goes:
            await asyncio.Event().wait()  # a delete the cluster never answers


@contextlib.asynccontextmanager
async def a_lock_never_free(*_):
    await asyncio.Event().wait()
    yield None


async def a_pruning(pool) -> asyncio.Future:
    landing.prune_later(
        session_factory=None, sandbox_pool=pool, sandbox_id="pod-1", session_id="t1", workstream="w1", packs=0,
        saga_settings=None,
    )
    [pruning] = landing._PRUNINGS
    return pruning


async def test_a_pruning_that_cannot_take_the_projects_lock_gives_the_day_up_and_lets_its_pod_go(monkeypatch, caplog):
    monkeypatch.setattr(landing, "project_lock", a_lock_never_free)
    monkeypatch.setattr(landing, "_PRUNE_PATIENCE", 0.2)
    pool = PrunesNever()
    with caplog.at_level(logging.WARNING, logger=landing.__name__):
        # Its pod waits with it, in no wake and no session: a wait for the lock has a bound.
        await asyncio.wait_for(await a_pruning(pool), 5)
    assert pool.let_go == [("pod-1", "t1", True)] and "Could not prune the history of project w1" in caplog.text
    assert not landing._PRUNINGS


@pytest.mark.parametrize("ended", ["by its bound", "by a cancel"])
async def test_a_pruning_whose_pod_never_goes_ends_all_the_same(monkeypatch, caplog, ended):
    monkeypatch.setattr(landing, "project_lock", a_lock_never_free)
    monkeypatch.setattr(landing, "_PRUNE_PATIENCE", 0.2 if ended == "by its bound" else 600)
    monkeypatch.setattr(landing, "_LET_GO_BOUND", 0.2)
    pool = PrunesNever(goes=False)
    pruning = await a_pruning(pool)
    if ended == "by a cancel":
        # As a loop that closes cancels it, once: nothing then waits on it without end.
        await asyncio.sleep(0.05)
        pruning.cancel()
    with caplog.at_level(logging.WARNING, logger=landing.__name__):
        done, _ = await asyncio.wait([pruning], timeout=5)
    assert done and pool.let_go == [("pod-1", "t1", True)] and "Could not let pod pod-1 go" in caplog.text
    assert pruning.cancelled() is (ended == "by a cancel")
