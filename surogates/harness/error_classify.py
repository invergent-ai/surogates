"""Classify harness exceptions into structured, user-facing categories.

The harness and orchestrator both catch broad exception types and emit
``harness.crash`` / ``session.fail`` events.  Historically the UI received
only a raw ``str(exc)`` which is meaningless to end users
(e.g. "Invalid LLM response: response is None or has no choices").

:func:`classify_harness_error` inspects an exception and returns
structured :class:`ErrorInfo` with a fixed category, a human-readable
title, a trimmed detail line, and a ``retryable`` hint for the UI.
Classification is pure — no I/O, no state — so it is safe to call from
any emission site without worrying about side effects.

The classifier is LLM-shape-aware (OpenAI / Anthropic SDK error classes
and HTTP status codes on the exception) but also covers database,
storage, network, timeout, governance, and unknown failures so every
``harness.crash`` carries a category, not only LLM-originated ones.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

__all__ = ["ErrorCategory", "ErrorInfo", "classify_harness_error"]

# Categories are stored on events and read by the frontend; treat the
# set as a stable contract.
ErrorCategory = str  # one of the values in _CATEGORY_TITLES below

# Fixed, user-facing title for each category.  Displayed in the UI error
# bubble as the primary line.
_CATEGORY_TITLES: Final[dict[str, str]] = {
    "provider_error":    "The model provider returned an error.",
    "rate_limit":        "Rate limit reached with the model provider.",
    "auth_failed":       "Authentication with the model provider failed.",
    "context_overflow":  "Conversation is too long for the selected model.",
    "network":           "Network error reaching the model provider.",
    "timeout":           "The model provider timed out.",
    "invalid_response":  "The model returned an empty or malformed response.",
    "tool_error":        "A tool failed during execution.",
    "storage_error":     "Workspace storage is unavailable.",
    "database_error":    "Session storage is unavailable.",
    "governance_denied": "Action blocked by governance policy.",
    "unknown":           "The session failed due to an internal error.",
}

# Categories whose resolution requires operator/user action rather than
# a passive retry.  The UI hides the "Retry" button when retryable is
# False so users don't burn cycles clicking it.
_NON_RETRYABLE: Final[frozenset[str]] = frozenset({
    "auth_failed",
    "governance_denied",
    "context_overflow",
})

# Maximum characters for the ``detail`` field.  The full traceback still
# lands in the ``traceback`` field on ``harness.crash`` / ``session.fail``
# for debugging — ``detail`` is just the message line for the UI.
_MAX_DETAIL_CHARS: Final[int] = 500

# Substring markers used to recognise context-overflow errors emitted by
# providers that don't set a distinct HTTP status.  Mirrors the
# ``_CONTEXT_PHRASES`` tuple in :mod:`surogates.harness.llm_call` so the
# two detection paths stay aligned.
_CONTEXT_PHRASES: Final[tuple[str, ...]] = (
    "context length",
    "context size",
    "maximum context",
    "token limit",
    "too many tokens",
    "reduce the length",
    "exceeds the limit",
    "context window",
    "request entity too large",
    "prompt is too long",
    "prompt exceeds max length",
)

# Substrings that mark an OpenAI-style "empty response" wrapper raised
# by :mod:`surogates.harness.llm_call`.  Used to distinguish a genuine
# empty-response from other ``ValueError`` cases.
_INVALID_RESPONSE_PHRASES: Final[tuple[str, ...]] = (
    "invalid llm response",
    "llm returned empty response",
    "response is none or has no choices",
)

# Network-layer exception class names we treat as transient network
# errors.  Matched by ``type(exc).__name__`` to avoid importing the
# providers as hard dependencies.
_NETWORK_EXC_NAMES: Final[frozenset[str]] = frozenset({
    "ConnectError",
    "ConnectTimeout",
    "ReadError",
    "RemoteProtocolError",
    "ServerDisconnectedError",
    "ProtocolError",
})

# Timeout exception class names.
_TIMEOUT_EXC_NAMES: Final[frozenset[str]] = frozenset({
    "TimeoutError",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "asyncio.TimeoutError",
})


@dataclass(frozen=True)
class ErrorInfo:
    """Structured classification of a harness exception.

    Attributes
    ----------
    category:
        Stable category key (see :data:`_CATEGORY_TITLES`).  Consumed by
        the UI to choose an icon / copy; stored on events for analytics.
    title:
        One-line, user-facing summary.  Fixed per category, never
        includes the raw exception text.
    detail:
        Trimmed ``str(exc)`` (≤500 chars).  Shown in the UI's collapsible
        "Show details" disclosure.
    retryable:
        Hint for the UI: ``True`` when a passive retry has a realistic
        chance of succeeding (transient provider / network / storage
        issues), ``False`` when resolution requires user action (auth
        failure, governance deny, context overflow).
    """

    category: str
    title: str
    detail: str
    retryable: bool


def classify_harness_error(exc: BaseException) -> ErrorInfo:
    """Classify ``exc`` into an :class:`ErrorInfo`.

    Checks are ordered most-specific to most-generic.  The first match
    wins.  Unknown exceptions fall through to the ``unknown`` category
    with the first line of ``str(exc)`` as the detail.
    """
    status_code = _extract_status_code(exc)
    error_msg = str(exc)
    error_msg_lower = error_msg.lower()
    type_name = type(exc).__name__
    module_name = type(exc).__module__ or ""

    category = _pick_category(
        exc=exc,
        status_code=status_code,
        error_msg_lower=error_msg_lower,
        type_name=type_name,
        module_name=module_name,
    )

    return ErrorInfo(
        category=category,
        title=_CATEGORY_TITLES[category],
        detail=_trim_detail(error_msg or type_name),
        retryable=category not in _NON_RETRYABLE,
    )


def _pick_category(
    *,
    exc: BaseException,
    status_code: int | None,
    error_msg_lower: str,
    type_name: str,
    module_name: str,
) -> str:
    """Return the best-matching category key for ``exc``."""

    # 1. HTTP status codes from LLM SDK errors -- most decisive signal.
    if status_code is not None:
        if status_code in (401, 403):
            return "auth_failed"
        if status_code == 429:
            # Some providers (Anthropic Sonnet 1M tier) conflate the
            # context-overflow tier gate with HTTP 429.  The message
            # contains "long context" + "extra usage" when that's the
            # case — treat it as context_overflow rather than rate_limit
            # so the UI hides the retry button.
            if "long context" in error_msg_lower and "extra usage" in error_msg_lower:
                return "context_overflow"
            return "rate_limit"
        if status_code == 413:
            return "context_overflow"
        if 500 <= status_code < 600:
            return "provider_error"

    # 2. OpenAI / Anthropic SDK class names (no hard import).
    #    Matches regardless of which SDK raised the error.
    if type_name in ("AuthenticationError", "PermissionDeniedError"):
        return "auth_failed"
    if type_name == "RateLimitError":
        return "rate_limit"
    if type_name == "APITimeoutError":
        return "timeout"

    # 3. Context-overflow phrases (local backends often return 400 or
    #    no status at all with a descriptive message).
    if any(phrase in error_msg_lower for phrase in _CONTEXT_PHRASES):
        return "context_overflow"

    # 4. Invalid / empty response wrappers.  These come from
    #    :mod:`surogates.harness.llm_call` when the provider returns no
    #    choices — the exact failure mode that triggered this feature.
    if isinstance(exc, ValueError) and any(
        phrase in error_msg_lower for phrase in _INVALID_RESPONSE_PHRASES
    ):
        return "invalid_response"

    # 5. Timeouts -- network-independent.
    if _is_timeout(exc, type_name):
        return "timeout"

    # 6. Network / transport failures.
    if type_name in _NETWORK_EXC_NAMES:
        return "network"
    if module_name.startswith("httpx") or module_name.startswith("aiohttp"):
        # httpx / aiohttp errors we haven't named explicitly — network.
        return "network"
    if isinstance(exc, ConnectionError):
        return "network"

    # 7. Database failures.
    if module_name.startswith(("sqlalchemy", "asyncpg", "psycopg")):
        return "database_error"

    # 8. Storage failures (aioboto3 / botocore / aiobotocore).
    if module_name.startswith(("botocore", "aiobotocore", "boto3", "aioboto3")):
        return "storage_error"
    if type_name in ("ClientError", "EndpointConnectionError", "NoCredentialsError"):
        return "storage_error"

    # 9. Governance / policy denials.  The governance package raises a
    #    narrow set of exceptions; match on the class name so we don't
    #    take a hard dependency.
    if "Policy" in type_name and ("Denied" in type_name or "Deny" in type_name):
        return "governance_denied"
    if type_name in ("PolicyDeniedError", "GovernanceError"):
        return "governance_denied"

    # 10. Tool-execution wrappers (rare — tools usually return result
    #     strings rather than raising, but saga compensators can raise).
    if type_name in ("ToolExecutionError", "SagaCompensationError"):
        return "tool_error"

    # 11. Plain APIError from the OpenAI SDK with no status code is
    #     almost always a provider-side 5xx that the SDK normalised.
    if type_name in ("APIError", "APIStatusError", "BadRequestError"):
        return "provider_error"

    return "unknown"


def _extract_status_code(exc: BaseException) -> int | None:
    """Return an HTTP status code from ``exc`` if one is attached.

    Checks, in order: ``exc.status_code``, ``exc.response.status_code``,
    ``exc.http_status``, ``exc.code`` (when numeric).  Returns ``None``
    if nothing resembles a status code.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status

    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status

    status = getattr(exc, "http_status", None)
    if isinstance(status, int):
        return status

    status = getattr(exc, "code", None)
    if isinstance(status, int):
        return status

    return None


def _is_timeout(exc: BaseException, type_name: str) -> bool:
    """Return ``True`` when ``exc`` represents a timeout."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if type_name in _TIMEOUT_EXC_NAMES:
        return True
    return False


def _trim_detail(text: str) -> str:
    """Trim ``text`` to the first line, capped at ``_MAX_DETAIL_CHARS``.

    Splits on the first newline to strip multi-line tracebacks or
    provider JSON blobs.  If the first line is longer than the cap,
    cuts at the last whitespace boundary before the limit and appends
    an ellipsis so the output reads naturally.
    """
    if not text:
        return ""

    first_line = text.splitlines()[0].strip()

    if len(first_line) <= _MAX_DETAIL_CHARS:
        return first_line

    cutoff = first_line.rfind(" ", 0, _MAX_DETAIL_CHARS)
    if cutoff < _MAX_DETAIL_CHARS // 2:
        # No whitespace near the boundary — hard cut.
        cutoff = _MAX_DETAIL_CHARS
    return first_line[:cutoff].rstrip() + "…"
