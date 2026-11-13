"""A session that reports its end elsewhere starts its agents in the foreground, and runs no monitor.

A sub-agent reports to its parent, a Keel case's step or work item to Keel,
and a routine's run where the routine says.  An agent such a session left
running in the background would end after that report: its notification
would wake a turn that is no step's, no item's, or announce a second time.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.subagents import reports_elsewhere
from surogates.keel.steps import CASE_STEP_KEY
from tests.test_case_step_isolation import CONTRACT


def a_session(*, parent=None, channel="web", config=None):
    return SimpleNamespace(id=uuid4(), parent_id=parent, channel=channel, config=config or {})


def test_a_chat_reports_to_nobody_else():
    assert not reports_elsewhere(a_session())


def test_a_sub_agent_a_case_step_and_a_routine_run_report_elsewhere():
    assert reports_elsewhere(a_session(parent=uuid4(), channel="delegation"))
    assert reports_elsewhere(a_session(channel="api", config={CASE_STEP_KEY: CONTRACT}))
    assert reports_elsewhere(a_session(channel="scheduled"))
    assert reports_elsewhere(a_session(config={"scheduled_session_id": str(uuid4())}))
