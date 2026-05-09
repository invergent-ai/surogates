"""Tests for URL SSRF protections."""

from __future__ import annotations

from surogates.tools.utils.url_safety import is_always_blocked_url, is_safe_url


class TestAlwaysBlockedMetadataFloor:
    def test_imds_ipv4_is_always_blocked(self) -> None:
        url = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"

        assert is_always_blocked_url(url) is True
        assert is_safe_url(url) is False

    def test_ecs_task_metadata_is_always_blocked(self) -> None:
        assert is_always_blocked_url("http://169.254.170.2/v2/credentials") is True

    def test_metadata_hostname_is_always_blocked_with_trailing_dot(self) -> None:
        assert is_always_blocked_url("http://metadata.google.internal./computeMetadata/v1") is True

    def test_public_url_is_not_in_always_blocked_floor(self) -> None:
        assert is_always_blocked_url("https://example.com/image.png") is False
