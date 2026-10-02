"""Which events left past the cursor give a wake work to do."""

from types import SimpleNamespace

from surogates.harness.loop_pending import _actionable_pending_events
from surogates.session.events import EventType


def event(id_: int, type_: EventType) -> SimpleNamespace:
    return SimpleNamespace(id=id_, type=type_)


def test_device_wait_events_give_a_wake_no_work():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.DEVICE_RESUMED)]
    assert _actionable_pending_events(events, cursor=4) == []


def test_a_user_message_still_does():
    events = [event(5, EventType.DEVICE_WAITING), event(6, EventType.USER_MESSAGE)]
    assert [e.id for e in _actionable_pending_events(events, cursor=4)] == [6]
