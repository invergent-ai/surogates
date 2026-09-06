"""Task parsing and split invariants, all offline."""
import json
import pathlib

from dabbench.dataset import _to_task, frozen_split, make_split

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "task_levels.json"


def test_to_task_normalizes():
    task = _to_task({
        "task_id": 5,
        "question": "Which country?",
        "guidelines": "Answer must be a country code",
        "level": " Easy ",
        "answer": "NL",
    })
    assert task.task_id == "5"
    assert task.level == "easy"
    assert task.answer == "NL"


def test_make_split_deterministic_and_stratified():
    rows = [(f"e{i}", "easy") for i in range(72)]
    rows += [(f"h{i}", "hard") for i in range(378)]
    dev1, holdout1 = make_split(rows)
    dev2, _ = make_split(rows)
    assert dev1 == dev2
    assert len(dev1) == 100 and len(holdout1) == 350
    assert not set(dev1) & set(holdout1)
    assert sum(1 for t in dev1 if t.startswith("e")) == 16  # 72 * 100/450


def test_frozen_split_invariants():
    split = frozen_split()
    assert len(split["dev"]) == 100
    assert len(split["holdout"]) == 350
    assert not set(split["dev"]) & set(split["holdout"])


def test_frozen_split_matches_generator():
    rows = [tuple(r) for r in json.loads(FIXTURE.read_text())]
    dev, holdout = make_split(rows)
    split = frozen_split()
    assert dev == split["dev"]
    assert holdout == split["holdout"]
