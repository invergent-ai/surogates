"""AC/TSQ aggregation math and rendering."""
from galbench.report import ScenarioOutcome, find_regressions, render, summarize


def _outcome(sid="banking-000", domain="banking", done=4, total=5,
             good=3, calls=4, **overrides):
    fields = dict(
        scenario_id=sid, domain=domain,
        goals_total=total, goals_done=done,
        calls_total=calls, calls_good=good,
        user_turns=3, agent_messages=6,
        completed_marker=True, terminal_status="completed",
    )
    fields.update(overrides)
    return ScenarioOutcome(**fields)


def test_ac_tsq_properties():
    o = _outcome(done=4, total=5, good=3, calls=4)
    assert o.ac == 0.8
    assert o.tsq == 0.75
    assert _outcome(calls=0, good=0).tsq is None


def test_summarize_macro_averages_and_domains():
    outcomes = [
        _outcome("banking-000", "banking", done=5, total=5),
        _outcome("telecom-000", "telecom", done=0, total=5, calls=0, good=0),
    ]
    s = summarize(outcomes)
    assert s["overall"]["ac"] == 0.5
    assert s["overall"]["tsq"] == 0.75  # only the scenario with calls
    assert s["by_domain"]["banking"]["ac"] == 1.0
    assert s["by_domain"]["telecom"]["no_tool_calls"] == 1


def test_regressions_cross_the_half_line():
    prev = [_outcome("banking-000", done=3, total=5)]
    cur = [_outcome("banking-000", done=1, total=5)]
    assert find_regressions(prev, cur) == ["banking-000"]
    assert find_regressions(cur, cur) == []


def test_render_failed_table_and_order():
    text = render(
        [_outcome(done=5), _outcome("telecom-001", "telecom", done=1, total=5,
                                    calls=0, good=0)],
        previous=[_outcome("telecom-001", "telecom", done=5, total=5)],
        run_id="dev-001",
    )
    assert text.index("## Regressions") < text.index("## Score")
    assert "`telecom-001`" in text
    assert "no tool calls made" in text
