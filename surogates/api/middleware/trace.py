"""Distributed trace context middleware.

Injects a :class:`~surogates.trace.TraceContext` into the contextvar for
every HTTP request.  If the caller sends a W3C ``traceparent`` header the
trace is continued; otherwise a new trace is originated.

The trace ID is also set as ``request.state.trace_id`` for convenience and
echoed back via the ``X-Trace-Id`` response header so callers can correlate
their requests with server-side events.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from surogates.trace import from_headers, new_span, new_trace, set_trace

if TYPE_CHECKING:
    from fastapi import FastAPI, Request, Response
    from starlette.middleware.base import RequestResponseEndpoint

    from surogates.config import Settings


def setup_trace_middleware(app: FastAPI, settings: Settings) -> None:
    """Attach the trace-context middleware to the FastAPI application."""

    @app.middleware("http")
    async def _trace_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Continue an existing trace or start a new one.
        # from_headers() already generates a fresh span_id for the parsed
        # context so we just set it — no extra new_span() needed.
        incoming = from_headers(request.headers)
        if incoming is not None:
            set_trace(incoming)
            ctx = incoming
        else:
            ctx = new_trace()

        request.state.trace_id = ctx.trace_id

        response: Response = await call_next(request)

        # Echo trace ID so callers can correlate.
        response.headers["X-Trace-Id"] = ctx.trace_id
        response.headers["traceparent"] = (
            f"00-{ctx.trace_id}-{ctx.span_id}-01"
        )
        return response
