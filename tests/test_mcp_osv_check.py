"""Tests for MCP stdio package malware checks."""

from __future__ import annotations

import json

from surogates.tools.mcp.osv_check import check_package_for_malware


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_npx_package_with_malware_advisory_is_blocked(monkeypatch) -> None:
    def fake_urlopen(req, timeout):  # noqa: ANN001
        return _FakeResponse({
            "vulns": [{"id": "MAL-2026-1", "summary": "malicious package"}],
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = check_package_for_malware("npx", ["@scope/example@1.2.3"])

    assert result is not None
    assert "BLOCKED" in result
    assert "@scope/example" in result
    assert "MAL-2026-1" in result


def test_regular_cve_does_not_block(monkeypatch) -> None:
    def fake_urlopen(req, timeout):  # noqa: ANN001
        return _FakeResponse({
            "vulns": [{"id": "GHSA-xxxx", "summary": "ordinary vuln"}],
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert check_package_for_malware("npx", ["example"]) is None


def test_osv_network_error_fails_open(monkeypatch) -> None:
    def fake_urlopen(req, timeout):  # noqa: ANN001
        raise TimeoutError("offline")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    assert check_package_for_malware("npx", ["example"]) is None


def test_non_package_command_is_skipped() -> None:
    assert check_package_for_malware("python", ["server.py"]) is None
