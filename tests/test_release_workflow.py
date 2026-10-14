"""The release workflow's rollout to the cluster waits for every other job of the release it rolls out.

How the release signs Windows is decided once for the run, by desktop-signing-mode. In a trial, Windows publishes
nothing, and the release is every job but Windows's: its rollout waits for none of them, and nothing of Linux's does.
Where the account signs, Windows's publish is the release's too, and the rollout that runs then waits for it as well.
Each reads that job's word, never the variable it was decided by.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
MODE = "needs.desktop-signing-mode.outputs.signing"
ROLLOUTS = {
    "rollout": f"{MODE} == 'trial'",
    "rollout-with-windows": f"{MODE} == 'account'",
}


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _reached(jobs: dict, name: str) -> set[str]:
    reached: set[str] = set()
    waiting = _needs(jobs[name])
    while waiting:
        need = waiting.pop()
        if need not in reached:
            reached.add(need)
            waiting.extend(_needs(jobs[need]))
    return reached


def _windows(jobs: dict) -> set[str]:
    return {name for name in jobs if name.startswith("desktop-win-")}


def test_the_release_rolls_the_cluster_out_by_one_job_or_the_other_and_never_past_a_failed_job():
    jobs = _jobs()
    assert {name for name, job in jobs.items() if job.get("uses") == "./.github/workflows/update-images.yml"} == set(ROLLOUTS)
    # A condition with no status check of its own is success() and it: never always(), !cancelled() or a result read by
    # hand, which would roll out a release that failed. And the two conditions never hold at once.
    for name, condition in ROLLOUTS.items():
        assert jobs[name]["if"] == condition
        assert "desktop-signing-mode" in _needs(jobs[name])


def test_while_windows_publishes_nothing_no_windows_job_holds_the_rollout_or_linux_s_release():
    jobs = _jobs()
    windows = _windows(jobs)
    # A job its needs do not reach could fail, and the cluster would be rolled out all the same.
    assert _reached(jobs, "rollout") == set(jobs) - set(ROLLOUTS) - windows
    # And no job of Linux's or of the cloud's release waits for one of Windows's: a trial of Windows's that fails stops none.
    for name in set(jobs) - set(ROLLOUTS) - windows:
        assert not _reached(jobs, name) & windows, name


def test_once_windows_publishes_the_rollout_waits_for_its_publish_as_for_every_other_job():
    jobs = _jobs()
    assert _reached(jobs, "rollout-with-windows") == set(jobs) - set(ROLLOUTS)
    assert "desktop-win-publish" in _needs(jobs["rollout-with-windows"])
    assert jobs["desktop-win-publish"]["if"] == f"{MODE} == 'account'"


def test_only_the_deciding_job_reads_how_the_run_signs():
    jobs = _jobs()
    readers = {name for name, job in jobs.items() if "vars.ARTIFACT_SIGNING_ACCOUNT" in yaml.safe_dump(job)}
    assert readers == {"desktop-signing-mode"}
    # It needs nothing, holds nothing, and so cannot hold the release.
    assert not _needs(jobs["desktop-signing-mode"])
    assert jobs["desktop-signing-mode"]["permissions"] == {}
