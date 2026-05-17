"""Verify the browser tool composes prefixed keys and source refs."""

from surogates.tools.builtin.browser import (
    build_browser_session_source_ref,
    build_browser_screenshot_key,
)


def test_source_ref_without_prefix():
    ref = build_browser_session_source_ref(
        storage_bucket="b",
        storage_key_prefix="",
        session_id="s-1",
    )
    assert ref == "s3://b/sessions/s-1"


def test_source_ref_with_prefix():
    ref = build_browser_session_source_ref(
        storage_bucket="b",
        storage_key_prefix="p-1/a-1",
        session_id="s-1",
    )
    assert ref == "s3://b/p-1/a-1/sessions/s-1"


def test_screenshot_key_without_prefix():
    key = build_browser_screenshot_key(
        storage_key_prefix="",
        session_id="s-1",
        relative_path="browser-screenshots/x.png",
    )
    assert key == "sessions/s-1/browser-screenshots/x.png"


def test_screenshot_key_with_prefix():
    key = build_browser_screenshot_key(
        storage_key_prefix="p-1/a-1",
        session_id="s-1",
        relative_path="browser-screenshots/x.png",
    )
    assert key == "p-1/a-1/sessions/s-1/browser-screenshots/x.png"


def test_screenshot_key_strips_leading_slash_on_relative_path():
    key = build_browser_screenshot_key(
        storage_key_prefix="p-1/a-1",
        session_id="s-1",
        relative_path="/leading/slash.png",
    )
    assert key == "p-1/a-1/sessions/s-1/leading/slash.png"
