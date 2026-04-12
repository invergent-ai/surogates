"""Distributed trace context propagation via contextvars.

Provides W3C Trace Context-compatible trace and span IDs that travel
implicitly through the async call-chain.  Every event, log line, and
cross-boundary call can include the current ``trace_id`` and ``span_id``
without explicit parameter threading.

Design mirrors :mod:`surogates.tenant.context` — a frozen dataclass held
in a :class:`contextvars.ContextVar`.

ID format
---------
- ``trace_id``: 32 hex characters (128-bit), compatible with W3C ``traceparent``.
- ``span_id``: 16 hex characters (64-bit).

These are deliberately compatible with OpenTelemetry so that future OTEL
adoption requires no ID format migration.
"""

from __future__ import annotations

import contextvars
import os
from dataclasses import dataclass, field
from typing import Mapping

__all__ = [
    "TraceContext",
    "get_trace",
    "set_trace",
    "new_trace",
    "new_span",
    "trace_headers",
    "from_headers",
]

# W3C Trace Context version.
_TRACE_VERSION = "00"

# Trace flag: sampled.
_TRACE_FLAGS = "01"


def _random_trace_id() -> str:
    """Generate a 128-bit random trace ID as a 32-character hex string."""
    return os.urandom(16).hex()


def _random_span_id() -> str:
    """Generate a 64-bit random span ID as a 16-character hex string."""
    return os.urandom(8).hex()


@dataclass(frozen=True)
class TraceContext:
    """Immutable snapshot of the current distributed trace position."""

    trace_id: str = field(default_factory=_random_trace_id)
    span_id: str = field(default_factory=_random_span_id)
    parent_span_id: str | None = None


_trace_ctx: contextvars.ContextVar[TraceContext] = contextvars.ContextVar(
    "trace_ctx"
)


# ------------------------------------------------------------------
# Read / write
# ------------------------------------------------------------------


def get_trace() -> TraceContext | None:
    """Return the active ``TraceContext``, or ``None`` if unset.

    Unlike :func:`surogates.tenant.context.get_tenant` this does *not*
    raise on a missing value — tracing is advisory, not mandatory.
    """
    return _trace_ctx.get(None)


def set_trace(ctx: TraceContext) -> contextvars.Token[TraceContext]:
    """Bind *ctx* as the current trace context.

    Returns a reset token for ``_trace_ctx.reset()``.
    """
    return _trace_ctx.set(ctx)


# ------------------------------------------------------------------
# Factories
# ------------------------------------------------------------------


def new_trace() -> TraceContext:
    """Create a brand-new trace with a fresh root span.

    Use at trace entry-points: API middleware, orchestrator dequeue.
    """
    ctx = TraceContext()
    set_trace(ctx)
    return ctx


def new_span(parent: TraceContext | None = None) -> TraceContext:
    """Create a child span under *parent* (or the current contextvar).

    The ``trace_id`` is inherited; a new ``span_id`` is generated; the
    parent's ``span_id`` becomes ``parent_span_id``.

    If no parent is available a fresh root trace is created instead.
    """
    parent = parent or get_trace()
    if parent is None:
        return new_trace()
    ctx = TraceContext(
        trace_id=parent.trace_id,
        span_id=_random_span_id(),
        parent_span_id=parent.span_id,
    )
    set_trace(ctx)
    return ctx


# ------------------------------------------------------------------
# W3C Trace Context header helpers
# ------------------------------------------------------------------


def trace_headers(ctx: TraceContext | None = None) -> dict[str, str]:
    """Return a ``traceparent`` header dict for outbound HTTP requests.

    Format: ``{version}-{trace_id}-{span_id}-{flags}``
    See https://www.w3.org/TR/trace-context/
    """
    ctx = ctx or get_trace()
    if ctx is None:
        return {}
    return {
        "traceparent": f"{_TRACE_VERSION}-{ctx.trace_id}-{ctx.span_id}-{_TRACE_FLAGS}",
    }


def from_headers(headers: Mapping[str, str]) -> TraceContext | None:
    """Parse an incoming ``traceparent`` header into a :class:`TraceContext`.

    Returns ``None`` if the header is missing or malformed.
    """
    raw = headers.get("traceparent")
    if raw is None:
        # Case-insensitive fallback.
        for key, value in headers.items():
            if key.lower() == "traceparent":
                raw = value
                break
    if raw is None:
        return None
    parts = raw.split("-")
    if len(parts) < 4:
        return None
    _version, trace_id, parent_span_id, _flags = parts[0], parts[1], parts[2], parts[3]
    if len(trace_id) != 32 or len(parent_span_id) != 16:
        return None
    # W3C spec requires lowercase hex only; reject all-zeros as invalid.
    try:
        int(trace_id, 16)
        int(parent_span_id, 16)
    except ValueError:
        return None
    if trace_id == "0" * 32 or parent_span_id == "0" * 16:
        return None
    return TraceContext(
        trace_id=trace_id,
        span_id=_random_span_id(),
        parent_span_id=parent_span_id,
    )
