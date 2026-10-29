"""``surogates rehearsal`` serves the world and the internal API from one app."""

from __future__ import annotations

from surogates.cli.main import COMMANDS, build_parser
from surogates.config import RehearsalSettings
from surogates.rehearsal.app import create_app


def test_settings_read_the_environment(monkeypatch):
    monkeypatch.setenv("SUROGATES_REHEARSAL_PORT", "8104")
    monkeypatch.setenv("SUROGATES_REHEARSAL_URL", "http://127.0.0.1:8104")
    settings = RehearsalSettings()
    assert settings.port == 8104 and settings.url == "http://127.0.0.1:8104"


def test_app_mounts_both_surfaces():
    app = create_app(settings=None, session_factory=None, vault=None, http=None)
    paths = {route.path for route in app.routes}
    assert "/internal/runs" in paths
    assert "/w/{phone_number_id}/whatsapp/{version}/{pnid}/messages" in paths
    assert "/health" in paths


def test_cli_has_the_command():
    assert "rehearsal" in COMMANDS
    assert build_parser().parse_args(["rehearsal"]).command == "rehearsal"
