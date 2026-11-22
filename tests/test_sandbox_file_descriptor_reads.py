"""What leaves a sandbox is the file the path check decided on.

The swap tests change the workspace at the moment between the check and the
open, by wrapping the check's own ``realpath``, so the order is forced, not
hoped for.  The others put the link or special file in place before the call.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import stat
import subprocess
import sys

import pytest

from surogates.coding_agents import pod_runner
from surogates.sandbox import executor_server
from surogates.sandbox.history import History

SECRET = b"outside the workspace"
# In range for a pid, and above any kernel's pid_max: no test here may name a real process.
NO_SUCH_PID = "2147483646"


@pytest.fixture(autouse=True)
def no_real_signal(monkeypatch):
    """No test in this file reaches a real process: what cancel would send is recorded."""
    sent = []

    def no_such_process(*_):
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(pod_runner.os, "killpg", lambda group, sig: sent.append((group, sig)))
    monkeypatch.setattr(pod_runner.os, "kill", no_such_process)
    monkeypatch.setattr(pod_runner.os, "getpgid", no_such_process)
    return sent


@pytest.fixture
def tree(tmp_path):
    workspace, elsewhere = tmp_path / "workspace", tmp_path / "elsewhere"
    (workspace / "docs").mkdir(parents=True)
    elsewhere.mkdir()
    (workspace / "docs" / "a.txt").write_bytes(b"inside")
    (elsewhere / "a.txt").write_bytes(SECRET)
    return workspace, elsewhere


def _after_check(monkeypatch, path, change):
    """Run *change* once, right after the check has resolved *path*."""
    real = os.path.realpath
    done = []

    def resolved(name, *args, **kwargs):
        answer = real(name, *args, **kwargs)
        if not done and str(name).endswith(path):
            done.append(True)
            change()
        return answer

    monkeypatch.setattr(executor_server.os.path, "realpath", resolved)
    return done


def _file(workspace, **args):
    return json.loads(executor_server._run_file(args, str(workspace)))


def test_a_file_swapped_for_a_link_after_the_check_is_not_read(tree, monkeypatch):
    workspace, elsewhere = tree
    target = workspace / "docs" / "a.txt"

    def swap():
        target.unlink()
        target.symlink_to(elsewhere / "a.txt")

    done = _after_check(monkeypatch, "docs/a.txt", swap)
    answer = _file(workspace, action="read", path="docs/a.txt")
    assert done
    assert "content_b64" not in answer
    assert "outside" in answer["error"]


def test_a_folder_swapped_for_a_link_after_the_check_is_not_read(tree, monkeypatch):
    workspace, elsewhere = tree

    def swap():
        (workspace / "docs").rename(workspace / "moved")
        (workspace / "docs").symlink_to(elsewhere)

    done = _after_check(monkeypatch, "docs/a.txt", swap)
    answer = _file(workspace, action="read", path="docs/a.txt")
    assert done
    assert "content_b64" not in answer
    assert "outside" in answer["error"]


def test_a_folder_swapped_for_a_link_after_the_check_is_not_written_through(tree, monkeypatch):
    workspace, elsewhere = tree

    def swap():
        (workspace / "docs").rename(workspace / "moved")
        (workspace / "docs").symlink_to(elsewhere)

    done = _after_check(monkeypatch, "docs/a.txt", swap)
    content = base64.b64encode(b"written").decode()
    answer = _file(workspace, action="write", path="docs/a.txt", content_b64=content)
    assert done
    assert "outside" in answer["error"]
    assert (elsewhere / "a.txt").read_bytes() == SECRET
    assert sorted(p.name for p in elsewhere.iterdir()) == ["a.txt"]


def test_a_link_inside_the_workspace_still_reads(tree):
    workspace, _ = tree
    (workspace / "link.txt").symlink_to(workspace / "docs" / "a.txt")
    answer = _file(workspace, action="read", path="link.txt")
    assert base64.b64decode(answer["content_b64"]) == b"inside"


def test_a_fifo_is_not_found_and_does_not_hang(tree):
    workspace, _ = tree
    os.mkfifo(workspace / "pipe")
    assert "not found" in _file(workspace, action="read", path="pipe")["error"]


def test_a_file_over_the_cap_is_refused(tree, monkeypatch):
    workspace, _ = tree
    monkeypatch.setattr(executor_server, "MAX_FILE_BYTES", 3)
    assert "50 MiB" in _file(workspace, action="read", path="docs/a.txt")["error"]


@pytest.mark.parametrize("names", [("run.log",), ("codex", "auth.json")])
def test_a_run_file_that_is_a_link_is_not_returned(tmp_path, names):
    base = tmp_path / "runs"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(SECRET)
    at = base.joinpath("r1", *names)
    at.parent.mkdir(parents=True)
    at.symlink_to(outside)
    (base / "r1" / "pid").write_text(NO_SUCH_PID)
    (base / "r1" / "exit_code").write_text("0")

    answer = pod_runner.poll({"run_id": "r1", "offset": 0}, base=str(base))

    assert answer["done"] is True
    assert SECRET.decode() not in answer["new_output"]
    assert "codex_auth_json" not in answer


def test_a_socket_is_not_found(tree):
    workspace, _ = tree
    listener = socket.socket(socket.AF_UNIX)
    cwd = os.getcwd()
    os.chdir(workspace)  # a socket's path has a short limit
    try:
        listener.bind("sock")
    finally:
        os.chdir(cwd)
    try:
        assert "not found" in _file(workspace, action="read", path="sock")["error"]
    finally:
        listener.close()


def test_a_write_past_the_path_limit_is_refused(tree):
    workspace, _ = tree
    deep = "/".join(["d" * 200] * 25) + "/x.bin"
    answer = _file(workspace, action="write", path=deep, content_b64=base64.b64encode(b"x").decode())
    assert "too long" in answer["error"]
    assert not (workspace / ("d" * 200)).exists()


def test_a_write_that_landed_is_reported_as_landed(tree, monkeypatch):
    workspace, _ = tree
    real_replace = os.replace

    def replaced(staged, name, **kwargs):
        real_replace(staged, name, **kwargs)
        os.mkdir(staged, dir_fd=kwargs["src_dir_fd"])  # something new at the staged name

    monkeypatch.setattr(executor_server.os, "replace", replaced)
    answer = _file(workspace, action="write", path="docs/a.txt", content_b64=base64.b64encode(b"new").decode())
    assert answer == {"ok": True, "bytes": 3}
    assert (workspace / "docs" / "a.txt").read_bytes() == b"new"


def _run(tmp_path, pid=NO_SUCH_PID):
    base = tmp_path / "runs"
    (base / "r1").mkdir(parents=True)
    (base / "r1" / "pid").write_text(pid)
    return base


@pytest.mark.parametrize("name", ["pid", "exit_code"])
def test_a_run_number_that_is_a_fifo_does_not_hang_a_poll(tmp_path, name):
    base = _run(tmp_path)
    (base / "r1" / name).unlink(missing_ok=True)
    os.mkfifo(base / "r1" / name)
    answer = pod_runner.poll({"run_id": "r1", "offset": 0}, base=str(base))
    assert answer["done"] is True and answer["exit_code"] is None


def test_a_cancel_with_a_fifo_for_a_pid_removes_the_run(tmp_path):
    base = _run(tmp_path)
    (base / "r1" / "pid").unlink()
    os.mkfifo(base / "r1" / "pid")
    assert pod_runner.cancel({"run_id": "r1"}, base=str(base))["ok"] is True
    assert not (base / "r1").exists()


def test_an_exit_code_that_is_a_link_is_not_read(tmp_path):
    base = _run(tmp_path)
    outside = tmp_path / "number"
    outside.write_text("424242")
    (base / "r1" / "exit_code").symlink_to(outside)
    answer = pod_runner.poll({"run_id": "r1", "offset": 0}, base=str(base))
    assert answer["done"] is True and answer["exit_code"] is None


def test_an_unreadable_auth_file_does_not_keep_the_credential_folder(tmp_path):
    base = _run(tmp_path)
    (base / "r1" / "exit_code").write_text("0")
    (base / "r1" / "codex").mkdir()
    (base / "r1" / "codex" / "auth.json").write_bytes(b"\xff\xfe")
    answer = pod_runner.poll({"run_id": "r1", "offset": 0}, base=str(base))
    assert "codex_auth_json" not in answer
    assert not (base / "r1").exists()


def test_a_run_file_with_a_second_name_is_not_returned(tmp_path):
    base = _run(tmp_path)
    (base / "r1" / "exit_code").write_text("0")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(SECRET)
    os.link(outside, base / "r1" / "run.log")
    answer = pod_runner.poll({"run_id": "r1", "offset": 0}, base=str(base))
    assert SECRET.decode() not in answer["new_output"]


def test_a_run_folder_that_is_a_link_is_not_read(tmp_path):
    elsewhere = _run(tmp_path)
    (elsewhere / "r1" / "exit_code").write_text("0")
    (elsewhere / "r1" / "run.log").write_bytes(SECRET)
    linked = tmp_path / "linked"
    linked.symlink_to(elsewhere)
    answer = pod_runner.poll({"run_id": "r1", "offset": 0}, base=str(linked))
    assert SECRET.decode() not in answer["new_output"]


def _alive(monkeypatch, pid=4242, started=5, group=None):
    """A process *pid*, started at *started*, in *group*: all of it mocked."""
    monkeypatch.setattr(pod_runner, "start_time", lambda asked: started if asked == pid else None, raising=False)
    monkeypatch.setattr(pod_runner.os, "getpgid", lambda _asked: pid if group is None else group)


@pytest.mark.parametrize("pid", [1, 0, -1, True, "4242", 2**40, None])
def test_a_cancel_never_signals_a_pid_no_run_can_have(tmp_path, monkeypatch, no_real_signal, pid):
    base = _run(tmp_path)
    monkeypatch.setattr(pod_runner, "start_time", lambda _asked: 5, raising=False)
    monkeypatch.setattr(pod_runner.os, "getpgid", lambda asked: asked)
    assert pod_runner.cancel({"run_id": "r1", "pid": pid, "start": 5}, base=str(base))["ok"] is True
    assert no_real_signal == []
    assert not (base / "r1").exists()


def test_a_cancel_takes_no_pid_from_the_run_file(tmp_path, monkeypatch, no_real_signal):
    base = _run(tmp_path, pid="4242")
    _alive(monkeypatch)
    assert pod_runner.cancel({"run_id": "r1"}, base=str(base))["ok"] is True
    assert no_real_signal == []


@pytest.mark.parametrize("group", [0, 1, 7, os.getpgrp()])
def test_a_cancel_never_signals_a_group_the_run_does_not_lead(tmp_path, monkeypatch, no_real_signal, group):
    base = _run(tmp_path)
    _alive(monkeypatch, group=group)
    pod_runner.cancel({"run_id": "r1", "pid": 4242, "start": 5}, base=str(base))
    assert no_real_signal == []


def test_a_cancel_never_signals_its_own_group(tmp_path, monkeypatch, no_real_signal):
    base = _run(tmp_path)
    own = os.getpgrp()
    _alive(monkeypatch, pid=own, group=own)
    pod_runner.cancel({"run_id": "r1", "pid": own, "start": 5}, base=str(base))
    assert no_real_signal == []


def test_a_cancel_leaves_a_process_that_took_the_runs_pid(tmp_path, monkeypatch, no_real_signal):
    base = _run(tmp_path)
    _alive(monkeypatch, started=6)
    pod_runner.cancel({"run_id": "r1", "pid": 4242, "start": 5}, base=str(base))
    assert no_real_signal == []


def test_a_cancel_stops_the_group_of_the_run_it_launched(tmp_path, monkeypatch, no_real_signal):
    base = _run(tmp_path)
    _alive(monkeypatch)
    asked = []
    real_getpgid = pod_runner.os.getpgid
    monkeypatch.setattr(pod_runner.os, "getpgid", lambda pid: (asked.append(pid), real_getpgid(pid))[1])
    pod_runner.cancel({"run_id": "r1", "pid": 4242, "start": 5}, base=str(base))
    assert no_real_signal == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    # Settled once: a leader gone after the first signal does not spare the rest of its group.
    assert asked == [4242]


def _credential(base):
    (base / "r1" / "codex").mkdir()
    (base / "r1" / "codex" / "auth.json").write_text('{"synthetic": true}')


@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
def test_a_run_file_that_cannot_be_opened_does_not_keep_the_credential(tmp_path, monkeypatch, call):
    from surogates.tools.workspace_io import local

    base = _run(tmp_path)
    _credential(base)

    def refused(*_):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(local, "_open_to_read", refused)
    assert call({"run_id": "r1", "offset": 0}, base=str(base))["ok"] is True
    assert not (base / "r1").exists()


@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
def test_a_pid_no_process_can_have_does_not_keep_the_credential(tmp_path, call):
    base = _run(tmp_path, pid="9" * 30)
    _credential(base)
    assert call({"run_id": "r1", "offset": 0}, base=str(base))["ok"] is True
    assert not (base / "r1").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file of any mode")
@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
@pytest.mark.parametrize("name", ["pid", "exit_code"])
def test_a_run_number_made_unreadable_does_not_keep_the_credential(tmp_path, call, name):
    base = _run(tmp_path)
    (base / "r1" / "exit_code").write_text("0")
    _credential(base)
    (base / "r1" / name).chmod(0)
    assert call({"run_id": "r1", "offset": 0}, base=str(base))["ok"] is True
    assert not (base / "r1").exists()


def test_a_poll_of_a_run_with_no_pid_removes_what_is_left(tmp_path):
    base = _run(tmp_path)
    _credential(base)
    (base / "r1" / "pid").unlink()
    assert pod_runner.poll({"run_id": "r1", "offset": 0}, base=str(base))["done"] is True
    assert not (base / "r1").exists()


def test_a_credential_folder_made_unreadable_is_still_removed(tmp_path):
    base = _run(tmp_path)
    _credential(base)
    (base / "r1" / "codex").chmod(0)
    assert pod_runner.cancel({"run_id": "r1"}, base=str(base))["ok"] is True
    assert not (base / "r1").exists()


@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
def test_a_link_at_the_runs_folder_is_refused_and_what_it_leads_to_is_left_alone(tmp_path, call):
    victim = _run(tmp_path)
    (victim / "r1" / "kept").write_text("x")
    victim.chmod(0o500)
    linked = tmp_path / "linked"
    linked.symlink_to(victim)
    try:
        answer = call({"run_id": "r1", "offset": 0}, base=str(linked))
        assert answer["cleaned"] is False
        assert stat.S_IMODE(victim.stat().st_mode) == 0o500
        assert (victim / "r1" / "kept").read_text() == "x"
    finally:
        victim.chmod(0o755)


def test_a_cleanup_goes_on_after_its_modes_are_taken_away_again(tmp_path, monkeypatch):
    base = _run(tmp_path)
    _credential(base)
    real, refused = pod_runner._scan, []

    def scan(folder):
        if len(refused) < 3:
            refused.append(True)
            raise PermissionError(13, "Permission denied")
        return real(folder)

    monkeypatch.setattr(pod_runner, "_scan", scan)
    assert pod_runner.cancel({"run_id": "r1"}, base=str(base))["cleaned"] is True
    assert len(refused) == 3 and not (base / "r1").exists()


def test_a_cleanup_that_runs_out_of_budget_says_so(tmp_path, monkeypatch):
    base = _run(tmp_path)
    _credential(base)
    monkeypatch.setattr(pod_runner, "_CLEANUP_S", 0)
    answer = pod_runner.cancel({"run_id": "r1"}, base=str(base))
    assert answer["ok"] is True and answer["cleaned"] is False
    assert (base / "r1").exists()


def test_a_cleanup_gives_up_on_a_tree_it_cannot_shrink_and_says_so(tmp_path, monkeypatch):
    base = _run(tmp_path)
    monkeypatch.setattr(pod_runner, "_CLEANUP_FAILURES", 5)

    def refused(_folder):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(pod_runner, "_scan", refused)
    assert pod_runner.cancel({"run_id": "r1"}, base=str(base))["cleaned"] is False


@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
def test_a_run_moved_away_counts_as_not_removed(tmp_path, call):
    base = _run(tmp_path)
    _credential(base)
    (base / "r1").rename(base / "elsewhere")
    answer = call({"run_id": "r1", "offset": 0, "launched": True}, base=str(base))
    assert answer["cleaned"] is False


@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
def test_an_ordinary_run_is_reported_cleaned(tmp_path, call):
    base = _run(tmp_path)
    (base / "r1" / "exit_code").write_text("0")
    _credential(base)
    assert call({"run_id": "r1", "offset": 0, "launched": True}, base=str(base))["cleaned"] is True
    assert not (base / "r1").exists()


def _identity(path):
    held = os.stat(path, follow_symlinks=False)
    return [held.st_dev, held.st_ino]


@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
def test_a_decoy_at_the_runs_name_is_not_taken_for_the_run(tmp_path, call):
    base = _run(tmp_path)
    _credential(base)
    (base / "r1" / "exit_code").write_text("0")
    made = _identity(base / "r1")
    (base / "r1").rename(tmp_path / "moved")
    (base / "r1").mkdir()
    (base / "r1" / "pid").write_text(NO_SUCH_PID)
    (base / "r1" / "exit_code").write_text("0")
    answer = call({"run_id": "r1", "offset": 0, "launched": True, "run_dir": made}, base=str(base))
    assert answer["cleaned"] is False
    assert (tmp_path / "moved" / "codex" / "auth.json").exists()
    assert (base / "r1").is_dir()


@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
def test_a_decoy_runs_folder_is_not_taken_for_the_runs_folder(tmp_path, call):
    base = _run(tmp_path)
    _credential(base)
    (base / "r1" / "exit_code").write_text("0")
    made = _identity(base / "r1")
    base.rename(tmp_path / "moved")
    (base / "r1").mkdir(parents=True)
    (base / "r1" / "pid").write_text(NO_SUCH_PID)
    (base / "r1" / "exit_code").write_text("0")
    answer = call({"run_id": "r1", "offset": 0, "launched": True, "run_dir": made}, base=str(base))
    assert answer["cleaned"] is False
    assert (tmp_path / "moved" / "r1" / "codex" / "auth.json").exists()


def test_a_link_at_the_runs_name_is_left_and_not_taken_for_the_run(tmp_path):
    base = _run(tmp_path)
    made = _identity(base / "r1")
    (base / "r1").rename(tmp_path / "moved")
    (base / "r1").symlink_to(tmp_path / "moved")
    answer = pod_runner.cancel({"run_id": "r1", "launched": True, "run_dir": made}, base=str(base))
    assert answer["cleaned"] is False
    assert (tmp_path / "moved" / "pid").exists()


def test_a_folder_swapped_in_just_before_the_removal_is_not_taken_for_the_run(tmp_path, monkeypatch):
    base = _run(tmp_path)
    made = _identity(base / "r1")
    real, swapped = os.rmdir, []

    def rmdir(name, *, dir_fd=None):
        if name == "r1" and not swapped:
            swapped.append(True)
            os.rename("r1", str(tmp_path / "moved"), src_dir_fd=dir_fd)
            os.mkdir("r1", dir_fd=dir_fd)
        return real(name, dir_fd=dir_fd)

    monkeypatch.setattr(pod_runner.os, "rmdir", rmdir)
    answer = pod_runner.cancel({"run_id": "r1", "launched": True, "run_dir": made}, base=str(base))
    assert swapped and answer["cleaned"] is False


def test_a_credential_with_a_second_name_is_reported(tmp_path):
    base = _run(tmp_path)
    _credential(base)
    made = _identity(base / "r1")
    os.link(base / "r1" / "codex" / "auth.json", tmp_path / "kept.json")
    answer = pod_runner.cancel({"run_id": "r1", "launched": True, "run_dir": made}, base=str(base))
    assert answer["cleaned"] is False
    assert not (base / "r1").exists()


def test_the_run_launch_made_is_reported_cleaned(tmp_path):
    base = _run(tmp_path)
    _credential(base)
    made = _identity(base / "r1")
    answer = pod_runner.cancel({"run_id": "r1", "launched": True, "run_dir": made}, base=str(base))
    assert answer["cleaned"] is True and not (base / "r1").exists()


def test_a_cleanup_stops_at_its_budget_inside_a_large_folder(tmp_path, monkeypatch):
    base = _run(tmp_path)
    (base / "r1" / "pid").unlink()
    for at in range(20):
        (base / "r1" / f"f{at}").write_text("x")
    monkeypatch.setattr(pod_runner, "_CLEANUP_STEPS", 3)
    assert pod_runner.cancel({"run_id": "r1"}, base=str(base))["cleaned"] is False
    assert len(list((base / "r1").iterdir())) == 17


def test_a_cleanup_stops_at_its_budget_while_moving_a_folders_contents_up(tmp_path, monkeypatch):
    base = _run(tmp_path)
    (base / "r1" / "pid").unlink()
    (base / "r1" / "wide").mkdir()
    for at in range(20):
        (base / "r1" / "wide" / f"f{at}").write_text("x")
    real_left, asked = pod_runner._CleanupWork.left, []

    def left(self):
        asked.append(True)
        return len(asked) <= 8 and real_left(self)

    monkeypatch.setattr(pod_runner._CleanupWork, "left", left)
    assert pod_runner.cancel({"run_id": "r1"}, base=str(base))["cleaned"] is False
    assert len(list((base / "r1" / "wide").iterdir())) > 10


def _refused_launch(tmp_path, **extra):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return {
        "run_id": "r1", "argv": ["true"], "stdin": "prompt", "codex_auth_json": '{"synthetic": true}',
        "env": {"WORKSPACE_DIR": str(workspace)}, **extra,
    }


@pytest.mark.parametrize("workdir", ["/", "missing"])
def test_a_refused_launch_leaves_no_credential(tmp_path, workdir):
    payload = _refused_launch(tmp_path)
    payload["workdir"] = workdir if workdir == "/" else str(tmp_path / "workspace" / workdir)
    base = tmp_path / "runs"
    answer = pod_runner.launch(payload, base=str(base))
    assert answer["ok"] is False and answer["cleaned"] is True and answer["reports_cleaned"] is True
    assert not (base / "r1").exists()


def test_a_launch_that_cannot_start_leaves_no_credential(tmp_path, monkeypatch):
    def cannot(*_args, **_kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(pod_runner.subprocess, "Popen", cannot)
    base = tmp_path / "runs"
    answer = pod_runner.launch(_refused_launch(tmp_path), base=str(base))
    assert answer["ok"] is False and answer["cleaned"] is True
    assert not (base / "r1").exists()


def test_a_launch_that_raises_leaves_no_credential_and_does_not_quote_the_error(tmp_path, monkeypatch):
    def cannot(*_args, **_kwargs):
        raise RuntimeError("synthetic-credential-value")

    monkeypatch.setattr(pod_runner.subprocess, "Popen", cannot)
    base = tmp_path / "runs"
    answer = pod_runner.launch(_refused_launch(tmp_path), base=str(base))
    assert answer["ok"] is False and answer["cleaned"] is True and answer["reports_cleaned"] is True
    assert "synthetic-credential-value" not in json.dumps(answer)
    assert "RuntimeError" in answer["error"]
    assert not (base / "r1").exists()


def test_a_launch_that_raises_and_cannot_remove_its_directory_says_so(tmp_path, monkeypatch):
    def cannot(*_args, **_kwargs):
        raise RuntimeError("cut short")

    monkeypatch.setattr(pod_runner.subprocess, "Popen", cannot)
    monkeypatch.setattr(pod_runner, "_remove_tree", lambda *_args: False)
    answer = pod_runner.launch(_refused_launch(tmp_path), base=str(tmp_path / "runs"))
    assert answer["ok"] is False and answer["cleaned"] is False


@pytest.mark.parametrize("value", ["synthetic\ud800value", "synthetic\0value"])
def test_a_launch_refused_for_its_environment_quotes_nothing_of_it(tmp_path, value):
    payload = _refused_launch(tmp_path)
    payload["env"]["SYNTHETIC_CREDENTIAL"] = value
    base = tmp_path / "runs"
    answer = pod_runner.launch(payload, base=str(base))
    assert answer["ok"] is False and answer["cleaned"] is True
    assert "Error" in answer["error"]
    shown = json.dumps(answer)
    assert "ud800" not in shown and "position" not in shown and "null byte" not in shown
    assert not (base / "r1").exists()


def test_a_launch_that_fails_before_making_its_directory_is_no_false_alarm(tmp_path):
    payload = _refused_launch(tmp_path)
    del payload["argv"]  # read before the directory is made
    base = tmp_path / "runs"
    answer = pod_runner.launch(payload, base=str(base))
    assert answer["ok"] is False and answer["cleaned"] is True
    assert not base.exists()


def test_a_launch_whose_directory_cannot_be_made_is_no_false_alarm(tmp_path):
    (tmp_path / "runs").write_text("a file where the runs folder goes")
    answer = pod_runner.launch(_refused_launch(tmp_path), base=str(tmp_path / "runs"))
    assert answer["ok"] is False and answer["cleaned"] is True


def test_a_launch_names_the_directory_it_made(tmp_path, monkeypatch):
    monkeypatch.setattr(pod_runner.subprocess, "Popen", lambda *_a, **_k: type("P", (), {"pid": int(NO_SUCH_PID)})())
    base = tmp_path / "runs"
    answer = pod_runner.launch(_refused_launch(tmp_path), base=str(base))
    assert answer["ok"] is True and answer["reports_cleaned"] is True
    assert answer["run_dir"] == _identity(base / "r1")


def test_the_module_imports_where_the_system_has_no_o_path():
    code = "import os; del os.O_PATH; import surogates.coding_agents.pod_runner"
    ran = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr


@pytest.mark.parametrize("mode", [0o311, 0o000])
@pytest.mark.parametrize("call", [pod_runner.poll, pod_runner.cancel])
def test_a_runs_folder_with_its_modes_taken_away_is_still_cleaned(tmp_path, call, mode):
    base = _run(tmp_path)
    _credential(base)
    (base / "r1" / "exit_code").write_text("0")
    base.chmod(mode)
    try:
        call({"run_id": "r1", "offset": 0}, base=str(base))
    finally:
        base.chmod(0o755)
    assert not (base / "r1").exists()


def test_a_run_folder_with_its_mode_taken_away_is_still_cleaned(tmp_path):
    base = _run(tmp_path)
    _credential(base)
    (base / "r1").chmod(0)
    assert pod_runner.poll({"run_id": "r1", "offset": 0}, base=str(base))["done"] is True
    assert not (base / "r1").exists()


def test_a_credential_moved_under_a_folder_with_no_mode_is_still_removed(tmp_path):
    base = _run(tmp_path)
    _credential(base)
    sub = base / "r1" / "codex" / "sub"
    sub.mkdir()
    (base / "r1" / "codex" / "auth.json").rename(sub / "auth.json")
    sub.chmod(0)
    pod_runner.cancel({"run_id": "r1"}, base=str(base))
    assert not (base / "r1").exists()


def test_a_credential_deeper_than_the_descriptor_limit_is_still_removed(tmp_path):
    base = _run(tmp_path)
    held = os.open(base / "r1", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(1200):
            os.mkdir("d", dir_fd=held)
            deeper = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=held)
            os.close(held)
            held = deeper
        os.close(os.open("auth.json", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=held))
    finally:
        os.close(held)
    pod_runner.cancel({"run_id": "r1"}, base=str(base))
    assert not (base / "r1").exists()


def test_a_cleanup_stays_in_the_run_when_its_folder_is_swapped_midway(tmp_path, monkeypatch):
    base = _run(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "kept").write_text("x")
    real_own, swapped = pod_runner._own, []

    def own(folder):
        real_own(folder)
        if os.fstat(folder).st_ino == held and not swapped:
            swapped.append(True)
            (base / "r1").rename(base / "moved")
            (base / "r1").symlink_to(other)

    held = os.stat(base / "r1").st_ino
    monkeypatch.setattr(pod_runner, "_own", own)
    pod_runner.cancel({"run_id": "r1"}, base=str(base))
    assert swapped
    assert (other / "kept").read_text() == "x"


def test_a_cleanup_removes_a_link_at_the_run_and_not_what_it_names(tmp_path):
    base = tmp_path / "runs"
    base.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "kept").write_text("x")
    (base / "r1").symlink_to(other)
    pod_runner.cancel({"run_id": "r1"}, base=str(base))
    assert not os.path.lexists(base / "r1")
    assert (other / "kept").exists()


def _history(project):
    history = History.__new__(History)
    object.__setattr__(history, "project", project)
    return history


def test_a_real_file_that_is_a_link_is_not_hashed_through(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(SECRET)
    (project / "plain.txt").write_bytes(SECRET)
    (project / "link.txt").symlink_to(outside)
    history = _history(project)
    assert history._real("link.txt") != history._real("plain.txt")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a folder of any mode")
def test_a_real_file_is_read_through_a_folder_that_can_only_be_searched(tmp_path):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "a.txt").write_bytes(b"inside")
    (project / "plain.txt").write_bytes(b"inside")
    (project / "docs").chmod(0o311)
    try:
        history = _history(project)
        assert history._real("docs/a.txt") == history._real("plain.txt")
    finally:
        (project / "docs").chmod(0o755)


def test_a_real_file_is_not_read_through_a_folder_link_inside_the_project(tmp_path):
    project = tmp_path / "project"
    (project / "c").mkdir(parents=True)
    (project / "c" / "b.txt").write_bytes(b"same")
    (project / "plain.txt").write_bytes(b"same")
    (project / "a").symlink_to(project / "c")
    history = _history(project)
    assert history._real("a/b.txt") != history._real("plain.txt")


def test_a_real_folder_swapped_after_the_check_is_not_read(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.txt").write_bytes(SECRET)
    (project / "docs" / "a.txt").write_bytes(SECRET)
    history = _history(project)
    plain = history._real("docs/a.txt")
    checked = History._inside

    def swapped(self, path):
        target = checked(self, path)
        (project / "docs").rename(project / "moved")
        (project / "docs").symlink_to(elsewhere)
        return target

    monkeypatch.setattr(History, "_inside", swapped)
    real = os.path.realpath

    def at_the_check(name, *args, **kwargs):
        # The check's answer, from before the swap.
        return str(project / "docs") if str(name) == str(project / "docs") else real(name, *args, **kwargs)

    monkeypatch.setattr("surogates.sandbox.history.os.path.realpath", at_the_check)
    assert history._real("docs/a.txt") != plain
