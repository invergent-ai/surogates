"""The interrupt listener parses reasons tolerantly — never drops a signal."""

from surogates.orchestrator.dispatcher import _parse_interrupt_reason


def test_canonical_json_reason():
    assert _parse_interrupt_reason('{"reason": "channel_stop"}') == "channel_stop"
    assert _parse_interrupt_reason(b'{"reason": "session deleted"}') == "session deleted"


def test_bare_string_is_the_reason():
    # Publishers that send just the reason (mission cascade, older callers) must
    # not crash the listener — the whole point of this fix.
    assert _parse_interrupt_reason("channel_stop") == "channel_stop"
    assert _parse_interrupt_reason(b"mission_cancel_cascade") == "mission_cancel_cascade"


def test_empty_or_none_defaults():
    assert _parse_interrupt_reason(None) == "interrupted"
    assert _parse_interrupt_reason("") == "interrupted"
    assert _parse_interrupt_reason(b"") == "interrupted"


def test_json_without_reason_defaults():
    assert _parse_interrupt_reason('{"foo": 1}') == "interrupted"
