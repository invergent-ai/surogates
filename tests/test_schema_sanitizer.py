"""Tests for LLM-compatible tool schema sanitization."""

from __future__ import annotations

import copy

from surogates.tools.schema_sanitizer import sanitize_tool_schemas


def _tool(parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "problem_tool",
            "description": "schema sanitizer fixture",
            "parameters": parameters,
        },
    }


def test_sanitizes_provider_hostile_schema_shapes_without_mutating_input() -> None:
    original = _tool(
        {
            "type": "object",
            "anyOf": [{"required": ["path"]}],
            "properties": {
                "path": {"type": ["string", "null"]},
                "filters": {
                    "anyOf": [
                        {"type": "array", "items": "string"},
                        {"type": "null"},
                    ],
                    "description": "optional filters",
                },
                "options": {"type": "object"},
                "extra": "object",
            },
            "required": ["path", "missing"],
        }
    )
    snapshot = copy.deepcopy(original)

    [sanitized] = sanitize_tool_schemas([original])

    assert original == snapshot
    params = sanitized["function"]["parameters"]
    assert "anyOf" not in params
    assert params["type"] == "object"
    assert params["required"] == ["path"]
    assert params["properties"]["path"]["type"] == "string"
    assert params["properties"]["path"]["nullable"] is True
    assert params["properties"]["filters"]["type"] == "array"
    assert params["properties"]["filters"]["nullable"] is True
    assert params["properties"]["filters"]["items"] == {"type": "string"}
    assert params["properties"]["options"] == {"type": "object", "properties": {}}
    assert params["properties"]["extra"] == {"type": "object", "properties": {}}


def test_missing_or_invalid_parameters_become_empty_object_schema() -> None:
    tools = [
        {"type": "function", "function": {"name": "missing", "description": "x"}},
        {"type": "function", "function": {"name": "bad", "description": "x", "parameters": "object"}},
    ]

    sanitized = sanitize_tool_schemas(tools)

    assert sanitized[0]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert sanitized[1]["function"]["parameters"] == {"type": "object", "properties": {}}
