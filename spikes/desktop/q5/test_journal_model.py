from __future__ import annotations

import pytest

from spikes.desktop.q5.journal_model import Api, Journal, Laptop, Lease, Worker, digest

REQ = {"op": "write", "path": "a.txt", "bytes": "x"}
OP = "call_1:1"


def _world(online: bool = True):
    journal, laptop, lease = Journal(), Laptop(), Lease()
    api = Api(journal, laptop, online=online)
    return journal, laptop, api, lease, Worker(journal, api, lease, my_token=lease.token)


def test_happy_path_runs_once() -> None:
    _, laptop, _, _, worker = _world()
    worker.dispatch(OP, REQ)
    assert worker.commit(OP, "call_1") == {"ok": True, "op": OP}
    assert laptop.effects == {OP: 1}


def test_lost_nudge_is_recovered_by_heartbeat_reconcile() -> None:
    _, laptop, api, _, worker = _world(online=False)
    worker.dispatch(OP, REQ)
    assert laptop.effects == {}
    api.online = True
    api.reconcile()
    assert worker.commit(OP, "call_1")["ok"] is True
    assert laptop.effects == {OP: 1}


def test_crash_before_start_runs_exactly_once_after_reconnect() -> None:
    _, laptop, api, _, worker = _world()
    laptop.crash_at = "after_received"
    worker.dispatch(OP, REQ)
    api.online = True
    api.reconcile()
    assert laptop.effects == {OP: 1}
    assert worker.commit(OP, "call_1")["ok"] is True


@pytest.mark.parametrize("point, expected_effects", [("after_started", 0), ("after_effect", 1)])
def test_crash_between_started_and_result_reports_interrupted(point: str, expected_effects: int) -> None:
    _, laptop, api, _, worker = _world()
    laptop.crash_at = point
    worker.dispatch(OP, REQ)
    api.online = True
    api.reconcile()
    assert worker.commit(OP, "call_1") == {"interrupted": True, "outcome": "unknown"}
    assert laptop.effects.get(OP, 0) == expected_effects


def test_crash_after_result_returns_stored_result() -> None:
    _, laptop, api, _, worker = _world()
    laptop.crash_at = "after_result"
    worker.dispatch(OP, REQ)
    api.online = True
    api.reconcile()
    assert worker.commit(OP, "call_1") == {"ok": True, "op": OP}
    assert laptop.effects == {OP: 1}


def test_new_worker_after_result_commit_reads_result_without_redispatch() -> None:
    journal, laptop, api, lease, old = _world()
    old.dispatch(OP, REQ)                  # laptop ran it and the api committed the result
    lease.token = "t2"                     # old worker died; its lease was stolen
    new = Worker(journal, api, lease, my_token="t2")
    new.dispatch(OP, REQ)                  # recovery resumes the recorded sequence
    assert new.commit(OP, "call_1")["ok"] is True
    assert laptop.effects == {OP: 1}


def test_stale_worker_can_neither_dispatch_nor_commit() -> None:
    journal, _, api, lease, old = _world(online=False)
    old.dispatch(OP, REQ)
    lease.token = "t2"
    with pytest.raises(PermissionError):
        old.dispatch("call_1:2", REQ)
    api.online = True
    api.reconcile()
    with pytest.raises(PermissionError):
        old.commit(OP, "call_1")


def test_same_id_with_changed_payload_is_a_protocol_error() -> None:
    _, _, _, _, worker = _world()
    worker.dispatch(OP, REQ)
    with pytest.raises(ValueError):
        worker.dispatch(OP, {**REQ, "bytes": "y"})


def test_cancel_while_offline_never_runs() -> None:
    journal, laptop, api, _, worker = _world(online=False)
    worker.dispatch(OP, REQ)
    journal.cancel(OP)
    api.online = True
    api.reconcile()
    assert laptop.effects == {}
    assert laptop.records[OP]["state"] == "cancelled"


def test_delayed_retry_after_payload_reclaim_does_not_rerun() -> None:
    journal, laptop, api, _, worker = _world()
    worker.dispatch(OP, REQ)
    worker.commit(OP, "call_1")
    laptop.compact(OP)
    assert laptop.handle(OP, REQ) == {"completed": True, "payload": "reclaimed"}
    assert laptop.effects == {OP: 1}
    assert laptop.handle(OP, {**REQ, "bytes": "y"}) == {"error": "protocol: digest changed"}
    assert digest(REQ) == journal.rows[OP]["digest"]
