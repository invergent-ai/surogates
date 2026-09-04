"""Offline episode scoring from stored artifacts."""
import json

from ecbench.scorer import score_episode


def _episode_dir(tmp_path, meta=None, final=None, calls=0):
    d = tmp_path / "episodes" / "01"
    d.mkdir(parents=True)
    base_meta = {
        "episode": 1,
        "session_id": "s",
        "wall_clock_s": 120.0,
        "terminal_status": "completed",
        "error": None,
        "artifacts": [],
        "collect_notes": [],
    }
    base_meta.update(meta or {})
    (d / "meta.json").write_text(json.dumps(base_meta))
    if final is not None:
        (d / "final_state.json").write_text(json.dumps(final))
    if calls:
        (d / "calls.jsonl").write_text(
            "\n".join('{"day": 1}' for _ in range(calls)) + "\n"
        )
    return str(d)


def test_scores_from_final_state(tmp_path):
    d = _episode_dir(tmp_path, final={
        "final_assets": 145000.0,
        "initial_balance": 100000.0,
        "final_day": 30,
        "day_count": 30,
        "max_day": 30,
        "done": True,
    }, calls=42)
    o = score_episode(d)
    assert o.final_assets == 145000.0
    assert o.multiple == 1.45
    assert o.days_completed == 30
    assert o.done is True
    assert o.source == "final_state"
    assert o.tool_batches == 42


def test_no_artifacts_is_unscoreable_not_a_crash(tmp_path):
    d = _episode_dir(tmp_path, meta={
        "terminal_status": "failed",
        "error": "HarnessError: boom",
    })
    o = score_episode(d)
    assert o.final_assets is None
    assert o.multiple is None
    assert o.source == "none"
    assert any("no scoreable state" in n for n in o.notes)
    assert o.error == "HarnessError: boom"


def test_zero_stake_yields_no_multiple(tmp_path):
    d = _episode_dir(tmp_path, final={
        "final_assets": 0.0, "initial_balance": 0,
        "final_day": 1, "day_count": 1, "max_day": 365, "done": False,
    })
    assert score_episode(d).multiple is None
