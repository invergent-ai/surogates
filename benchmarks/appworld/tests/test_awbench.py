"""Offline suite plus appworld seam checks (skipped without the package)."""
import importlib.util
import pathlib
import re

import pytest

from awbench.cli import next_run_id, select_ids
from awbench.report import TaskOutcome, render, summarize
from awbench.runner import build_prompt, evaluation_summary

PACKAGE_DIR = pathlib.Path(__file__).parent.parent / "awbench"


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


# -- prompt ------------------------------------------------------------

def test_build_prompt_carries_identity_and_instruction():
    prompt = build_prompt("Send $20 to my roommate on Venmo.", {
        "first_name": "Lily", "last_name": "Miller",
        "email": "lily@example.com", "phone_number": "555-0101",
    })
    assert "Lily Miller" in prompt
    assert "lily@example.com" in prompt
    assert "Send $20 to my roommate on Venmo." in prompt
    assert "supervisor app" in prompt


def test_build_prompt_handles_missing_supervisor_fields():
    prompt = build_prompt("Do it.", {})
    assert "the user" in prompt
    assert "unknown" in prompt


# -- evaluation normalization -----------------------------------------

class FakeEvaluation:
    def __init__(self, report):
        self._report = report

    def to_dict(self):
        return self._report


def test_evaluation_summary_passes_on_empty_failures():
    passed, report = evaluation_summary(
        FakeEvaluation({"failures": [], "passes": ["t1", "t2"]})
    )
    assert passed is True
    assert report["passes"] == ["t1", "t2"]


def test_evaluation_summary_fails_on_failures():
    passed, _ = evaluation_summary(
        FakeEvaluation({"failures": ["t3"], "passes": ["t1"]})
    )
    assert passed is False


def test_evaluation_summary_unknown_shape_is_none():
    class Opaque:
        pass

    passed, report = evaluation_summary(Opaque())
    assert passed is None
    assert "repr" in report


# -- report ------------------------------------------------------------

def test_summarize_and_render():
    outcomes = [
        TaskOutcome("t1", True),
        TaskOutcome("t2", False, failures=2, terminal_status="completed"),
        TaskOutcome("t3", None, evaluate_error="boom"),
    ]
    s = summarize(outcomes)
    assert s["evaluable"] == 2 and s["passed"] == 1 and s["tgc"] == 50.0
    text = render(outcomes, run_id="smoke-001")
    assert "**1/2**" in text
    assert "`t2`" in text and "`t3`" in text and "boom" in text


# -- cli ---------------------------------------------------------------

def test_select_ids_unmatched_is_error():
    with pytest.raises(SystemExit, match="no task"):
        select_ids(["a", "b"], "a,zzz")
    assert select_ids(["a", "b"], "b") == ["b"]


def test_next_run_id_sequences_per_prefix(tmp_path):
    (tmp_path / "test_normal-001").mkdir()
    (tmp_path / "smoke-003").mkdir()
    assert next_run_id(tmp_path, "test_normal") == "test_normal-002"
    assert next_run_id(tmp_path, "smoke") == "smoke-004"


# -- appworld seams (need the package installed) -----------------------

appworld_present = importlib.util.find_spec("appworld") is not None


@pytest.mark.skipif(not appworld_present, reason="appworld not installed")
def test_appworld_interfaces():
    from appworld import AppWorld, load_task_ids  # noqa: F401

    assert callable(load_task_ids)
    import inspect

    params = inspect.signature(AppWorld.__init__).parameters
    for needed in ("task_id", "experiment_name", "remote_apis_url"):
        assert needed in params, f"AppWorld.__init__ lost {needed!r}"
