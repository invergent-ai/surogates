"""Verify the workspace source ref includes the storage key prefix."""

from surogates.harness.tool_exec import build_workspace_source_ref


def test_source_ref_without_prefix():
    assert (
        build_workspace_source_ref(
            storage_bucket="b",
            storage_key_prefix="",
            workspace_prefix="sessions/root/",
        )
        == "s3://b/sessions/root/"
    )


def test_source_ref_with_prefix():
    assert (
        build_workspace_source_ref(
            storage_bucket="b",
            storage_key_prefix="p/a",
            workspace_prefix="sessions/root/",
        )
        == "s3://b/p/a/sessions/root/"
    )


def test_source_ref_strips_trailing_slash_from_prefix():
    """A trailing slash on the storage key prefix must not produce a double-slash."""
    assert (
        build_workspace_source_ref(
            storage_bucket="b",
            storage_key_prefix="p/a/",
            workspace_prefix="sessions/root/",
        )
        == "s3://b/p/a/sessions/root/"
    )
