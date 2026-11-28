"""Offline suite plus live library seam tests (skipped without vendor)."""
import importlib.util
import json
import pathlib
import re

import pytest

from abbench.cli import next_run_id
from abbench.client import HarnessClient
from abbench.protocol import (
    build_preamble,
    is_complete,
    parse_tool_calls,
    tool_results_message,
)
from abbench.report import TaskOutcome, render, summarize

PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "abbench"

library_present = importlib.util.find_spec("automationbench") is not None


# -- isolation ---------------------------------------------------------

def test_no_product_imports():
    offenders = []
    for path in PACKAGE_DIR.rglob("*.py"):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if re.match(r"\s*(import|from)\s+(surogate_ops|surogates)\b", line):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == []


# -- protocol ----------------------------------------------------------

def test_preamble_carries_prompt_catalog_and_marker():
    text = build_preamble(
        "You are a workflow automation agent.",
        "Move the deal to closed-won.",
        [{"name": "api_search", "description": "Find endpoints",
          "parameters": [{"name": "query", "type": "str", "required": True}]}],
    )
    assert "workflow automation agent" in text
    assert "api_search" in text
    assert "TASK_COMPLETE" in text
    assert text.rstrip().endswith("Move the deal to closed-won.")


def test_parse_tool_calls_and_completion():
    calls, errors = parse_tool_calls(
        '```tool_call\n{"tool_name": "api_search", '
        '"tool_args": {"query": "crm deals"}}\n```\nmore text'
    )
    assert errors == []
    assert calls == [{"tool_name": "api_search",
                      "tool_args": {"query": "crm deals"}}]
    assert is_complete("done. TASK_COMPLETE")
    assert not is_complete("still working")
    calls, errors = parse_tool_calls("```tool_call\nbroken\n```")
    assert calls == [] and len(errors) == 1


def test_tool_results_message_shape():
    msg = tool_results_message(
        [{"tool_name": "api_search", "response": "[]"}]
    )
    assert msg.startswith("TOOL RESULTS:")
    assert "TASK_COMPLETE" in msg


# -- report ------------------------------------------------------------

def test_summarize_pass_rate_and_unscored():
    outcomes = [
        TaskOutcome("sales/1", "sales", 1.0, 1.0, tool_calls=5),
        TaskOutcome("sales/2", "sales", 0.5, 0.0, tool_calls=3),
        TaskOutcome("hr/3", "hr", None, None, score_error="boom"),
    ]
    s = summarize(outcomes)
    assert s["scored"] == 2 and s["unscored"] == 1
    assert s["overall"]["passed"] == 1
    assert s["overall"]["pass_rate"] == 50.0
    assert s["overall"]["partial_credit"] == 0.75
    text = render(outcomes, run_id="smoke-001")
    assert "`sales/2`" in text and "`hr/3`" in text and "boom" in text


# -- cli ---------------------------------------------------------------

def test_next_run_id_sequences_per_prefix(tmp_path):
    (tmp_path / "full-001").mkdir()
    (tmp_path / "smoke-009").mkdir()
    assert next_run_id(tmp_path, "full") == "full-002"
    assert next_run_id(tmp_path, "smoke") == "smoke-010"


# -- library seams (need vendored automationbench installed) -----------

@pytest.mark.skipif(not library_present, reason="automationbench not installed")
class TestLibrarySeams:
    def test_load_tasks_and_world(self):
        from abbench.bridge import load_domain_tasks, make_world

        tasks = load_domain_tasks("sales")
        assert len(tasks) == 100
        task = tasks[0]
        assert task.system_prompt and task.user_prompt
        assert isinstance(task.info.get("assertions"), list)

        world, info = make_world(task)
        assert world.meta.allowed_services
        assert info["assertions"]

    def test_catalog_hides_world_param(self):
        from abbench.bridge import tool_catalog

        catalog = tool_catalog()
        names = {t["name"] for t in catalog}
        assert "api_search" in names
        for tool in catalog:
            assert all(p["name"] != "world" for p in tool["parameters"])

    def test_dispatch_search_and_unknown(self):
        from abbench.bridge import dispatch, load_domain_tasks, make_world

        world, _ = make_world(load_domain_tasks("sales")[0])
        out = dispatch(world, "api_search", {"query": "deals"})
        assert isinstance(out, str) and out
        err = json.loads(dispatch(world, "not_a_tool", {}))
        assert "Unknown tool" in err["error"]

    def test_score_untouched_world_is_not_passing(self):
        from abbench.bridge import load_domain_tasks, make_world, score

        task = load_domain_tasks("sales")[0]
        world, info = make_world(task)
        partial, strict = score(world, info)
        assert 0.0 <= partial <= 1.0
        assert strict in (0.0, 1.0)
        # An untouched world must not strictly pass a real task.
        assert strict == 0.0

    def test_score_matches_upstream_setup_state(self):
        """score() == upstream's rubric over the state upstream's setup_state builds (free assertions excluded)."""
        import copy

        from automationbench import rubric
        from automationbench.runner import strip_none_values
        from abbench.bridge import load_domain_tasks, make_world, score

        for task in load_domain_tasks("sales")[:20]:
            world, info = make_world(task)
            up = {"world": world, "info": info,
                  "initial_state": copy.deepcopy(strip_none_values(task.info.get("initial_state") or {}))}
            want = (float(rubric.partial_credit(up)), float(rubric.task_completed_correctly(up)))
            assert score(world, info) == want


# -- session config ------------------------------------------------------

async def test_create_session_sends_the_extra_config_and_nothing_by_default():
    import httpx
    import respx

    with respx.mock:
        route = respx.post("http://localhost:8000/v1/api/sessions").mock(
            return_value=httpx.Response(200, json={"id": "sess-1"}))
        async with HarnessClient("http://localhost:8000", "tok", "agent-1") as client:
            assert await client.create_session() == "sess-1"
            assert await client.create_session({"current_date": "2026-03-15"}) == "sess-1"
    assert json.loads(route.calls[0].request.content)["config"] == {}
    assert json.loads(route.calls[1].request.content)["config"] == {"current_date": "2026-03-15"}


@pytest.mark.skipif(not library_present, reason="automationbench not installed")
def test_world_date_is_the_worlds_own_day():
    from abbench.bridge import load_domain_tasks, world_date

    tasks = [t for d in ("finance", "hr") for t in load_domain_tasks(d)]
    days = {t.task_id: world_date(t) for t in tasks}
    dated = {k: v for k, v in days.items() if v}
    assert dated, "some worlds set meta.current_time"
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in dated.values())
    task = next(t for t in tasks if days[t.task_id])
    raw = task.info["initial_state"]["meta"]["current_time"]
    assert str(raw).startswith(days[task.task_id])


@pytest.mark.parametrize("raw, day", [
    ("2026-03-15T09:00:00Z", "2026-03-15"),
    ("2026-03-15T23:30:00-05:00", "2026-03-16"),  # the world's clock in UTC, as the harness's date is
    ("2026-03-15T09:00:00", "2026-03-15"),        # no zone: UTC, the world's own default
    (None, None),
    ("not a time", None),                          # never a failed task: the run day
])
def test_world_date_reads_the_worlds_clock_offline(raw, day):
    from types import SimpleNamespace

    from abbench.bridge import world_date

    meta = {"schema_version": "0.1.0", "current_time": raw}
    assert world_date(SimpleNamespace(info={"initial_state": {"meta": meta}})) == day
    assert world_date(SimpleNamespace(info={"initial_state": {}})) is None
    assert world_date(SimpleNamespace(info={})) is None


def test_the_report_says_which_day_the_sessions_had():
    rows = [TaskOutcome(task_id="hr/1", domain="hr", partial_credit=1.0, strict=1.0, current_date="2026-03-15"),
            TaskOutcome(task_id="hr/2", domain="hr", partial_credit=0.0, strict=0.0)]
    assert "the world's own day on 1 task(s)" in render(rows, run_id="r")
    assert "Session date: the run day" in render(rows[1:], run_id="r")


def test_score_gives_the_rubric_the_initial_state(monkeypatch):
    """Offline: score() hands upstream's rubric the state upstream's setup_state builds, initial_state included
    (None-stripped, a copy), so assertions already true before the agent acts are not counted as passed."""
    import sys
    import types

    from abbench.bridge import score

    seen = {}
    rubric = types.SimpleNamespace(
        partial_credit=lambda state: seen.setdefault("state", state) and 0.5,
        task_completed_correctly=lambda state: 0.0,
    )
    runner = types.SimpleNamespace(strip_none_values=lambda d: {k: v for k, v in d.items() if v is not None})
    package = types.ModuleType("automationbench")
    package.rubric, package.runner = rubric, runner
    monkeypatch.setitem(sys.modules, "automationbench", package)
    monkeypatch.setitem(sys.modules, "automationbench.rubric", rubric)
    monkeypatch.setitem(sys.modules, "automationbench.runner", runner)
    initial = {"gmail": {"messages": []}, "slack": None}
    world, info = object(), {"assertions": [], "initial_state": initial}
    assert score(world, info) == (0.5, 0.0)
    assert seen["state"]["initial_state"] == {"gmail": {"messages": []}}
    assert seen["state"]["initial_state"] is not initial
    assert seen["state"]["world"] is world and seen["state"]["info"] is info
