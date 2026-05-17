"""Verify session workspace keys are scoped under storage_key_prefix."""

from surogates.storage.tenant import (
    prefixed_session_workspace_key,
    prefixed_session_workspace_prefix,
    storage_key_prefix,
)


def test_storage_key_prefix_returns_empty_for_none_config():
    assert storage_key_prefix(None) == ""


def test_storage_key_prefix_returns_empty_for_missing_key():
    assert storage_key_prefix({}) == ""


def test_storage_key_prefix_returns_value_when_set():
    assert storage_key_prefix({"storage_key_prefix": "p-1/a-1"}) == "p-1/a-1"


def test_storage_key_prefix_coerces_none_to_empty():
    """Older sessions may stamp the field as None — normalise to ''."""
    assert storage_key_prefix({"storage_key_prefix": None}) == ""


def test_prefixed_session_workspace_prefix_without_storage_prefix():
    assert prefixed_session_workspace_prefix({}, "s-1") == "s-1/"


def test_prefixed_session_workspace_prefix_with_storage_prefix():
    assert (
        prefixed_session_workspace_prefix({"storage_key_prefix": "p-1/a-1"}, "s-1")
        == "p-1/a-1/s-1/"
    )


def test_prefixed_session_workspace_key_without_storage_prefix():
    assert (
        prefixed_session_workspace_key({}, "s-1", "docs/readme.md")
        == "s-1/docs/readme.md"
    )


def test_prefixed_session_workspace_key_with_storage_prefix():
    assert (
        prefixed_session_workspace_key(
            {"storage_key_prefix": "p-1/a-1"},
            "s-1",
            "docs/readme.md",
        )
        == "p-1/a-1/s-1/docs/readme.md"
    )


def test_prefixed_session_workspace_key_empty_key():
    """Empty key collapses to the workspace prefix without a trailing slash artefact."""
    assert (
        prefixed_session_workspace_key(
            {"storage_key_prefix": "p-1/a-1"}, "s-1", "",
        )
        == "p-1/a-1/s-1/"
    )
