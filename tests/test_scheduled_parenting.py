from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.loop import _should_notify_parent_on_completion


def test_scheduled_child_sessions_do_not_notify_parent_as_workers() -> None:
    session = SimpleNamespace(parent_id=uuid4(), channel="scheduled")

    assert _should_notify_parent_on_completion(session) is False


def test_worker_child_sessions_notify_parent_on_completion() -> None:
    session = SimpleNamespace(parent_id=uuid4(), channel="worker")

    assert _should_notify_parent_on_completion(session) is True


def test_scheduled_run_marker_does_not_notify_parent_even_if_channel_drifted() -> None:
    # A scheduled run is identified by channel=="scheduled" OR a
    # scheduled_session_id config marker. Whichever way it is recognised, it
    # must never wake the parent as sub-agent work — otherwise a loop run whose
    # channel drifted would both surface a loop.result and re-enqueue the
    # parent.
    session = SimpleNamespace(
        parent_id=uuid4(),
        channel="api",
        config={"scheduled_session_id": str(uuid4())},
    )

    assert _should_notify_parent_on_completion(session) is False
