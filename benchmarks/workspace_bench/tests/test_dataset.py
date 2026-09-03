"""Parsing and split invariants, all offline."""
import json

import pytest

from wsbench.dataset import (
    _parse_listish,
    frozen_split,
    make_split,
    row_to_task,
)


def _row(**overrides):
    row = {
        "absolute_id": "3",
        "language": "en",
        "persona": "Backend Developer",
        "task": "Extract the dependencies.",
        "task_diff": "Medium",
        "output_files": '["deps.md"]',
        "rubrics": '["Was deps.md created?", "Does it list 43 deps?"]',
        "rubric_types": '["Basic Evaluation", "Outcome Evaluation"]',
        "file_dep_graph": "[]",
        "data_manifest": '[{"filename": "a.md", "stored_relpath": "data/xx_a.md"}]',
        "tested_capabilities": '["Lineage Tracing"]',
    }
    row.update(overrides)
    return row


def test_parse_listish_accepts_json_and_python_repr():
    assert _parse_listish('["a", "b"]') == ["a", "b"]
    # metadata.json stores Python reprs; the CSV stores JSON. Both load.
    assert _parse_listish("['a', 'b']") == ["a", "b"]
    assert _parse_listish("") == []


def test_parse_listish_rejects_non_list():
    with pytest.raises(ValueError):
        _parse_listish('{"a": 1}')


def test_row_to_task():
    task = row_to_task(_row(), "/snap")
    assert task.task_id == "3"
    assert task.difficulty == "medium"
    assert task.output_files == ("deps.md",)
    assert len(task.rubrics) == 2
    assert task.manifest[0].filename == "a.md"
    assert task.manifest[0].stored_relpath == "data/xx_a.md"
    assert task.local_dir == "/snap/task_lite_clean_en/3"


def test_make_split_deterministic_and_disjoint():
    rows = [
        (str(i), f"P{i % 3}", ["easy", "medium", "hard"][i % 3])
        for i in range(100)
    ]
    dev1, holdout1 = make_split(rows)
    dev2, holdout2 = make_split(rows)
    assert dev1 == dev2 and holdout1 == holdout2
    assert not set(dev1) & set(holdout1)
    assert len(dev1) + len(holdout1) == 100
    assert len(dev1) == 70


def test_make_split_stratifies():
    # 60 easy + 40 hard: each stratum must land close to 70/30 on its own,
    # not just in aggregate.
    rows = [(f"e{i}", "P", "easy") for i in range(60)]
    rows += [(f"h{i}", "P", "hard") for i in range(40)]
    dev, _ = make_split(rows)
    easy_dev = sum(1 for t in dev if t.startswith("e"))
    hard_dev = sum(1 for t in dev if t.startswith("h"))
    assert easy_dev == 42
    assert hard_dev == 28


def test_frozen_split_invariants():
    split = frozen_split()
    dev, holdout = split["dev"], split["holdout"]
    assert len(dev) == 70
    assert len(holdout) == 30
    assert not set(dev) & set(holdout)
    assert all(isinstance(t, str) for t in dev + holdout)


def test_frozen_split_matches_generator_when_csv_available():
    """The committed split must be reproducible from make_split, so a
    regenerated file that silently drifted would fail loudly."""
    import pathlib

    csv_path = pathlib.Path(__file__).parent / "fixtures" / "lite_en_ids.json"
    rows = json.loads(csv_path.read_text())
    dev, holdout = make_split([tuple(r) for r in rows])
    split = frozen_split()
    assert dev == split["dev"]
    assert holdout == split["holdout"]
