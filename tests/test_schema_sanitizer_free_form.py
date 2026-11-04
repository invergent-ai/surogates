"""A free-form object argument must not read as an object with no fields.

``"properties": {}`` on its own says the object has none, and a model that takes
the schema literally sends ``{}``. On PROD ``worker_complete`` lost its metadata
and ``share_note`` its citation this way, so a rubric reading ``result_metadata``
could not pass and every cited board note was rejected.
"""
from __future__ import annotations

from typing import Any

from surogates.tools.schema_sanitizer import sanitize_tool_schemas


def _tool(parameters: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": "t", "parameters": parameters}}


def _parameters(tool: dict[str, Any]) -> dict[str, Any]:
    return sanitize_tool_schemas([tool])[0]["function"]["parameters"]


def test_a_nested_object_with_no_properties_is_declared_free_form() -> None:
    params = _parameters(_tool({
        "type": "object",
        "properties": {"metadata": {"type": "object", "description": "Free-form."}},
    }))

    metadata = params["properties"]["metadata"]
    assert metadata["properties"] == {}
    assert metadata["additionalProperties"] is True


def test_an_object_with_declared_properties_is_left_closed_as_written() -> None:
    params = _parameters(_tool({
        "type": "object",
        "properties": {"ref": {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "additionalProperties": False,
        }},
        "additionalProperties": False,
    }))

    assert params["additionalProperties"] is False
    assert params["properties"]["ref"]["additionalProperties"] is False


def test_an_explicit_additional_properties_is_kept() -> None:
    params = _parameters(_tool({
        "type": "object",
        "properties": {"m": {"type": "object", "additionalProperties": {"type": "string"}}},
    }))

    assert params["properties"]["m"]["additionalProperties"] == {"type": "string"}


def test_a_tool_with_no_arguments_stays_closed() -> None:
    params = _parameters(_tool({"type": "object", "properties": {}}))

    assert params == {"type": "object", "properties": {}}


def test_share_note_declares_what_a_citation_holds() -> None:
    from surogates.board.tools import _SHARE_NOTE_SCHEMA

    item = _SHARE_NOTE_SCHEMA.parameters["properties"]["notes"]["items"]
    ref = item["properties"]["ref"]

    assert ref["required"] == ["kind"]
    assert set(ref["properties"]) == {
        "kind", "tool", "contains", "session_id", "event_id", "artifact_id",
    }
    assert "ref" not in item["required"]
