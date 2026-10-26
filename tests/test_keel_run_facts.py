"""What a Keel report says of a session's work: its use and its bundle, always within Keel's bounds."""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from uuid import uuid4

import pytest

from surogates.keel.run_facts import (
    BUNDLE_VERSION_CHARS,
    MAX_COUNT,
    bundle_version_in,
    usage_report,
    wake_bundle_version,
    work_usage,
    work_usage_or_none,
)
from surogates.session.events import EventType
from surogates.session.models import Event
from surogates.session.store import TreeUsage

COST_SHAPE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
COST_CHARS = 40


def test_a_bundle_version_is_sent_only_within_keels_bounds():
    assert BUNDLE_VERSION_CHARS == 200
    assert bundle_version_in("v12") == "v12"
    assert bundle_version_in("x" * 200) == "x" * 200
    for refused in ("", "x" * 201, 12, None, ["v1"]):
        assert bundle_version_in(refused) is None


def test_usage_is_reported_within_keels_bounds():
    assert usage_report(TreeUsage(1200, 340, 50, Decimal("0.0123"))) == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_tokens": 50,
        "estimated_cost_usd": "0.0123",
    }
    assert usage_report(TreeUsage(2**63 + 5, -1, 2**64, Decimal("-0.5"))) == {
        "input_tokens": MAX_COUNT,
        "output_tokens": 0,
        "cache_read_tokens": MAX_COUNT,
        "estimated_cost_usd": "0",
    }
    answers = {
        cost: usage_report(TreeUsage(0, 0, 0, Decimal(cost)))["estimated_cost_usd"]
        for cost in (
            "NaN", "sNaN", "Infinity", "-Infinity", "-0", "-0.5",
            "1E+1", "0.000000", "0.0123", "1E+100", "0." + "1" * 60,
        )
    }
    assert answers["NaN"] == answers["sNaN"] == "0"
    assert answers["Infinity"] == answers["-Infinity"] == "0"
    assert answers["-0"] == answers["-0.5"] == "0"
    assert answers["1E+1"] == "10"
    assert answers["0.000000"] == "0.000000"
    assert answers["0.0123"] == "0.0123"
    for cost, answer in answers.items():
        assert COST_SHAPE.match(answer), cost
        assert len(answer) <= COST_CHARS, cost


class _Wakes:
    """A store whose newest wake that records a bundle is *event*, or whose read fails."""

    def __init__(self, data: dict | None = None, *, fails: bool = False) -> None:
        self._data, self._fails = data, fails

    async def last_event(self, session_id, *types, with_key=None, **_):
        if self._fails:
            raise RuntimeError("database down: secret-body")
        if self._data is None:
            return None
        return Event(session_id=session_id, type=EventType.HARNESS_WAKE.value, data=self._data)


async def test_the_bundle_version_never_fails_a_report(caplog):
    sid = uuid4()
    assert await wake_bundle_version(_Wakes({"bundle_version": "v7", "worker_id": "w"}), sid) == "v7"
    assert await wake_bundle_version(_Wakes({"bundle_version": "x" * 201}), sid) is None
    assert await wake_bundle_version(_Wakes({"bundle_version": None}), sid) is None
    assert await wake_bundle_version(_Wakes(None), sid) is None

    with caplog.at_level(logging.WARNING):
        assert await wake_bundle_version(_Wakes(fails=True), sid) is None
    assert str(sid) in caplog.text
    assert "RuntimeError" in caplog.text
    assert "secret-body" not in caplog.text


class _Counters:
    def __init__(self, used: TreeUsage | None = None) -> None:
        self._used = used

    async def session_tree_usage(self, session_id):
        if self._used is None:
            raise RuntimeError("database down: secret-body")
        return self._used


async def test_usage_that_cannot_be_read_is_none_for_an_end(caplog):
    sid = uuid4()
    used = TreeUsage(1200, 340, 50, Decimal("0.0123"))
    assert await work_usage(_Counters(used), sid) == usage_report(used)
    assert await work_usage_or_none(_Counters(used), sid) == usage_report(used)

    with caplog.at_level(logging.WARNING):
        assert await work_usage_or_none(_Counters(), sid) is None
    assert str(sid) in caplog.text
    assert "RuntimeError" in caplog.text
    assert "secret-body" not in caplog.text

    with pytest.raises(RuntimeError):
        await work_usage(_Counters(), sid)
