from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.loop import _should_notify_parent_on_completion


def test_scheduled_child_sessions_do_not_notify_parent_as_workers() -> None:
    session = SimpleNamespace(parent_id=uuid4(), channel="scheduled")

    assert _should_notify_parent_on_completion(session) is False


def test_worker_child_sessions_notify_parent_on_completion() -> None:
    session = SimpleNamespace(parent_id=uuid4(), channel="worker")

    assert _should_notify_parent_on_completion(session) is True
