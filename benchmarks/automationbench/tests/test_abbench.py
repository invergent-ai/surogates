"""Offline suite plus live library seam tests (skipped without vendor)."""
import importlib.util
import json
import pathlib
import re

import pytest

from abbench.cli import next_run_id
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
