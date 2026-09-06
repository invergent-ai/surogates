"""Split invariants and parquet-value normalization, offline."""
import json
import pathlib

from galbench.dataset import _dictish, _listish, frozen_split, make_split

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "scenario_ids.json"


def test_listish_and_dictish_accept_all_writer_shapes():
    assert _listish(["a"]) == ["a"]
    assert _listish('["a", "b"]') == ["a", "b"]

    class FakeArray:
        def tolist(self):
            return ["x"]

    assert _listish(FakeArray()) == ["x"]
    assert _dictish('{"a": 1}') == {"a": 1}
    assert _dictish({"a": 1}) == {"a": 1}


def test_make_split_deterministic_per_domain_quota():
    ids = [f"{d}-{i:03d}" for d in ("banking", "telecom") for i in range(100)]
    dev1, holdout1 = make_split(ids)
    dev2, _ = make_split(ids)
    assert dev1 == dev2
    assert len(dev1) == 40 and len(holdout1) == 160
    assert sum(1 for s in dev1 if s.startswith("banking")) == 20


def test_frozen_split_invariants():
    split = frozen_split()
    assert len(split["dev"]) == 100
    assert len(split["holdout"]) == 400
    assert not set(split["dev"]) & set(split["holdout"])


def test_frozen_split_matches_generator():
    ids = json.loads(FIXTURE.read_text())
    dev, holdout = make_split(ids)
    split = frozen_split()
    assert dev == split["dev"]
    assert holdout == split["holdout"]
