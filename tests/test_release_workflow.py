"""The release workflow's rollout to the cluster waits for every other job of the run."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def test_the_rollout_runs_only_once_every_other_job_of_the_release_has_succeeded():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    reached: set[str] = set()
    waiting = _needs(jobs["rollout"])
    while waiting:
        name = waiting.pop()
        if name not in reached:
            reached.add(name)
            waiting.extend(_needs(jobs[name]))
    # A job its needs do not reach could fail, and the cluster would be rolled out all the same.
    assert reached == set(jobs) - {"rollout"}
    # Nothing in its condition lets it run past a job that failed, was skipped or was cancelled.
    assert jobs["rollout"].get("if", "success()") == "success()"
