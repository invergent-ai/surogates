"""Tests for surogates.tools.coerce module.

Covers: integer, number, boolean, union type coercion, and edge cases.
"""

from __future__ import annotations

import pytest

from surogates.tools.coerce import coerce_tool_args, _coerce_boolean, _coerce_number, _coerce_value
from surogates.tools.registry import ToolRegistry, ToolSchema


def _make_registry_with_schema(properties: dict) -> ToolRegistry:
    """Create a registry with a single tool that has the given properties schema."""
    reg = ToolRegistry()
    reg.register(
        "my_tool",
        ToolSchema(
            name="my_tool",
            description="test tool",
            parameters={"type": "object", "properties": properties},
        ),
        handler=lambda x: x,
    )
    return reg


# ---------------------------------------------------------------------------
# _coerce_number
# ---------------------------------------------------------------------------


class TestCoerceNumber:
    def test_integer_string(self) -> None:
        assert _coerce_number("42") == 42

    def test_float_string(self) -> None:
        assert _coerce_number("3.14") == 3.14

    def test_negative_integer(self) -> None:
        assert _coerce_number("-7") == -7

    def test_integer_only_with_int(self) -> None:
        assert _coerce_number("42", integer_only=True) == 42

    def test_integer_only_with_float_returns_string(self) -> None:
        result = _coerce_number("3.14", integer_only=True)
        assert result == "3.14"

    def test_non_numeric_returns_original(self) -> None:
        result = _coerce_number("abc")
        assert result == "abc"

    def test_empty_string_returns_original(self) -> None:
        result = _coerce_number("")
        assert result == ""

    def test_inf_returns_float(self) -> None:
        result = _coerce_number("inf")
        assert result == float("inf")

    def test_nan_returns_float(self) -> None:
        import math
        result = _coerce_number("nan")
        assert math.isnan(result)

    def test_zero(self) -> None:
        assert _coerce_number("0") == 0
        assert isinstance(_coerce_number("0"), int)

    def test_float_that_is_integer(self) -> None:
        assert _coerce_number("5.0") == 5
        assert isinstance(_coerce_number("5.0"), int)


# ---------------------------------------------------------------------------
# _coerce_boolean
# ---------------------------------------------------------------------------


class TestCoerceBoolean:
    def test_true_lowercase(self) -> None:
        assert _coerce_boolean("true") is True

    def test_false_lowercase(self) -> None:
        assert _coerce_boolean("false") is False

    def test_true_mixed_case(self) -> None:
        assert _coerce_boolean("True") is True

    def test_false_mixed_case(self) -> None:
        assert _coerce_boolean("False") is False

    def test_true_with_whitespace(self) -> None:
        assert _coerce_boolean("  true  ") is True

    def test_non_boolean_returns_original(self) -> None:
        assert _coerce_boolean("yes") == "yes"
        assert _coerce_boolean("1") == "1"
        assert _coerce_boolean("") == ""


# ---------------------------------------------------------------------------
# _coerce_value
# ---------------------------------------------------------------------------


class TestCoerceValue:
    def test_integer_type(self) -> None:
        assert _coerce_value("42", "integer") == 42

    def test_number_type(self) -> None:
        assert _coerce_value("3.14", "number") == 3.14

    def test_boolean_type(self) -> None:
        assert _coerce_value("true", "boolean") is True

    def test_string_type_unchanged(self) -> None:
        result = _coerce_value("hello", "string")
        assert result == "hello"

    def test_union_type_integer_first(self) -> None:
        result = _coerce_value("42", ["integer", "string"])
        assert result == 42

    def test_union_type_no_match(self) -> None:
        result = _coerce_value("hello", ["integer", "number"])
        assert result == "hello"

    def test_union_type_boolean(self) -> None:
        result = _coerce_value("true", ["boolean", "string"])
        assert result is True

    def test_unknown_type_unchanged(self) -> None:
        result = _coerce_value("data", "array")
        assert result == "data"


# ---------------------------------------------------------------------------
# coerce_tool_args
# ---------------------------------------------------------------------------


class TestCoerceToolArgs:
    def test_coerces_integer_arg(self) -> None:
        reg = _make_registry_with_schema({"count": {"type": "integer"}})
        args = {"count": "42"}
        result = coerce_tool_args("my_tool", args, reg)
        assert result["count"] == 42

    def test_coerces_boolean_arg(self) -> None:
        reg = _make_registry_with_schema({"verbose": {"type": "boolean"}})
        args = {"verbose": "true"}
        result = coerce_tool_args("my_tool", args, reg)
        assert result["verbose"] is True

    def test_coerces_number_arg(self) -> None:
        reg = _make_registry_with_schema({"rate": {"type": "number"}})
        args = {"rate": "0.5"}
        result = coerce_tool_args("my_tool", args, reg)
        assert result["rate"] == 0.5

    def test_non_string_values_unchanged(self) -> None:
        reg = _make_registry_with_schema({"count": {"type": "integer"}})
        args = {"count": 42}
        result = coerce_tool_args("my_tool", args, reg)
        assert result["count"] == 42

    def test_unknown_tool_returns_args_unchanged(self) -> None:
        reg = ToolRegistry()
        args = {"count": "42"}
        result = coerce_tool_args("unknown_tool", args, reg)
        assert result["count"] == "42"

    def test_empty_args_returns_empty(self) -> None:
        reg = _make_registry_with_schema({"count": {"type": "integer"}})
        assert coerce_tool_args("my_tool", {}, reg) == {}

    def test_none_args_returns_none(self) -> None:
        reg = _make_registry_with_schema({"count": {"type": "integer"}})
        assert coerce_tool_args("my_tool", None, reg) is None

    def test_no_properties_in_schema(self) -> None:
        reg = ToolRegistry()
        reg.register(
            "bare_tool",
            ToolSchema(name="bare_tool", description="test", parameters={}),
            handler=lambda x: x,
        )
        args = {"count": "42"}
        result = coerce_tool_args("bare_tool", args, reg)
        assert result["count"] == "42"

    def test_arg_not_in_schema_unchanged(self) -> None:
        reg = _make_registry_with_schema({"count": {"type": "integer"}})
        args = {"count": "42", "extra": "hello"}
        result = coerce_tool_args("my_tool", args, reg)
        assert result["count"] == 42
        assert result["extra"] == "hello"

    def test_no_type_in_prop_schema_unchanged(self) -> None:
        reg = _make_registry_with_schema({"name": {"description": "A name"}})
        args = {"name": "42"}
        result = coerce_tool_args("my_tool", args, reg)
        assert result["name"] == "42"

    def test_coerce_failure_preserves_original(self) -> None:
        reg = _make_registry_with_schema({"count": {"type": "integer"}})
        args = {"count": "not-a-number"}
        result = coerce_tool_args("my_tool", args, reg)
        assert result["count"] == "not-a-number"

    def test_multiple_args_coerced(self) -> None:
        reg = _make_registry_with_schema({
            "count": {"type": "integer"},
            "verbose": {"type": "boolean"},
            "name": {"type": "string"},
        })
        args = {"count": "7", "verbose": "false", "name": "test"}
        result = coerce_tool_args("my_tool", args, reg)
        assert result["count"] == 7
        assert result["verbose"] is False
        assert result["name"] == "test"
