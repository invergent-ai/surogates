"""A session that reports its end elsewhere starts its agents in the foreground, and runs no monitor.

A sub-agent reports to its parent, a Keel case's step or work item to Keel,
a routine's run where the routine says, and an evaluation row to the run
that scores it.  An agent such a session left running in the background
would end after that report: its notification would wake a turn that is no
step's, no item's, no score's, or announce a second time.
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


# An evaluation row is read as its turn ends, by the run that scores it: a background agent's report would
# wake a turn nobody scores, and the row would be scored without the agent's work.

EVAL_ROW = {"memory_boundary": "eval:run-1", "eval_partition_id": "run-1"}


def test_an_evaluation_row_reports_to_the_run_that_scores_it():
    assert reports_elsewhere(a_session(channel="api", config=EVAL_ROW))


def test_the_row_is_the_one_the_server_stamps():
    from surogates.api.routes.sessions import apply_eval_isolation

    # An api session naming an eval partition is stamped an evaluation row ...
    stamped = apply_eval_isolation({"eval_partition_id": "run-1"}, channel="api")
    assert reports_elsewhere(a_session(channel="api", config=stamped))
    # ... a boundary a client sends itself is dropped, on any channel ...
    for channel in ("web", "api"):
        forged = apply_eval_isolation({"memory_boundary": "eval:run-1"}, channel=channel)
        assert not reports_elsewhere(a_session(channel=channel, config=forged))
    # ... and a partition named in a session that never went through the stamp is no row.
    assert not reports_elsewhere(a_session(channel="api", config={"eval_partition_id": "run-1"}))


def test_an_api_session_that_is_no_evaluation_row_reports_to_nobody_else():
    assert not reports_elsewhere(a_session(channel="api"))


def test_an_evaluation_row_is_offered_no_monitor():
    from surogates.harness.subagents import MONITOR_TOOL, offer_family

    session = a_session(channel="api", config=EVAL_ROW)
    offered = offer_family({"agent", "terminal", "read_file"}, session.config, root=not reports_elsewhere(session))
    assert "agent" in offered and MONITOR_TOOL not in offered
    chat = a_session(channel="api")
    assert MONITOR_TOOL in offer_family({"agent", "terminal", "read_file"}, chat.config, root=not reports_elsewhere(chat))


class _Store:
    def __init__(self, session):
        self.session = session

    async def get_session(self, _id):
        return self.session


async def _call_agent(monkeypatch, config, arguments):
    """The agent tool's answer and the mode it chose, for a main session with *config*."""
    from surogates.tools.builtin import agents

    chosen = {}

    async def start(_kwargs, **kw):
        chosen["background"] = kw["background"]
        return "started"

    monkeypatch.setattr(agents, "_start_agent", start)
    monkeypatch.setattr(agents, "budget_for_children", lambda _config: 0)
    session = a_session(channel="api", config=config)
    answer = await agents._agent_handler(
        {"description": "read the report", "prompt": "Read report.txt and give its total.", **arguments},
        session_store=_Store(session), tenant=object(), session_id=session.id,
    )
    return answer, chosen.get("background")


async def test_an_evaluation_rows_agent_waits_for_its_report(monkeypatch):
    answer, background = await _call_agent(monkeypatch, EVAL_ROW, {})
    assert answer == "started" and background is False


async def test_an_evaluation_row_asking_for_the_background_waits_and_is_told_so(monkeypatch):
    from surogates.tools.builtin.agents import WAITED_NOTE

    answer, background = await _call_agent(monkeypatch, EVAL_ROW, {"run_in_background": True})
    assert background is False and answer == f"{WAITED_NOTE}\n\nstarted"


async def test_a_case_step_asking_for_the_background_waits_too(monkeypatch):
    from surogates.tools.builtin.agents import WAITED_NOTE

    answer, background = await _call_agent(monkeypatch, {CASE_STEP_KEY: CONTRACT}, {"run_in_background": True})
    assert background is False and answer.startswith(WAITED_NOTE)


async def test_a_sub_agent_asking_for_the_background_is_still_refused(monkeypatch):
    from surogates.tools.builtin import agents

    monkeypatch.setattr(agents, "budget_for_children", lambda _config: 0)
    child = a_session(parent=uuid4(), channel="delegation", config={"delegation_depth": 1})
    answer = await agents._agent_handler(
        {"description": "go on", "prompt": "Do more.", "run_in_background": True},
        session_store=_Store(child), tenant=object(), session_id=child.id,
    )
    assert "cannot start agents in the background" in answer


async def test_an_evaluation_row_resumes_no_agent_its_report_would_come_after_the_row(monkeypatch):
    # A finished agent resumed by send_message runs in the background: its report would miss the scored turn.
    from surogates.tools.builtin import agents

    row = a_session(channel="api", config=EVAL_ROW)
    child = SimpleNamespace(id=uuid4(), parent_id=row.id, status="completed", config={})
    why = await agents._why_not_resume(_Store(child), row, child, None)
    assert why is not None and "start a new agent" in why
    chat = a_session(channel="api")
    child.parent_id = chat.id
    assert await agents._why_not_resume(_Store(child), chat, child, None) is None


def test_only_a_main_session_that_reports_elsewhere_reads_that_its_agents_wait():
    from tests.test_agent_guidance import builder, fragment

    wait = fragment("guidance/agents_wait")
    assert wait in builder({"agent", "send_message"}, EVAL_ROW).build()
    assert wait in builder({"agent", "send_message"}, {CASE_STEP_KEY: CONTRACT}).build()
    assert wait not in builder({"agent", "send_message"}, {}).build()


async def test_an_api_chats_agent_still_runs_in_the_background(monkeypatch):
    answer, background = await _call_agent(monkeypatch, {}, {})
    assert answer == "started" and background is True
