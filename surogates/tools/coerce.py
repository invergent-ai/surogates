"""Tool argument type coercion.

LLMs frequently return numbers as strings (``"42"`` instead of ``42``) and
booleans as strings (``"true"`` instead of ``true``).  This coerces values
to match the tool's JSON Schema type declarations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from surogates.tools.registry import ToolRegistry


def coerce_tool_args(tool_name: str, args: dict[str, Any], registry: ToolRegistry) -> dict[str, Any]:
    """Coerce tool args to match their JSON Schema types.

    Handles: integer, number, boolean, and union types.
    Original values preserved when coercion fails.
    """
    if not args or not isinstance(args, dict):
        return args
    entry = registry.get(tool_name)
    if entry is None:
        return args
    properties = entry.schema.parameters.get("properties", {})
    if not properties:
        return args

    for key, value in args.items():
        if not isinstance(value, str):
            continue
        prop_schema = properties.get(key)
        if not prop_schema:
            continue
        expected = prop_schema.get("type")
        if not expected:
            continue
        coerced = _coerce_value(value, expected)
        if coerced is not value:
            args[key] = coerced
    return args


def _coerce_value(value: str, expected_type: str | list[str]) -> Any:
    """Attempt to coerce a string *value* to *expected_type*.

    Returns the original string when coercion is not applicable or fails.
    """
    if isinstance(expected_type, list):
        # Union type -- try each in order, return first successful coercion
        for t in expected_type:
            result = _coerce_value(value, t)
            if result is not value:
                return result
        return value

    if expected_type in ("integer", "number"):
        return _coerce_number(value, integer_only=(expected_type == "integer"))
    if expected_type == "boolean":
        return _coerce_boolean(value)
    return value


def _coerce_number(value: str, integer_only: bool = False) -> Any:
    """Try to parse *value* as a number.  Returns original string on failure."""
    try:
        f = float(value)
    except (ValueError, OverflowError):
        return value
    # Guard against inf/nan before int() conversion
    if f != f or f == float("inf") or f == float("-inf"):
        return f
    # If it looks like an integer (no fractional part), return int
    if f == int(f):
        return int(f)
    if integer_only:
        # Schema wants an integer but value has decimals -- keep as string
        return value
    return f


def _coerce_boolean(value: str) -> Any:
    """Try to parse *value* as a boolean.  Returns original string on failure."""
    low = value.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    return value
