"""The release workflow's rollout to the cluster waits for every other job of the release it rolls out.

How the release signs Windows and macOS is decided once for the run, by desktop-signing-mode. In Windows's trial, Windows
publishes nothing, and the release is every job but Windows's: its rollout waits for none of them, and nothing of Linux's
does. Where the account signs, Windows's publish is the release's too, and the rollout that runs then waits for it as
well. macOS is the release's through one job, desktop-mac-released, which each rollout waits for: macOS's publish where
the Developer ID signs, and, in macOS's trial, nothing of the trial's own jobs. Each reads that job's words, never the
variables they were decided by.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
MODE = "needs.desktop-signing-mode.outputs.signing"
MAC_MODE = "needs.desktop-signing-mode.outputs.mac-signing"
ROLLOUTS = {
    "rollout": f"{MODE} == 'trial'",
    "rollout-with-windows": f"{MODE} == 'account'",
}
MAC_TRIAL = {"desktop-mac-trial", "desktop-mac-trial-describe"}


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


def _mac(jobs: dict) -> set[str]:
    return {name for name in jobs if name.startswith("desktop-mac-") or name in {"desktop-vm-image-arm64", "desktop-vm-manifest-arm64"}}


def test_the_release_rolls_the_cluster_out_by_one_job_or_the_other_and_never_past_a_failed_job():
    jobs = _jobs()
    assert {name for name, job in jobs.items() if job.get("uses") == "./.github/workflows/update-images.yml"} == set(ROLLOUTS)
    # A condition with no status check of its own is success() and it: never always(), !cancelled() or a result read by
    # hand, which would roll out a release that failed. And the two conditions never hold at once.
    for name, condition in ROLLOUTS.items():
        assert jobs[name]["if"] == condition
        assert "desktop-signing-mode" in _needs(jobs[name])
        assert "desktop-mac-released" in _needs(jobs[name])


def test_while_windows_publishes_nothing_no_windows_job_holds_the_rollout_or_linux_s_release():
    jobs = _jobs()
    windows = _windows(jobs)
    # A job its needs do not reach could fail, and the cluster would be rolled out all the same.
    assert _reached(jobs, "rollout") == set(jobs) - set(ROLLOUTS) - windows - MAC_TRIAL
    # And no job of Linux's or of the cloud's release waits for one of Windows's or macOS's: a trial that fails stops none.
    for name in set(jobs) - set(ROLLOUTS) - windows - _mac(jobs):
        assert not _reached(jobs, name) & (windows | _mac(jobs)), name


def test_once_windows_publishes_the_rollout_waits_for_its_publish_as_for_every_other_job():
    jobs = _jobs()
    assert _reached(jobs, "rollout-with-windows") == set(jobs) - set(ROLLOUTS) - MAC_TRIAL
    assert "desktop-win-publish" in _needs(jobs["rollout-with-windows"])
    assert jobs["desktop-win-publish"]["if"] == f"{MODE} == 'account'"


def test_macos_is_the_release_s_by_its_publish_where_the_developer_id_signs_and_its_trial_holds_nothing():
    jobs = _jobs()
    gate = jobs["desktop-mac-released"]
    assert gate["if"] == (
        f"${{{{ !cancelled() && ({MAC_MODE} == 'trial' || (needs.desktop-mac-publish.result == 'success'"
        " && needs.desktop-vm-manifest-arm64.result == 'success')) }}"
    )
    assert _needs(gate) == ["desktop-signing-mode", "desktop-mac-publish", "desktop-vm-manifest-arm64"]
    assert gate["permissions"] == {}
    # Every job of macOS's publishing runs only where the Developer ID signs, by its own condition or by needing one
    # that has it, so that in a trial each is skipped as soon as its needs end; the trial only where none signs.
    for name in ("desktop-vm-image-arm64", "desktop-mac-build", "desktop-mac-publish"):
        assert jobs[name]["if"] == f"{MAC_MODE} == 'developer-id'", name
    for name in ("desktop-mac-sign", "desktop-mac-describe", "desktop-vm-manifest-arm64"):
        assert "desktop-vm-image-arm64" in _reached(jobs, name), name
    assert jobs["desktop-mac-trial"]["if"] == f"{MAC_MODE} == 'trial'"
    # No job waits for the trial but its describe.
    for name, job in jobs.items():
        if name != "desktop-mac-trial-describe":
            assert not set(_needs(job)) & MAC_TRIAL, name
    # And the arm64 guest image is built and attached only by macOS's publishing.
    assert _reached(jobs, "desktop-mac-released") & _mac(jobs) == _mac(jobs) - MAC_TRIAL - {"desktop-mac-released"}


def test_only_the_deciding_job_reads_how_the_run_signs():
    jobs = _jobs()
    for variable in ("vars.ARTIFACT_SIGNING_ACCOUNT", "vars.APPLE_TEAM_ID"):
        readers = {name for name, job in jobs.items() if variable in yaml.safe_dump(job)}
        assert readers == {"desktop-signing-mode"}, variable
    # It needs nothing, holds nothing, and so cannot hold the release.
    assert not _needs(jobs["desktop-signing-mode"])
    assert jobs["desktop-signing-mode"]["permissions"] == {}
