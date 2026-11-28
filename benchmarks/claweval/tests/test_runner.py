from claweval_bench.client import Event
from claweval_bench.runner import was_rate_limited


def _ev(type_: str, data: dict) -> Event:
    return Event(id=1, type=type_, data=data)


def test_rate_limit_detected_in_crash():
    events = [
        _ev("tool.call", {"name": "x"}),
        _ev("harness.crash", {
            "error": "Provider is rate-limited for 295 more seconds; "
                     "skipping API call.",
        }),
    ]
    assert was_rate_limited(events) is True


def test_rate_limit_detected_in_session_fail():
    events = [_ev("session.fail", {"error": "rate-limited for 182 more seconds"})]
    assert was_rate_limited(events) is True


def test_ordinary_failure_is_not_rate_limit():
    events = [
        _ev("harness.crash", {"error": "some other internal error"}),
        _ev("session.fail", {"reason": "max_retries_exhausted"}),
    ]
    assert was_rate_limited(events) is False


def test_rate_limit_string_only_counts_in_crash_events():
    # A tool result that merely mentions the phrase must not trip backoff.
    events = [_ev("tool.result", {"text": "docs on being rate-limited"})]
    assert was_rate_limited(events) is False


def test_a_task_with_a_fixed_day_gives_it_as_the_session_date_only_when_asked():
    from types import SimpleNamespace

    from claweval_bench.runner import session_config

    dated = SimpleNamespace(environment=SimpleNamespace(mock_today="2026-03-26"))
    undated = SimpleNamespace(environment=SimpleNamespace(mock_today=None))
    assert session_config(dated, world_date=True) == {"current_date": "2026-03-26"}
    assert session_config(dated) is None  # default: the run day, as before
    assert session_config(undated, world_date=True) is None


def test_the_session_date_survives_a_resume_and_reaches_the_report(tmp_path):
    import json
    from types import SimpleNamespace

    from claweval_bench import cli, report
    from claweval_bench.runner import RolloutResult, _persist

    task_dir = tmp_path / "tasks" / "T1"
    task_dir.mkdir(parents=True)
    _persist(task_dir, RolloutResult(task_id="T1", session_id="s", terminal_status="completed",
                                     current_date="2026-03-26"))
    assert json.loads((task_dir / "meta.json").read_text())["current_date"] == "2026-03-26"
    (task_dir / "scores.json").write_text(json.dumps({"scores": None, "grader_error": "x"}))
    resumed = cli._resume_outcome(tmp_path, SimpleNamespace(task_id="T1", category="c"))
    assert resumed["current_date"] == "2026-03-26"
    assert "the task's own day on 1 task(s)" in report.render([resumed], run_id="r")
    assert "Session date: the run day" in report.render([{**resumed, "current_date": None}], run_id="r")
