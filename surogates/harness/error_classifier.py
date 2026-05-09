"""Central API error classification for retry, failover, and compression."""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from typing import Any


class FailoverReason(enum.Enum):
    """Provider/API failure reason used by retry and recovery code."""

    auth = "auth"
    auth_permanent = "auth_permanent"
    billing = "billing"
    rate_limit = "rate_limit"
    overloaded = "overloaded"
    server_error = "server_error"
    timeout = "timeout"
    context_overflow = "context_overflow"
    payload_too_large = "payload_too_large"
    image_too_large = "image_too_large"
    model_not_found = "model_not_found"
    provider_policy_blocked = "provider_policy_blocked"
    format_error = "format_error"
    thinking_signature = "thinking_signature"
    long_context_tier = "long_context_tier"
    oauth_long_context_beta_forbidden = "oauth_long_context_beta_forbidden"
    llama_cpp_grammar_pattern = "llama_cpp_grammar_pattern"
    unknown = "unknown"


@dataclass(frozen=True)
class ClassifiedError:
    """Structured classification of an API error with recovery hints."""

    reason: FailoverReason
    status_code: int | None = None
    provider: str | None = None
    model: str | None = None
    message: str = ""
    error_context: dict[str, Any] = field(default_factory=dict)
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False


_BILLING_PATTERNS = (
    "insufficient credits",
    "insufficient_quota",
    "insufficient balance",
    "credit balance",
    "credits have been exhausted",
    "top up your credits",
    "payment required",
    "billing hard limit",
    "exceeded your current quota",
)
_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttled",
    "requests per minute",
    "tokens per minute",
    "requests per day",
    "try again in",
    "please retry after",
    "resource_exhausted",
    "rate increased too quickly",
    "throttlingexception",
    "servicequotaexceededexception",
)
_USAGE_LIMIT_PATTERNS = ("usage limit", "quota", "limit exceeded", "key limit exceeded")
_USAGE_LIMIT_TRANSIENT_SIGNALS = ("try again", "retry", "resets at", "reset in", "wait")
_CONTEXT_OVERFLOW_PATTERNS = (
    "context length",
    "context size",
    "maximum context",
    "context window",
    "too many tokens",
    "prompt is too long",
    "prompt exceeds max length",
    "reduce the length",
    "exceeds the limit",
    "maximum context length exceeded",
    "request entity too large",
)
_MODEL_NOT_FOUND_PATTERNS = ("model not found", "does not exist", "unknown model")
_SSL_TRANSIENT_PATTERNS = (
    "tlsv1_alert_internal_error",
    "ssl",
    "sslerror",
    "tls alert",
    "decryption failed or bad record mac",
)
_SERVER_DISCONNECT_PATTERNS = (
    "peer closed",
    "server disconnected",
    "remote protocol error",
    "connection closed",
    "connection reset",
    "broken pipe",
)
_TRANSPORT_ERROR_TYPES = {
    "ReadTimeout",
    "ConnectTimeout",
    "PoolTimeout",
    "ConnectError",
    "ReadError",
    "RemoteProtocolError",
    "ServerDisconnectedError",
    "ProtocolError",
    "TimeoutError",
}


def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
    approx_tokens: int = 0,
    context_length: int = 200_000,
    num_messages: int = 0,
) -> ClassifiedError:
    """Classify an API exception into retry and recovery actions."""
    status_code = extract_status_code(error)
    body = extract_error_body(error)
    error_msg = _combined_error_message(error, body)
    provider_lower = provider.lower()
    error_type = type(error).__name__

    def result(reason: FailoverReason, **overrides: Any) -> ClassifiedError:
        values = {
            "reason": reason,
            "status_code": status_code,
            "provider": provider or None,
            "model": model or None,
            "message": extract_message(error, body),
            "error_context": extract_error_context(error),
        }
        values.update(overrides)
        return ClassifiedError(**values)

    if error_type in {"AuthenticationError", "PermissionDeniedError"}:
        return result(
            FailoverReason.auth,
            retryable=False,
            should_rotate_credential=error_type == "AuthenticationError",
            should_fallback=True,
        )

    if error_type == "RateLimitError":
        return result(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if error_type == "APITimeoutError":
        return result(FailoverReason.timeout, retryable=True)

    if status_code == 400 and "signature" in error_msg and "thinking" in error_msg:
        return result(FailoverReason.thinking_signature, retryable=True)

    if status_code == 429 and "extra usage" in error_msg and "long context" in error_msg:
        return result(
            FailoverReason.long_context_tier,
            retryable=True,
            should_compress=True,
        )

    if (
        status_code == 400
        and "long context beta" in error_msg
        and "not yet available" in error_msg
    ):
        return result(
            FailoverReason.oauth_long_context_beta_forbidden,
            retryable=True,
        )

    if status_code == 400 and (
        "error parsing grammar" in error_msg
        or "json-schema-to-grammar" in error_msg
        or ("unable to generate parser" in error_msg and "template" in error_msg)
    ):
        return result(FailoverReason.llama_cpp_grammar_pattern, retryable=True)

    if any(pattern in error_msg for pattern in _SSL_TRANSIENT_PATTERNS):
        return result(FailoverReason.timeout, retryable=True)

    if status_code == 402:
        has_usage_limit = any(pattern in error_msg for pattern in _USAGE_LIMIT_PATTERNS)
        has_transient = any(
            pattern in error_msg for pattern in _USAGE_LIMIT_TRANSIENT_SIGNALS
        )
        if has_usage_limit and has_transient:
            return result(
                FailoverReason.rate_limit,
                retryable=True,
                should_rotate_credential=True,
                should_fallback=True,
            )
        return result(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code in (401, 403):
        return result(
            FailoverReason.auth,
            retryable=False,
            should_rotate_credential=status_code == 401,
            should_fallback=True,
        )

    if status_code == 413:
        return result(
            FailoverReason.payload_too_large,
            retryable=True,
            should_compress=True,
        )

    if status_code == 429 or any(pattern in error_msg for pattern in _RATE_LIMIT_PATTERNS):
        return result(
            FailoverReason.rate_limit,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code == 400 and any(pattern in error_msg for pattern in _CONTEXT_OVERFLOW_PATTERNS):
        return result(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    if status_code == 400 and provider_lower == "anthropic" and _large_context(
        approx_tokens,
        context_length,
        num_messages,
    ):
        return result(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    if any(pattern in error_msg for pattern in _BILLING_PATTERNS):
        return result(
            FailoverReason.billing,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if any(pattern in error_msg for pattern in _CONTEXT_OVERFLOW_PATTERNS):
        return result(
            FailoverReason.context_overflow,
            retryable=True,
            should_compress=True,
        )

    if any(pattern in error_msg for pattern in _SERVER_DISCONNECT_PATTERNS):
        if not status_code and _large_context(approx_tokens, context_length, num_messages):
            return result(
                FailoverReason.context_overflow,
                retryable=True,
                should_compress=True,
            )
        return result(FailoverReason.timeout, retryable=True)

    if status_code == 404 and any(pattern in error_msg for pattern in _MODEL_NOT_FOUND_PATTERNS):
        return result(
            FailoverReason.model_not_found,
            retryable=False,
            should_fallback=True,
        )

    if status_code in (500, 502):
        return result(FailoverReason.server_error, retryable=True)
    if status_code in (503, 529):
        return result(FailoverReason.overloaded, retryable=True)
    if error_type in _TRANSPORT_ERROR_TYPES or isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return result(FailoverReason.timeout, retryable=True)
    if status_code is not None and 400 <= status_code < 500:
        return result(FailoverReason.format_error, retryable=False, should_fallback=True)
    if status_code is not None and 500 <= status_code < 600:
        return result(FailoverReason.server_error, retryable=True)
    return result(FailoverReason.unknown, retryable=True)


def extract_status_code(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def extract_error_body(error: Exception) -> dict[str, Any]:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(error, "response", None)
    body = getattr(response, "json", None)
    if callable(body):
        try:
            value = body()
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def extract_error_context(error: Exception) -> dict[str, Any]:
    body = extract_error_body(error)
    return body if isinstance(body, dict) else {}


def extract_message(error: Exception, body: dict[str, Any] | None = None) -> str:
    body = body or {}
    err = body.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if body.get("message"):
        return str(body["message"])
    return str(error)


def _combined_error_message(error: Exception, body: dict[str, Any]) -> str:
    parts = [str(error).lower()]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        message = str(err.get("message") or "").lower()
        if message and message not in parts[0]:
            parts.append(message)
        metadata = err.get("metadata")
        if isinstance(metadata, dict):
            raw = metadata.get("raw")
            if isinstance(raw, str) and raw.strip():
                try:
                    inner = json.loads(raw)
                except json.JSONDecodeError:
                    inner = {}
                inner_err = inner.get("error") if isinstance(inner, dict) else None
                if isinstance(inner_err, dict):
                    inner_msg = str(inner_err.get("message") or "").lower()
                    if inner_msg and all(inner_msg not in part for part in parts):
                        parts.append(inner_msg)
    elif isinstance(body, dict) and body.get("message"):
        message = str(body["message"]).lower()
        if message and message not in parts[0]:
            parts.append(message)
    return " ".join(parts)


def _large_context(approx_tokens: int, context_length: int, num_messages: int) -> bool:
    return approx_tokens > context_length * 0.6 or (
        context_length <= 256_000 and (approx_tokens > 120_000 or num_messages > 200)
    )
