"""The model's null reaches a nullable tool parameter as null, not as the string "null".

The sanitizer collapses ``anyOf: [{type: string}, {type: null}]`` to
``{type: string, nullable: true}`` for the model.  Our engine's tool-call parser
types each value from that schema, so a literal ``null`` came back as the
four-character string ``"null"``.  AutomationBench's ``api_fetch`` took
``body="null"`` 2,710 times in one run and its Drive listing failed on each
(``**None``); outside surogates the same model's ``null`` arrived as null.  The
registry now restores JSON null at dispatch, only for the exact string and only
where the parameter's own schema admits null.
"""
from __future__ import annotations

from typing import Any

import pytest

from surogates.tools.registry import ToolRegistry, ToolSchema
from surogates.tools.schema_sanitizer import restore_json_nulls, sanitize_tool_schemas

UNION = {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _api_fetch_schema() -> dict[str, Any]:
    """AutomationBench's ``api_fetch`` as its MCP server lists it."""
    return {
        "type": "object",
        "properties": {
            "method": {"type": "string"},
            "url": {"type": "string"},
            "params": {**UNION, "description": "JSON query parameters"},
            "body": {**UNION, "description": "JSON body"},
        },
        "required": ["method", "url", "params", "body"],
    }


def _sanitized(parameters: dict[str, Any]) -> dict[str, Any]:
    tool = {"type": "function", "function": {"name": "api_fetch", "parameters": parameters}}
    return sanitize_tool_schemas([tool])[0]["function"]["parameters"]


@pytest.mark.parametrize("schema", [_api_fetch_schema(), _sanitized(_api_fetch_schema())],
                         ids=["as-the-server-lists-it", "as-the-model-sees-it"])
def test_a_null_body_reaches_the_tool_as_null(schema):
    args = {"method": "GET", "url": "googleapis.com/drive/v3/files", "params": "null", "body": "null"}

    assert restore_json_nulls(args, schema) == {
        "method": "GET", "url": "googleapis.com/drive/v3/files", "params": None, "body": None,
    }


def test_the_sanitized_schema_is_the_one_the_model_reads_as_a_plain_string():
    body = _sanitized(_api_fetch_schema())["properties"]["body"]

    assert body["type"] == "string" and body["nullable"] is True


@pytest.mark.parametrize("prop", [
    {"type": "string", "nullable": True},
    {"type": ["string", "null"]},
    {"oneOf": [{"type": "string"}, {"type": "null"}]},
    {"type": "string", "enum": ["a", "b", None]},
])
def test_every_way_a_schema_admits_null_is_honoured(prop):
    schema = {"type": "object", "properties": {"x": prop}}

    assert restore_json_nulls({"x": "null"}, schema) == {"x": None}


def test_a_plain_string_parameter_keeps_the_string_null():
    schema = {"type": "object", "properties": {"note": {"type": "string"}, "body": UNION}}
    args = {"note": "null", "body": "null"}

    assert restore_json_nulls(args, schema) == {"note": "null", "body": None}


@pytest.mark.parametrize("value", ["NULL", "Null", "None", "none", " null", "null ", "", "nil"])
def test_only_the_exact_string_null_is_turned_into_null(value):
    schema = {"type": "object", "properties": {"body": UNION}}

    assert restore_json_nulls({"body": value}, schema) == {"body": value}


def test_an_argument_the_schema_does_not_name_is_left_alone():
    schema = {"type": "object", "properties": {"body": UNION}}

    assert restore_json_nulls({"extra": "null"}, schema) == {"extra": "null"}


def test_nested_objects_and_arrays_are_restored_where_their_schema_admits_null():
    schema = {
        "type": "object",
        "properties": {
            "filter": {"anyOf": [
                {"type": "object", "properties": {"owner": UNION, "label": {"type": "string"}}},
                {"type": "null"},
            ]},
            "tags": {"type": "array", "items": UNION},
        },
    }
    args = {"filter": {"owner": "null", "label": "null"}, "tags": ["a", "null"]}

    assert restore_json_nulls(args, schema) == {
        "filter": {"owner": None, "label": "null"}, "tags": ["a", None],
    }


def test_arguments_with_nothing_to_restore_are_returned_as_they_are():
    args = {"method": "GET", "url": "u", "params": '{"q": 1}', "body": None}

    assert restore_json_nulls(args, _api_fetch_schema()) is args


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    '{"method": "GET", "url": "u", "params": "null", "body": "null"}',
    {"method": "GET", "url": "u", "params": "null", "body": "null"},
], ids=["json-text", "parsed"])
async def test_dispatch_hands_the_tool_null(arguments):
    seen: list[dict[str, Any]] = []

    async def handler(args, **_kwargs):
        seen.append(args)
        return "ok"

    registry = ToolRegistry()
    registry.register(
        name="api_fetch",
        schema=ToolSchema(name="api_fetch", description="Fetch.", parameters=_sanitized(_api_fetch_schema())),
        handler=handler,
    )

    assert await registry.dispatch("api_fetch", arguments) == "ok"
    assert seen == [{"method": "GET", "url": "u", "params": None, "body": None}]
