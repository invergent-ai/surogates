"""Tests for :mod:`surogates.harness.error_classify`."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from surogates.harness.error_classify import (
    ErrorInfo,
    classify_harness_error,
)


# ---------------------------------------------------------------------------
# HTTP status code routing
# ---------------------------------------------------------------------------


def _with_status(exc: Exception, status: int) -> Exception:
    """Attach a ``status_code`` attribute to mimic SDK error objects."""
    exc.status_code = status  # type: ignore[attr-defined]
    return exc


class TestStatusCodeRouting:
    def test_401_is_auth_failed(self) -> None:
        info = classify_harness_error(_with_status(Exception("unauthorized"), 401))
        assert info.category == "auth_failed"
        assert info.retryable is False
        assert info.title == "Authentication with the model provider failed."

    def test_403_is_auth_failed(self) -> None:
        info = classify_harness_error(_with_status(Exception("forbidden"), 403))
        assert info.category == "auth_failed"
        assert info.retryable is False

    def test_429_is_rate_limit(self) -> None:
        info = classify_harness_error(_with_status(Exception("slow down"), 429))
        assert info.category == "rate_limit"
        assert info.retryable is True

    def test_429_with_long_context_is_context_overflow(self) -> None:
        """Anthropic Sonnet 1M tier gate uses HTTP 429 but is not a rate limit."""
        exc = _with_status(
            Exception("Extra usage is required for long context requests"),
            429,
        )
        info = classify_harness_error(exc)
        assert info.category == "context_overflow"
        assert info.retryable is False

    def test_413_is_context_overflow(self) -> None:
        info = classify_harness_error(_with_status(Exception("payload too large"), 413))
        assert info.category == "context_overflow"
        assert info.retryable is False

    def test_500_is_provider_error(self) -> None:
        info = classify_harness_error(_with_status(Exception("internal"), 500))
        assert info.category == "provider_error"
        assert info.retryable is True

    def test_502_is_provider_error(self) -> None:
        info = classify_harness_error(_with_status(Exception("bad gateway"), 502))
        assert info.category == "provider_error"

    def test_status_from_response_attr(self) -> None:
        exc = Exception("oops")
        exc.response = SimpleNamespace(status_code=503)  # type: ignore[attr-defined]
        info = classify_harness_error(exc)
        assert info.category == "provider_error"


# ---------------------------------------------------------------------------
# SDK exception class name routing
# ---------------------------------------------------------------------------


def _make(type_name: str, module: str, message: str) -> Exception:
    """Fabricate an exception with a specific ``type(exc).__name__`` and module.

    We can't import the real openai / anthropic / boto classes as test
    dependencies, and :func:`classify_harness_error` matches on class
    name and module string precisely so it doesn't need them either.
    """
    cls = type(type_name, (Exception,), {"__module__": module})
    return cls(message)


class TestSDKExceptionNames:
    def test_openai_rate_limit_error(self) -> None:
        info = classify_harness_error(_make("RateLimitError", "openai", "slow down"))
        assert info.category == "rate_limit"

    def test_openai_authentication_error(self) -> None:
        info = classify_harness_error(_make("AuthenticationError", "openai", "bad key"))
        assert info.category == "auth_failed"
        assert info.retryable is False

    def test_anthropic_permission_denied(self) -> None:
        info = classify_harness_error(
            _make("PermissionDeniedError", "anthropic", "forbidden")
        )
        assert info.category == "auth_failed"

    def test_api_error_without_status_is_provider(self) -> None:
        info = classify_harness_error(_make("APIError", "openai", "Provider returned error"))
        assert info.category == "provider_error"
        assert info.retryable is True

    def test_api_timeout_error(self) -> None:
        info = classify_harness_error(
            _make("APITimeoutError", "openai", "took too long"),
        )
        assert info.category == "timeout"


# ---------------------------------------------------------------------------
# Context-overflow phrase detection
# ---------------------------------------------------------------------------


class TestContextOverflow:
    def test_context_length_phrase(self) -> None:
        info = classify_harness_error(
            Exception("context length exceeded the limit"),
        )
        assert info.category == "context_overflow"
        assert info.retryable is False

    def test_token_limit_phrase(self) -> None:
        info = classify_harness_error(Exception("token limit reached"))
        assert info.category == "context_overflow"

    def test_prompt_too_long_phrase(self) -> None:
        info = classify_harness_error(Exception("prompt is too long"))
        assert info.category == "context_overflow"

    def test_request_entity_too_large_phrase(self) -> None:
        """Some backends return 400 with 'request entity too large' in the
        body rather than HTTP 413 — treat as context overflow."""
        info = classify_harness_error(
            Exception("request entity too large"),
        )
        assert info.category == "context_overflow"


# ---------------------------------------------------------------------------
# Invalid / empty response wrappers (the exact failure mode from the
# reported session).
# ---------------------------------------------------------------------------


class TestInvalidResponse:
    def test_invalid_response_none_or_no_choices(self) -> None:
        """The exact ValueError raised by llm_call.py when a provider
        returns an empty stream."""
        exc = ValueError("Invalid LLM response: response is None or has no choices")
        info = classify_harness_error(exc)
        assert info.category == "invalid_response"
        assert info.retryable is True
        assert info.title == "The model returned an empty or malformed response."

    def test_llm_returned_empty_response(self) -> None:
        info = classify_harness_error(ValueError("LLM returned empty response"))
        assert info.category == "invalid_response"

    def test_generic_value_error_is_unknown(self) -> None:
        """ValueError that isn't an invalid-response wrapper falls through."""
        info = classify_harness_error(ValueError("something else entirely"))
        assert info.category == "unknown"


# ---------------------------------------------------------------------------
# Network & timeout
# ---------------------------------------------------------------------------


class TestNetworkAndTimeout:
    def test_asyncio_timeout(self) -> None:
        info = classify_harness_error(asyncio.TimeoutError())
        assert info.category == "timeout"
        assert info.retryable is True

    def test_builtin_timeout_error(self) -> None:
        info = classify_harness_error(TimeoutError("timed out"))
        assert info.category == "timeout"

    def test_connection_error(self) -> None:
        info = classify_harness_error(ConnectionError("refused"))
        assert info.category == "network"
        assert info.retryable is True

    def test_httpx_read_error(self) -> None:
        info = classify_harness_error(_make("ReadError", "httpx", "peer closed"))
        assert info.category == "network"

    def test_httpx_module_default(self) -> None:
        """Any httpx-namespace exception should be classified as network."""
        info = classify_harness_error(_make("MysteryHttpxError", "httpx", "?"))
        assert info.category == "network"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class TestDatabase:
    def test_sqlalchemy_operational_error(self) -> None:
        info = classify_harness_error(
            _make("OperationalError", "sqlalchemy.exc", "connection refused"),
        )
        assert info.category == "database_error"
        assert info.retryable is True

    def test_asyncpg_error(self) -> None:
        info = classify_harness_error(
            _make("ConnectionDoesNotExistError", "asyncpg.exceptions", "lost"),
        )
        assert info.category == "database_error"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class TestStorage:
    def test_botocore_client_error(self) -> None:
        info = classify_harness_error(
            _make("ClientError", "botocore.exceptions", "AccessDenied"),
        )
        assert info.category == "storage_error"

    def test_endpoint_connection_error_by_name(self) -> None:
        info = classify_harness_error(
            _make("EndpointConnectionError", "something.else", "?"),
        )
        assert info.category == "storage_error"

    def test_aioboto3_error(self) -> None:
        info = classify_harness_error(
            _make("S3UploadError", "aioboto3", "bucket gone"),
        )
        assert info.category == "storage_error"


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


class TestGovernance:
    def test_policy_denied_error(self) -> None:
        info = classify_harness_error(
            _make("PolicyDeniedError", "surogates.governance", "blocked"),
        )
        assert info.category == "governance_denied"
        assert info.retryable is False

    def test_governance_error(self) -> None:
        info = classify_harness_error(
            _make("GovernanceError", "surogates.governance", "blocked"),
        )
        assert info.category == "governance_denied"

    def test_class_name_with_policy_denied(self) -> None:
        info = classify_harness_error(
            _make("CustomPolicyDeniedError", "some.app", "blocked"),
        )
        assert info.category == "governance_denied"


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


class TestUnknownFallback:
    def test_runtime_error_is_unknown(self) -> None:
        info = classify_harness_error(RuntimeError("weird"))
        assert info.category == "unknown"
        assert info.retryable is True
        assert info.detail == "weird"

    def test_empty_message_uses_type_name(self) -> None:
        info = classify_harness_error(RuntimeError(""))
        assert info.category == "unknown"
        assert info.detail == "RuntimeError"


# ---------------------------------------------------------------------------
# Detail trimming
# ---------------------------------------------------------------------------


class TestDetailTrimming:
    def test_multiline_takes_first_line(self) -> None:
        exc = Exception("first line\nsecond line\nthird line")
        info = classify_harness_error(exc)
        assert info.detail == "first line"

    def test_long_message_trimmed_with_ellipsis(self) -> None:
        # 600 chars of "word " repeated, no newlines.
        msg = ("word " * 120).strip()
        info = classify_harness_error(Exception(msg))
        assert len(info.detail) <= 501  # 500 + ellipsis
        assert info.detail.endswith("…")
        # Should cut on a whitespace boundary (no mid-word cut).
        assert not info.detail[:-1].endswith(" ")

    def test_long_single_token_hard_cut(self) -> None:
        # No whitespace for 600 chars -- falls back to hard cut.
        msg = "x" * 600
        info = classify_harness_error(Exception(msg))
        assert len(info.detail) <= 501
        assert info.detail.endswith("…")


# ---------------------------------------------------------------------------
# ErrorInfo immutability
# ---------------------------------------------------------------------------


class TestErrorInfoShape:
    def test_is_frozen_dataclass(self) -> None:
        info = ErrorInfo(
            category="unknown",
            title="x",
            detail="y",
            retryable=True,
        )
        import dataclasses
        assert dataclasses.is_dataclass(info)
        # Frozen — mutation raises.
        import pytest
        with pytest.raises(dataclasses.FrozenInstanceError):
            info.category = "other"  # type: ignore[misc]
