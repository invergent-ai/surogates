"""Answer-key derivation from task_scores fixtures."""
import json

import pytest

from dabbench import answers
from dabbench.answers import build_key, load_key


@pytest.fixture
def fake_snapshot(tmp_path, monkeypatch):
    root = tmp_path / "snapshot"
    (root / "data" / "task_scores").mkdir(parents=True)
    (root / "data" / "tasks").mkdir(parents=True)

    def row(sub, task, score, answer):
        return json.dumps({
            "submission_id": sub, "task_id": task,
            "score": score, "level": "easy", "agent_answer": answer,
        })

    (root / "data" / "task_scores" / "a.jsonl").write_text("\n".join([
        row("a", "1", True, "42"),
        row("a", "2", True, "NL"),
        row("a", "3", False, "wrong"),
    ]))
    (root / "data" / "task_scores" / "b.jsonl").write_text("\n".join([
        row("b", "1", True, "42.00"),
        row("b", "1", True, "42.00"),  # duplicate rows in one submission
        row("b", "2", True, "NL"),
        "not json at all",
    ]))
    (root / "data" / "tasks" / "all.jsonl").write_text("\n".join(
        json.dumps({"task_id": t, "question": "q", "guidelines": "g",
                    "level": "easy", "answer": ""})
        for t in ("1", "2", "3")
    ))
    (root / "data" / "tasks" / "dev.jsonl").write_text(json.dumps({
        "task_id": "2", "question": "q", "guidelines": "g",
        "level": "easy", "answer": "GROUND-TRUTH-NL",
    }))
    monkeypatch.setattr(answers, "KEY_PATH", tmp_path / "answer_key.json")
    return root


def test_build_key_takes_modal_answer_and_dev_overrides(fake_snapshot):
    key = build_key(snapshot_dir=str(fake_snapshot))
    assert key["covered"] == 2
    assert key["total"] == 3
    assert key["uncovered_task_ids"] == ["3"]
    # "42.00" has 2 votes vs "42" with 1 -> modal form wins.
    assert key["entries"]["1"]["answer"] == "42.00"
    assert key["entries"]["1"]["distinct_accepted"] == 2
    # Public dev ground truth overrides the derived entry.
    assert key["entries"]["2"]["answer"] == "GROUND-TRUTH-NL"
    assert key["entries"]["2"]["source"] == "upstream-dev"


def test_load_key_roundtrip(fake_snapshot):
    build_key(snapshot_dir=str(fake_snapshot))
    key = load_key()
    assert key["entries"]["1"]["answer"] == "42.00"


def test_load_key_missing_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(answers, "KEY_PATH", tmp_path / "nope.json")
    with pytest.raises(SystemExit, match="build-key"):
        load_key()
