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
