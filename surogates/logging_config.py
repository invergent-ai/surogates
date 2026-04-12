"""Structured JSON logging with automatic trace context injection.

Replaces the default Python logging formatters with a JSON-lines formatter
that includes ``trace_id``, ``span_id``, and ``parent_span_id`` from the
active :class:`surogates.trace.TraceContext` contextvar on every log record.

Usage::

    from surogates.logging_config import configure_logging
    configure_logging()  # call once at process startup
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class _TraceFilter(logging.Filter):
    """Logging filter that injects trace context fields into every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        from surogates.trace import get_trace

        ctx = get_trace()
        record.trace_id = ctx.trace_id if ctx else ""  # type: ignore[attr-defined]
        record.span_id = ctx.span_id if ctx else ""  # type: ignore[attr-defined]
        record.parent_span_id = ctx.parent_span_id or "" if ctx else ""  # type: ignore[attr-defined]
        return True


class StructuredFormatter(logging.Formatter):
    """Emit each log record as a single JSON line.

    Fields included: ``timestamp``, ``level``, ``logger``, ``message``,
    ``trace_id``, ``span_id``, ``parent_span_id``, and ``error`` (when
    an exception is attached).
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Trace fields (injected by _TraceFilter).
        trace_id = getattr(record, "trace_id", "")
        if trace_id:
            entry["trace_id"] = trace_id
            entry["span_id"] = getattr(record, "span_id", "")
            parent = getattr(record, "parent_span_id", "")
            if parent:
                entry["parent_span_id"] = parent

        # Exception info.
        if record.exc_info and record.exc_info[1] is not None:
            entry["error"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def configure_logging(
    level: int | str = logging.INFO,
    *,
    structured: bool = True,
) -> None:
    """Configure the root logger for the process.

    Parameters
    ----------
    level:
        Minimum log level.
    structured:
        When ``True`` (default) emit JSON lines.  When ``False`` use a
        human-readable format that still includes trace IDs for local
        development.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers to avoid duplicate output.
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_TraceFilter())

    if structured:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(trace_id)s/%(span_id)s] %(name)s — %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )

    root.addHandler(handler)
