from gaia_bench.client import Event
from gaia_bench.detectors import detect, tool_error_names
from gaia_bench.runner import RolloutResult


def result(events, **kw):
    base = dict(
        task_id="t", session_id="s", answer=None,
        events=events, wall_clock_s=1.0,
        terminal_status="completed", error=None,
    )
    base.update(kw)
    return RolloutResult(**base)


def llm_response(text="ok", finish="stop"):
    return Event(id=1, type="llm.response",
                 data={"message": {"content": text}, "finish_reason": finish})


def tool_call(name):
    return Event(id=2, type="tool.call", data={"name": name})


def tool_result(name, content):
    return Event(id=3, type="tool.result", data={"name": name, "content": content})


def session_complete(reason):
    return Event(id=9, type="session.complete", data={"reason": reason})


def test_no_final_answer_when_marker_absent():
    r = result([llm_response("I think 4.")], answer=None)
    assert "no_final_answer" in detect(r, level=1)


def test_no_final_answer_absent_when_answer_present():
    r = result([llm_response("FINAL ANSWER: 4")], answer="4")
    assert "no_final_answer" not in detect(r, level=1)


def test_empty_response_from_thinking_budget_completion():
    r = result([llm_response("", finish="length"),
                session_complete("thinking_budget_exhausted")])
    assert "empty_response" in detect(r, level=1)


def test_empty_response_not_flagged_on_normal_completion():
    r = result([llm_response("some text", finish="length"),
                session_complete("stop")], answer="4")
    assert "empty_response" not in detect(r, level=1)


def test_tool_error_from_error_payload():
    r = result([
        tool_call("web_search"),
        tool_result("web_search", {"error": "Tool execution failed: boom"}),
    ], answer="4")
    assert "tool_error" in detect(r, level=1)
    assert tool_error_names(r) == ["web_search"]


def test_tool_error_detects_string_failure_content():
    r = result([
        tool_result("read_file", "Tool execution failed: unsupported format"),
    ], answer="4")
    assert "tool_error" in detect(r, level=1)


def test_no_tool_error_on_clean_result():
    r = result([tool_result("web_search", {"results": []})], answer="4")
    assert "tool_error" not in detect(r, level=1)
    assert tool_error_names(r) == []


def test_tool_output_mentioning_a_traceback_is_not_an_error():
    # Regression: a successful GitHub API fetch of a bug report whose body
    # contains a Python traceback was flagged as a tool failure. Prose
    # about errors is data, not a failure signal.
    body = '{"output": "Traceback (most recent call last): ValueError"}'
    r = result([tool_result("terminal", body)], answer="4")
    assert "tool_error" not in detect(r, level=1)
    assert tool_error_names(r) == []


def test_nonzero_exit_code_without_error_is_not_a_tool_error():
    # grep exiting 1 means "no matches", which the harness labels
    # explicitly. Treating it as failure would flag healthy searches.
    body = ('{"output": "", "exit_code": 1, "error": null, '
            '"exit_code_meaning": "No matches found (not an error)"}')
    r = result([tool_result("terminal", body)], answer="4")
    assert "tool_error" not in detect(r, level=1)


def test_harness_failure_envelope_is_still_detected():
    r = result([tool_result("read_file", "Tool execution failed: boom")],
               answer="4")
    assert "tool_error" in detect(r, level=1)


def test_timeout_from_terminal_status():
    r = result([llm_response()], terminal_status="timeout", answer="4")
    assert "timeout" in detect(r, level=1)


def test_step_cap_from_budget_exhausted_completion():
    r = result([llm_response(), session_complete("budget_exhausted")],
               answer="4")
    assert "step_cap" in detect(r, level=1)


def test_step_cap_not_flagged_on_normal_completion():
    r = result([llm_response(), session_complete("stop")], answer="4")
    assert "step_cap" not in detect(r, level=1)


def session_fail(reason, attempts=3):
    return Event(id=9, type="session.fail",
                 data={"reason": reason, "attempts": attempts})


def test_empty_llm_response_is_its_own_cause_not_infra_noise():
    # The harness gives up after N empty completions from the provider.
    # That is a reproducible model-level failure, not an infrastructure
    # blip, and lumping it into infra_error hides it from the fix list.
    r = result([llm_response("", finish="stop"),
                session_fail("empty_llm_response")],
               terminal_status="failed")
    flags = detect(r, level=2)
    assert "empty_llm_response" in flags
    assert "infra_error" not in flags


def test_other_session_failures_are_still_infra_errors():
    r = result([session_fail("worker_crash")], terminal_status="failed")
    flags = detect(r, level=1)
    assert "infra_error" in flags
    assert "empty_llm_response" not in flags


def test_infra_error_from_error_field():
    r = result([], terminal_status="error",
               error="HarnessError: create_session failed (HTTP 503)")
    assert "infra_error" in detect(r, level=1)


def test_infra_error_from_failed_status():
    r = result([llm_response()], terminal_status="failed")
    assert "infra_error" in detect(r, level=1)


def test_no_tool_use_flagged_for_level_two_without_tools():
    r = result([llm_response("FINAL ANSWER: 4")], answer="4")
    assert "no_tool_use" in detect(r, level=2)


def test_no_tool_use_not_flagged_for_level_one():
    r = result([llm_response("FINAL ANSWER: 4")], answer="4")
    assert "no_tool_use" not in detect(r, level=1)


def test_no_tool_use_not_flagged_when_browsing_happened():
    r = result([tool_call("web_search"), llm_response("FINAL ANSWER: 4")],
               answer="4")
    assert "no_tool_use" not in detect(r, level=2)


def test_no_tool_use_not_flagged_for_terminal_research():
    # Regression: an agent that researched a GitHub issue via terminal+curl
    # was flagged as having done no work. Retrieval is not only the browse
    # tools; choosing a poor path is Stage-2's wrong_retrieval_path, not
    # this detector's job.
    r = result([tool_call("terminal"), llm_response("FINAL ANSWER: 4")],
               answer="4")
    assert "no_tool_use" not in detect(r, level=2)


def test_clean_run_has_no_flags():
    r = result([tool_call("web_search"), llm_response("FINAL ANSWER: 4")],
               answer="4")
    assert detect(r, level=2) == []
