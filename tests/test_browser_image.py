"""Static checks for the browser image customizations."""

from __future__ import annotations

from pathlib import Path


def test_browser_image_exposes_no_remote_view_of_the_desktop() -> None:
    """Nothing in the image may serve the X display to a remote viewer.

    The shell streams the page over CDP, which is scoped to a tab; a desktop
    view is not. neko stays off because its WebRTC media path needs UDP/TURN
    the cluster cannot provide, and x11vnc/websockify were removed with the
    live view. Re-adding either would silently restore a surface where a
    viewer holding the control lease drives a machine rather than a page, so
    this asserts their absence rather than trusting review to notice.
    """

    dockerfile = Path("images/browser/Dockerfile").read_text()
    assert "autostart=false" in dockerfile
    assert "/etc/supervisor/conf.d/services/neko.conf" in dockerfile
    assert "x11vnc" not in dockerfile
    assert "websockify" not in dockerfile


def test_browser_image_keeps_the_profile_sync_dependency() -> None:
    """zstd compresses captured login profiles; the image is useless without it."""

    dockerfile = Path("images/browser/Dockerfile").read_text()
    assert "zstd" in dockerfile
