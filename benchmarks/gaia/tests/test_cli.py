import json

import pytest

from gaia_bench.cli import (
    _require_env,
    build_parser,
    load_outcomes,
    save_outcomes,
)
from gaia_bench.report import TaskOutcome


def test_run_parses_split_and_limits():
    args = build_parser().parse_args(
        ["run", "--split", "dev", "--limit", "10", "--concurrency", "2"]
    )
    assert args.command == "run"
    assert args.split == "dev"
    assert args.limit == 10
    assert args.concurrency == 2


def test_run_defaults_concurrency_below_worker_limit():
    args = build_parser().parse_args(["run", "--split", "dev"])
    # The local worker runs concurrency: 10; going above just queues.
    assert args.concurrency <= 10


def test_tasks_flag_takes_a_comma_separated_list():
    args = build_parser().parse_args(
        ["run", "--split", "dev", "--tasks", "2a649bb1,2d83110e"]
    )
    assert args.tasks == "2a649bb1,2d83110e"


def test_select_tasks_matches_on_id_prefix():
    # Task ids are long uuids; the loop is driven from 8-char prefixes
    # shown in reports, so selection must accept those.
    from gaia_bench.cli import select_tasks

    class T:
        def __init__(self, tid):
            self.task_id = tid

    tasks = [T("2a649bb1-aaaa"), T("2d83110e-bbbb"), T("99999999-cccc")]
    got = select_tasks(tasks, "2a649bb1,2d83110e")
    assert [t.task_id for t in got] == ["2a649bb1-aaaa", "2d83110e-bbbb"]


def test_select_tasks_returns_all_when_unset():
    from gaia_bench.cli import select_tasks

    tasks = [type("T", (), {"task_id": "a"})()]
    assert select_tasks(tasks, None) == tasks


def test_select_tasks_raises_on_an_unmatched_id():
    # Silently running 2 of 3 requested tasks would look like a passing
    # verification of something never exercised.
    from gaia_bench.cli import select_tasks

    tasks = [type("T", (), {"task_id": "aaaa1111"})()]
    with pytest.raises(SystemExit, match="nope"):
        select_tasks(tasks, "aaaa1111,nope")


def test_only_failing_takes_a_run_id():
    args = build_parser().parse_args(
        ["run", "--split", "dev", "--only-failing", "run-1"]
    )
    assert args.only_failing == "run-1"


def test_report_accepts_compare():
    args = build_parser().parse_args(
        ["report", "run-2", "--compare", "run-1"]
    )
    assert args.run_id == "run-2"
    assert args.compare == "run-1"


def test_outcomes_round_trip(tmp_path):
    outcomes = [
        TaskOutcome(task_id="a", level=1, strict_pass=True,
                    lenient_pass=True, flags=[]),
        TaskOutcome(task_id="b", level=2, strict_pass=False,
                    lenient_pass=False, flags=["timeout"],
                    root_cause="gave_up_early", owner="iteration_caps"),
    ]
    path = tmp_path / "outcomes.json"
    save_outcomes(str(path), outcomes)
    loaded = load_outcomes(str(path))
    assert loaded == outcomes


def test_load_outcomes_missing_file_returns_empty(tmp_path):
    assert load_outcomes(str(tmp_path / "nope.json")) == []


def test_missing_credential_error_names_the_variable(monkeypatch):
    # A "credential missing" message that does not say WHICH variable
    # costs the reader an afternoon. Three unrelated tokens are in play.
    monkeypatch.delenv("SUROGATES_SA_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="SUROGATES_SA_TOKEN"):
        _require_env("SUROGATES_SA_TOKEN")


def test_empty_env_var_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("SUROGATES_SA_TOKEN", "")
    with pytest.raises(SystemExit, match="SUROGATES_SA_TOKEN"):
        _require_env("SUROGATES_SA_TOKEN")
