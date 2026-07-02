"""Unit tests for the pod-side /code launcher (real subprocesses, tmp dirs)."""

from __future__ import annotations

import time

import pytest

from surogates.coding_agents import pod_runner


def _wait_done(run_id, base, timeout=10.0):
    offset = 0
    out = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = pod_runner.poll({"run_id": run_id, "offset": offset}, base=base)
        out += res["new_output"]
        offset = res["offset"]
        if res["done"]:
            return res, out
        time.sleep(0.05)
    raise AssertionError("run did not finish in time")


def test_launch_poll_captures_output_and_exit(tmp_path):
    base = str(tmp_path)
    launched = pod_runner.launch(
        {
            "run_id": "r1",
            "argv": ["python3", "-c", "import sys; print(sys.stdin.read().strip())"],
            "stdin": "hello-from-stdin",
            "env": {},
        },
        base=base,
    )
    assert launched["ok"] is True
    assert launched["pid"] > 0

    res, out = _wait_done("r1", base)
    assert res["exit_code"] == 0
    assert "hello-from-stdin" in out


def test_env_is_applied_and_conflicts_scrubbed(tmp_path, monkeypatch):
    base = str(tmp_path)
    # A stray provider var in the pod env must be scrubbed so it can't
    # override the user's injected credential.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stray-pod-key")
    pod_runner.launch(
        {
            "run_id": "r2",
            "argv": [
                "python3", "-c",
                "import os; print('TOK=' + os.environ.get('CLAUDE_CODE_OAUTH_TOKEN','')); "
                "print('STRAY=' + os.environ.get('ANTHROPIC_API_KEY','none'))",
            ],
            "stdin": None,
            "env": {"CLAUDE_CODE_OAUTH_TOKEN": "user-oauth-token"},
            "scrub": ["ANTHROPIC_API_KEY"],
        },
        base=base,
    )
    _res, out = _wait_done("r2", base)
    assert "TOK=user-oauth-token" in out
    assert "STRAY=none" in out


def test_codex_auth_json_written_and_home_exported(tmp_path):
    base = str(tmp_path)
    pod_runner.launch(
        {
            "run_id": "r3",
            "argv": [
                "python3", "-c",
                "import os,pathlib; h=os.environ['CODEX_HOME']; "
                "print('AUTH=' + pathlib.Path(h, 'auth.json').read_text())",
            ],
            "stdin": None,
            "env": {},
            "codex_auth_json": '{"tokens":{"access_token":"tok"}}',
        },
        base=base,
    )
    _res, out = _wait_done("r3", base)
    assert '"access_token":"tok"' in out or '"access_token": "tok"' in out


def test_cancel_kills_running_process(tmp_path):
    base = str(tmp_path)
    pod_runner.launch(
        {"run_id": "r4", "argv": ["sleep", "30"], "stdin": None, "env": {}},
        base=base,
    )
    # Confirm it is running.
    res = pod_runner.poll({"run_id": "r4", "offset": 0}, base=base)
    assert res["done"] is False

    cancelled = pod_runner.cancel({"run_id": "r4"}, base=base)
    assert cancelled["ok"] is True

    # After cancel the process is gone.
    time.sleep(0.2)
    res2 = pod_runner.poll({"run_id": "r4", "offset": res["offset"]}, base=base)
    assert res2["done"] is True


def test_codex_auth_read_back_on_completion(tmp_path):
    base = str(tmp_path)
    # A codex run whose CLI rewrites auth.json with a refreshed token.
    pod_runner.launch(
        {
            "run_id": "rb1",
            "argv": [
                "python3", "-c",
                "import os,pathlib; p=pathlib.Path(os.environ['CODEX_HOME'],'auth.json'); "
                "p.write_text('{\"tokens\":{\"access_token\":\"refreshed\"}}')",
            ],
            "stdin": None,
            "env": {},
            "codex_auth_json": '{"tokens":{"access_token":"original"}}',
        },
        base=base,
    )
    res, _out = _wait_done("rb1", base)
    assert res["done"] is True
    assert "refreshed" in res.get("codex_auth_json", "")


def test_poll_unknown_run_is_done_with_error(tmp_path):
    res = pod_runner.poll({"run_id": "missing", "offset": 0}, base=str(tmp_path))
    assert res["done"] is True
    assert res.get("error")


def test_dispatch_routes_actions(tmp_path):
    base = str(tmp_path)
    out = pod_runner.dispatch(
        {"action": "launch", "run_id": "r5", "argv": ["true"], "stdin": None, "env": {}},
        base=base,
    )
    assert out["ok"] is True
    # Unknown action is a clean error, not a crash.
    err = pod_runner.dispatch({"action": "frobnicate"}, base=base)
    assert err["ok"] is False


def test_launch_uses_payload_workdir(tmp_path):
    base = str(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    pod_runner.launch(
        {
            "run_id": "wd1",
            "argv": ["python3", "-c", "import os; print('CWD=' + os.getcwd())"],
            "stdin": None,
            "env": {"WORKSPACE_DIR": str(tmp_path)},
            "workdir": str(repo),
        },
        base=base,
    )
    _res, out = _wait_done("wd1", base)
    assert f"CWD={repo.resolve()}" in out


def test_launch_rejects_workdir_outside_workspace(tmp_path):
    base = str(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = pod_runner.launch(
        {
            "run_id": "wd2",
            "argv": ["python3", "-c", "print('should not run')"],
            "stdin": None,
            "env": {"WORKSPACE_DIR": str(ws)},
            "workdir": str(tmp_path),
        },
        base=base,
    )
    assert result["ok"] is False
    assert "workdir must be inside WORKSPACE_DIR" in result["error"]


def test_checkout_runs_command_in_workspace(tmp_path):
    result = pod_runner.checkout(
        {
            "run_id": "co1",
            "command": "pwd && echo cloned",
            "env": {"WORKSPACE_DIR": str(tmp_path)},
        },
    )
    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "cloned" in result["output"]
    # Ran in the workspace root.
    assert str(tmp_path.resolve()) in result["output"]


def test_checkout_redacts_secret_in_output(tmp_path):
    token = "github_pat_11ABCDE0secretmiddlepart9876"
    result = pod_runner.checkout(
        {
            "run_id": "co2",
            "command": f"echo leaked {token}",
            "env": {"WORKSPACE_DIR": str(tmp_path)},
        },
    )
    assert result["ok"] is True
    # A token that leaks to stdout must be masked before it is returned.
    assert token not in result["output"]


def test_checkout_reports_failure_with_error(tmp_path):
    result = pod_runner.checkout(
        {
            "run_id": "co3",
            "command": "echo boom 1>&2; exit 3",
            "env": {"WORKSPACE_DIR": str(tmp_path)},
        },
    )
    assert result["ok"] is False
    assert result["exit_code"] == 3
    assert "boom" in result["error"]


def test_checkout_requires_a_command(tmp_path):
    result = pod_runner.checkout(
        {"run_id": "co4", "env": {"WORKSPACE_DIR": str(tmp_path)}},
    )
    assert result["ok"] is False
    assert "command is required" in result["error"]


def test_checkout_missing_workspace_is_error(tmp_path):
    result = pod_runner.checkout(
        {
            "run_id": "co5",
            "command": "echo hi",
            "env": {"WORKSPACE_DIR": str(tmp_path / "does-not-exist")},
        },
    )
    assert result["ok"] is False
    assert "workspace missing" in result["error"]


def test_dispatch_routes_checkout(tmp_path):
    out = pod_runner.dispatch(
        {
            "action": "checkout",
            "run_id": "co6",
            "command": "echo ok",
            "env": {"WORKSPACE_DIR": str(tmp_path)},
        },
    )
    assert out["ok"] is True
    assert "ok" in out["output"]
