"""Static checks for the browser image customizations."""

from __future__ import annotations

from pathlib import Path


def test_browser_image_serves_live_view_over_websockify() -> None:
    """neko stays off: its WebRTC media path needs UDP/TURN the cluster
    cannot provide, so the live view is x11vnc + websockify on :8080."""
    dockerfile = Path("images/browser/Dockerfile").read_text()
    assert "autostart=false" in dockerfile
    assert "/etc/supervisor/conf.d/services/neko.conf" in dockerfile
    assert "x11vnc" in dockerfile
    assert "websockify" in dockerfile
