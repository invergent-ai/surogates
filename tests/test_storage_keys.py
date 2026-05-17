"""Unit tests for the storage key prefix helper."""

from surogates.storage.keys import prefixed


def test_prefixed_with_empty_prefix_returns_key_unchanged():
    assert prefixed("sessions/abc/foo.json", "") == "sessions/abc/foo.json"


def test_prefixed_with_prefix_prepends_with_single_slash():
    assert prefixed("sessions/abc/foo.json", "p-1/a-1") == "p-1/a-1/sessions/abc/foo.json"


def test_prefixed_strips_trailing_slash_from_prefix():
    assert prefixed("foo", "p-1/a-1/") == "p-1/a-1/foo"


def test_prefixed_strips_leading_slash_from_key():
    assert prefixed("/foo", "p-1/a-1") == "p-1/a-1/foo"


def test_prefixed_handles_both_empty():
    assert prefixed("", "") == ""


def test_prefixed_empty_key_with_prefix_returns_prefix():
    assert prefixed("", "p-1/a-1") == "p-1/a-1"


def test_prefixed_preserves_trailing_slash_on_key():
    """Workspace prefixes carry a trailing slash; helper must preserve it."""
    assert prefixed("sessions/s-1/", "p-1/a-1") == "p-1/a-1/sessions/s-1/"


def test_prefixed_with_empty_prefix_preserves_trailing_slash():
    assert prefixed("sessions/s-1/", "") == "sessions/s-1/"
