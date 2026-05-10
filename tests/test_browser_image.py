"""Static checks for the browser image customizations."""

from __future__ import annotations

from pathlib import Path


def test_browser_image_autostarts_neko_live_view() -> None:
    dockerfile = Path("images/browser/Dockerfile").read_text()
    assert "autostart=true" in dockerfile
    assert "/etc/supervisor/conf.d/services/neko.conf" in dockerfile
