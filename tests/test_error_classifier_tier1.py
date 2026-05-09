"""Tier 1 recovery classification tests for provider/API failures."""

from __future__ import annotations

import json
from types import SimpleNamespace

from surogates.harness.error_classifier import (
    FailoverReason,
    classify_api_error,
)


def _exc(message: str, status: int | None = None, body: dict | None = None) -> Exception:
    exc = Exception(message)
    if status is not None:
        exc.status_code = status  # type: ignore[attr-defined]
        exc.response = SimpleNamespace(status_code=status)  # type: ignore[attr-defined]
    if body is not None:
        exc.body = body  # type: ignore[attr-defined]
    return exc


def test_anthropic_oauth_long_context_beta_forbidden_retries_without_compression() -> None:
    classified = classify_api_error(
        _exc("The long context beta is not yet available for this subscription.", 400),
        provider="anthropic",
        model="claude-sonnet-4",
    )

    assert classified.reason == FailoverReason.oauth_long_context_beta_forbidden
    assert classified.retryable is True
    assert classified.should_compress is False


def test_llama_cpp_grammar_pattern_rejection_is_retryable_format_recovery() -> None:
    classified = classify_api_error(
        _exc(
            "Unable to generate parser for this template: json-schema-to-grammar failed",
            400,
        ),
        provider="llama.cpp",
    )

    assert classified.reason == FailoverReason.llama_cpp_grammar_pattern
    assert classified.retryable is True
    assert classified.should_compress is False


def test_402_disambiguates_transient_usage_cap_from_billing() -> None:
    transient = classify_api_error(_exc("Usage limit exceeded, try again in 5 minutes", 402))
    billing = classify_api_error(_exc("Insufficient credits. Please top up your credits.", 402))

    assert transient.reason == FailoverReason.rate_limit
    assert transient.retryable is True
    assert transient.should_rotate_credential is True
    assert billing.reason == FailoverReason.billing
    assert billing.retryable is False
    assert billing.should_rotate_credential is True


def test_ssl_transient_alert_is_timeout_not_context_compression() -> None:
    classified = classify_api_error(
        _exc("SSL error: TLSV1_ALERT_INTERNAL_ERROR"),
        approx_tokens=180_000,
        context_length=200_000,
    )

    assert classified.reason == FailoverReason.timeout
    assert classified.retryable is True
    assert classified.should_compress is False


def test_server_disconnect_on_large_session_is_context_overflow() -> None:
    classified = classify_api_error(
        _exc("peer closed connection without sending complete message body"),
        approx_tokens=180_000,
        context_length=200_000,
        num_messages=250,
    )

    assert classified.reason == FailoverReason.context_overflow
    assert classified.should_compress is True


def test_anthropic_bare_400_large_session_is_context_overflow() -> None:
    classified = classify_api_error(
        _exc("Bad Request", 400),
        provider="anthropic",
        approx_tokens=180_000,
        context_length=200_000,
        num_messages=250,
    )

    assert classified.reason == FailoverReason.context_overflow
    assert classified.should_compress is True


def test_openrouter_metadata_raw_nested_error_is_parsed() -> None:
    body = {
        "error": {
            "message": "Provider returned error",
            "metadata": {
                "raw": json.dumps(
                    {"error": {"message": "maximum context length exceeded"}}
                )
            },
        }
    }

    classified = classify_api_error(_exc("Provider returned error", 400, body))

    assert classified.reason == FailoverReason.context_overflow
    assert classified.should_compress is True


def test_bedrock_and_alibaba_429_patterns_are_rate_limits() -> None:
    bedrock = classify_api_error(_exc("ThrottlingException: too many requests", 429))
    alibaba = classify_api_error(_exc("ServiceQuotaExceededException: rate increased too quickly", 429))

    assert bedrock.reason == FailoverReason.rate_limit
    assert bedrock.retryable is True
    assert alibaba.reason == FailoverReason.rate_limit
    assert alibaba.retryable is True
