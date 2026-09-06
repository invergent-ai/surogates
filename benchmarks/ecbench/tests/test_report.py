"""Aggregation math and report rendering."""
from ecbench.report import render, summarize
from ecbench.scorer import EpisodeOutcome


def _outcome(episode=1, assets=150000.0, stake=100000.0, **overrides):
    fields = dict(
        episode=episode,
        final_assets=assets,
        initial_balance=stake,
        days_completed=365,
        max_days=365,
        done=True,
        source="final_state",
        terminal_status="completed",
        tool_batches=100,
        wall_clock_s=1000.0,
    )
    fields.update(overrides)
    return EpisodeOutcome(**fields)


def test_summarize_means_and_unscoreable():
    outcomes = [
        _outcome(1, assets=200000.0),
        _outcome(2, assets=100000.0),
        _outcome(3, assets=None, source="none", terminal_status="failed"),
    ]
    s = summarize(outcomes)
    assert s["episodes"] == 3
    assert s["scored"] == 2
    assert s["unscoreable"] == 1
    assert s["mean_assets"] == 150000.0
    assert s["mean_multiple"] == 1.5
    assert s["full_horizon"] == 2


def test_render_lists_every_episode():
    text = render([
        _outcome(1, assets=143000.0),
        _outcome(2, assets=None, source="none",
                 error="HarnessError: boom", days_completed=0, done=False),
    ], run_id="smoke-001")
    assert "Mean final assets" in text
    assert "143,000" in text
    assert "| 02 | -- | -- |" in text
    assert "HarnessError: boom" in text


def test_render_with_nothing_scoreable():
    text = render([_outcome(1, assets=None, source="none", done=False)],
                  run_id="smoke-002")
    assert "No scoreable episodes" in text
