"""Success-rate math and unverifiable handling."""
from eogbench.report import TaskOutcome, render, summarize


def _outcome(task_id="csm/a", domain="csm", passed=True, vp=2, vt=2, **kw):
    fields = dict(
        task_id=task_id, domain=domain, passed=passed,
        verifiers_total=vt, verifiers_passed=vp,
        terminal_status="completed",
    )
    fields.update(kw)
    return TaskOutcome(**fields)


def test_summarize_excludes_unverifiable():
    outcomes = [
        _outcome("csm/a", passed=True),
        _outcome("csm/b", passed=False, vp=1),
        _outcome("itsm/c", "itsm", passed=None, vp=0, vt=0,
                 verify_error="gym unreachable"),
    ]
    s = summarize(outcomes)
    assert s["verifiable"] == 2
    assert s["unverifiable"] == 1
    assert s["success_rate"] == 50.0
    assert s["by_domain"]["csm"]["success_rate"] == 50.0
    assert "itsm" not in s["by_domain"]  # no verifiable rows there


def test_render_lists_failures_with_verifier_names():
    text = render([
        _outcome("csm/a", passed=True),
        _outcome("csm/b", passed=False, vp=1,
                 failed_verifiers=["update_entitlement"]),
        _outcome("itsm/c", "itsm", passed=None, vp=0, vt=0,
                 verify_error="gym unreachable"),
    ], run_id="smoke-001")
    assert "**1/2**" in text
    assert "update_entitlement" in text
    assert "gym unreachable" in text
    assert "| `csm/a` |" not in text  # passing task not in failed table
