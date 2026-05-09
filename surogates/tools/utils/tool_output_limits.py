"""Configurable limits for model-visible tool output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_BYTES = 50_000
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_LINE_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class ToolOutputLimits:
    max_bytes: int = DEFAULT_MAX_BYTES
    max_lines: int = DEFAULT_MAX_LINES
    max_line_length: int = DEFAULT_MAX_LINE_LENGTH


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def get_tool_output_limits() -> ToolOutputLimits:
    """Return configured tool-output limits, falling back on safe defaults."""
    try:
        from surogates.config import load_settings
        cfg = load_settings().tool_output
        return ToolOutputLimits(
            max_bytes=_positive_int(cfg.max_bytes, DEFAULT_MAX_BYTES),
            max_lines=_positive_int(cfg.max_lines, DEFAULT_MAX_LINES),
            max_line_length=_positive_int(
                cfg.max_line_length, DEFAULT_MAX_LINE_LENGTH,
            ),
        )
    except Exception:
        return ToolOutputLimits()


def get_max_bytes() -> int:
    return get_tool_output_limits().max_bytes


def get_max_lines() -> int:
    return get_tool_output_limits().max_lines


def get_max_line_length() -> int:
    return get_tool_output_limits().max_line_length
