"""Coding-agent execution workflows through a simulated sandbox."""

from __future__ import annotations

import json

import pytest

from surogates.coding_agents.agents import CodeInvocation
from surogates.coding_agents.runner import RunnerConfig, run_code_agent

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeSandbox:
    """Simulates the pod ``_code`` command: scripted poll responses."""

    def __init__(self, polls, *, launch_ok=True):
        self.polls = list(polls)
        self.launch_ok = launch_ok
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, name, input_json):
        payload = json.loads(input_json)
        action = payload["action"]
        self.calls.append((action, payload))
        if action == "launch":
            if not self.launch_ok:
                return json.dumps({"ok": False, "error": "spawn failed"})
            return json.dumps({"ok": True, "run_id": payload["run_id"], "pid": 99, "start": 1234})
        if action == "poll":
            return json.dumps(self.polls.pop(0))
        if action == "cancel":
            return json.dumps({"ok": True})
        return json.dumps({"ok": False, "error": "unknown"})


def _inv():
    return CodeInvocation(argv=["claude", "-p"], stdin="do the thing")


async def _noop_sleep(_seconds):
    return None


async def test_happy_path_streams_and_parses_result():
    polls = [
        {
            "ok": True, "done": False, "exit_code": None, "offset": 40,
            "new_output": json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "working"}]},
            }) + "\n",
        },
        {
            "ok": True, "done": True, "exit_code": 0, "offset": 120,
            "new_output": json.dumps({
                "type": "result", "result": "All done.",
                "usage": {"input_tokens": 50, "output_tokens": 9},
            }) + "\n",
        },
    ]
    sandbox = _FakeSandbox(polls)
    progress: list[str] = []

    result = await run_code_agent(
        run_id="run-1",
        agent="claude",
        invocation=_inv(),
        env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"},
        codex_auth_json=None,
        execute=sandbox.execute,
        emit_progress=lambda chunk: progress.append(chunk) or _noop_sleep(0),
        should_cancel=lambda: False,
        sleep=_noop_sleep,
        config=RunnerConfig(poll_interval=0.0),
    )

    assert result.final_message == "All done."
    assert result.input_tokens == 50
    assert result.output_tokens == 9
    assert result.error is None
    # The launch never put the credential into argv.
    launch_payload = sandbox.calls[0][1]
    assert launch_payload["action"] == "launch"
    assert launch_payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert "working" in "".join(progress)


async def test_launch_failure_returns_error():
    sandbox = _FakeSandbox([], launch_ok=False)
    result = await run_code_agent(
        run_id="run-2",
        agent="claude",
        invocation=_inv(),
        env={},
        codex_auth_json=None,
        execute=sandbox.execute,
        emit_progress=lambda chunk: _noop_sleep(0),
        should_cancel=lambda: False,
        sleep=_noop_sleep,
        config=RunnerConfig(poll_interval=0.0),
    )
    assert result.error is not None
    assert "spawn failed" in result.error
    # No poll calls after a failed launch.
    assert all(c[0] != "poll" for c in sandbox.calls)


async def test_interrupt_cancels_run():
    _assistant = json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "thinking"}]},
    }) + "\n"
    polls = [
        {"ok": True, "done": False, "exit_code": None, "offset": 5, "new_output": _assistant},
        {"ok": True, "done": False, "exit_code": None, "offset": 10, "new_output": _assistant},
    ]
    sandbox = _FakeSandbox(polls)
    flag = {"cancel": False}

    async def emit(chunk):
        flag["cancel"] = True  # request cancel after first progress

    result = await run_code_agent(
        run_id="run-3",
        agent="codex",
        invocation=_inv(),
        env={},
        codex_auth_json=None,
        execute=sandbox.execute,
        emit_progress=emit,
        should_cancel=lambda: flag["cancel"],
        sleep=_noop_sleep,
        config=RunnerConfig(poll_interval=0.0),
    )
    assert result.error is not None
    assert "interrupt" in result.error.lower() or "cancel" in result.error.lower()
    assert any(c[0] == "cancel" for c in sandbox.calls)
    # Named as launch answered, never by a file the sandbox can change.
    cancel = next(c[1] for c in sandbox.calls if c[0] == "cancel")
    assert (cancel["pid"], cancel["start"]) == (99, 1234)


async def test_deadline_exceeded_cancels():
    # Never-done polls; a tiny deadline with a fake clock.
    polls = [
        {"ok": True, "done": False, "exit_code": None, "offset": i, "new_output": "x"}
        for i in range(100)
    ]
    sandbox = _FakeSandbox(polls)
    clock = {"t": 0.0}

    def now():
        clock["t"] += 10.0
        return clock["t"]

    result = await run_code_agent(
        run_id="run-4",
        agent="claude",
        invocation=_inv(),
        env={},
        codex_auth_json=None,
        execute=sandbox.execute,
        emit_progress=lambda chunk: _noop_sleep(0),
        should_cancel=lambda: False,
        sleep=_noop_sleep,
        now=now,
        config=RunnerConfig(poll_interval=0.0, max_wait=25.0),
    )
    assert result.error is not None
    assert "deadline" in result.error.lower() or "too long" in result.error.lower()
    assert any(c[0] == "cancel" for c in sandbox.calls)


async def test_codex_auth_json_forwarded_to_launch():
    polls = [{
        "ok": True, "done": True, "exit_code": 0, "offset": 30,
        "new_output": json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "ok"},
        }) + "\n",
    }]
    sandbox = _FakeSandbox(polls)
    await run_code_agent(
        run_id="run-5",
        agent="codex",
        invocation=CodeInvocation(argv=["codex", "exec"], stdin=None),
        env={},
        codex_auth_json='{"tokens":{"access_token":"t"}}',
        execute=sandbox.execute,
        emit_progress=lambda chunk: _noop_sleep(0),
        should_cancel=lambda: False,
        sleep=_noop_sleep,
        config=RunnerConfig(poll_interval=0.0),
    )
    launch_payload = sandbox.calls[0][1]
    assert launch_payload["codex_auth_json"] == '{"tokens":{"access_token":"t"}}'


async def test_codex_auth_writeback_surfaced_in_result():
    polls = [{
        "ok": True, "done": True, "exit_code": 0, "offset": 30,
        "new_output": json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "ok"},
        }) + "\n",
        "codex_auth_json": '{"tokens":{"access_token":"refreshed"}}',
    }]
    sandbox = _FakeSandbox(polls)
    result = await run_code_agent(
        run_id="run-6",
        agent="codex",
        invocation=CodeInvocation(argv=["codex", "exec"], stdin=None),
        env={},
        codex_auth_json='{"tokens":{"access_token":"old"}}',
        execute=sandbox.execute,
        emit_progress=lambda chunk: _noop_sleep(0),
        should_cancel=lambda: False,
        sleep=_noop_sleep,
        config=RunnerConfig(poll_interval=0.0),
    )
    assert result.updated_codex_auth_json == '{"tokens":{"access_token":"refreshed"}}'


async def test_extra_env_and_workdir_forwarded_to_launch():
    polls = [
        {
            "ok": True, "done": True, "exit_code": 0, "offset": 10,
            "new_output": json.dumps({"type": "result", "result": "ok"}) + "\n",
        },
    ]
    sandbox = _FakeSandbox(polls)

    await run_code_agent(
        run_id="run-2",
        agent="claude",
        invocation=_inv(),
        env={"CLAUDE_CODE_OAUTH_TOKEN": "tok"},
        codex_auth_json=None,
        execute=sandbox.execute,
        emit_progress=lambda chunk: _noop_sleep(0),
        should_cancel=lambda: False,
        sleep=_noop_sleep,
        config=RunnerConfig(poll_interval=0.0),
        extra_env={"GH_TOKEN": "github_pat_x"},
        workdir="/workspace/api",
    )

    launch_payload = sandbox.calls[0][1]
    assert launch_payload["env"]["GH_TOKEN"] == "github_pat_x"
    assert launch_payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert launch_payload["workdir"] == "/workspace/api"


async def test_a_run_whose_directory_the_pod_could_not_remove_is_reported(caplog):
    sandbox = _FakeSandbox([
        {"ok": True, "done": True, "exit_code": 0, "offset": 0, "new_output": "", "cleaned": False},
    ])
    with caplog.at_level("WARNING", logger="surogates.coding_agents.runner"):
        result = await run_code_agent(
            run_id="r-left", agent="claude", invocation=_inv(), env={}, codex_auth_json=None,
            execute=sandbox.execute, emit_progress=lambda _chunk: None, should_cancel=lambda: False,
            sleep=_noop_sleep,
        )
    assert result.cleanup_incomplete is True
    assert "event=code_run.cleanup_incomplete run_id=r-left" in caplog.text


async def test_a_cancelled_run_whose_directory_the_pod_could_not_remove_is_reported():
    class LeftBehind(_FakeSandbox):
        async def execute(self, name, input_json):
            if json.loads(input_json)["action"] == "cancel":
                await super().execute(name, input_json)
                return json.dumps({"ok": True, "cleaned": False})
            return await super().execute(name, input_json)

    sandbox = LeftBehind([])
    result = await run_code_agent(
        run_id="r-stop", agent="claude", invocation=_inv(), env={}, codex_auth_json=None,
        execute=sandbox.execute, emit_progress=lambda _chunk: None, should_cancel=lambda: True,
        sleep=_noop_sleep,
    )
    assert result.cleanup_incomplete is True


async def test_an_older_pod_that_does_not_say_is_not_reported():
    sandbox = _FakeSandbox([{"ok": True, "done": True, "exit_code": 0, "offset": 0, "new_output": ""}])
    result = await run_code_agent(
        run_id="r-old", agent="claude", invocation=_inv(), env={}, codex_auth_json=None,
        execute=sandbox.execute, emit_progress=lambda _chunk: None, should_cancel=lambda: False,
        sleep=_noop_sleep,
    )
    assert result.cleanup_incomplete is False


class _ReportingSandbox(_FakeSandbox):
    """A pod that says it answers ``cleaned``."""

    def __init__(self, polls, *, cancel=None, cancel_raises=False):
        super().__init__(polls)
        self.cancel_answer, self.cancel_raises = cancel, cancel_raises

    async def execute(self, name, input_json):
        payload = json.loads(input_json)
        if payload["action"] == "launch":
            await super().execute(name, input_json)
            return json.dumps({
                "ok": True, "run_id": payload["run_id"], "pid": 99, "start": 1234,
                "run_dir": [7, 8], "reports_cleaned": True,
            })
        if payload["action"] == "cancel":
            await super().execute(name, input_json)
            if self.cancel_raises:
                raise ConnectionError("pod gone")
            return json.dumps(self.cancel_answer)
        return await super().execute(name, input_json)


async def _run(sandbox, *, cancelled=False):
    return await run_code_agent(
        run_id="r", agent="claude", invocation=_inv(), env={}, codex_auth_json=None,
        execute=sandbox.execute, emit_progress=lambda _chunk: None, should_cancel=lambda: cancelled,
        sleep=_noop_sleep,
    )


async def test_a_reporting_pods_final_poll_with_no_answer_counts_as_not_cleaned():
    sandbox = _ReportingSandbox([{"ok": True, "done": True, "exit_code": 0, "offset": 0, "new_output": ""}])
    assert (await _run(sandbox)).cleanup_incomplete is True


async def test_a_reporting_pods_confirmed_cleanup_is_not_reported():
    sandbox = _ReportingSandbox([
        {"ok": True, "done": True, "exit_code": 0, "offset": 0, "new_output": "", "cleaned": True},
    ])
    assert (await _run(sandbox)).cleanup_incomplete is False


@pytest.mark.parametrize("answer", [{"ok": True}, {"ok": False, "error": "x"}, {"ok": True, "cleaned": "yes"}])
async def test_a_reporting_pods_cancel_with_no_boolean_answer_counts_as_not_cleaned(answer):
    assert (await _run(_ReportingSandbox([], cancel=answer), cancelled=True)).cleanup_incomplete is True


async def test_a_reporting_pods_failed_cancel_counts_as_not_cleaned():
    assert (await _run(_ReportingSandbox([], cancel_raises=True), cancelled=True)).cleanup_incomplete is True


async def test_poll_and_cancel_name_the_run_as_launch_did():
    polled = _ReportingSandbox([
        {"ok": True, "done": True, "exit_code": 0, "offset": 0, "new_output": "", "cleaned": True},
    ])
    await _run(polled)
    poll = next(c[1] for c in polled.calls if c[0] == "poll")
    assert poll["launched"] is True and poll["run_dir"] == [7, 8]

    stopped = _ReportingSandbox([], cancel={"ok": True, "cleaned": True})
    await _run(stopped, cancelled=True)
    cancel = next(c[1] for c in stopped.calls if c[0] == "cancel")
    assert cancel["launched"] is True and cancel["run_dir"] == [7, 8]
    assert (cancel["pid"], cancel["start"]) == (99, 1234)


async def test_a_refused_launch_that_left_its_directory_is_reported():
    class Refused(_FakeSandbox):
        async def execute(self, name, input_json):
            return json.dumps({"ok": False, "error": "no", "cleaned": False, "reports_cleaned": True})

    result = await _run(Refused([]))
    assert result.error and result.cleanup_incomplete is True
