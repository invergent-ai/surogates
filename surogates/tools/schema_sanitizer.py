"""Sanitize tool JSON schemas for broad LLM backend compatibility."""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)

_TOP_LEVEL_FORBIDDEN_KEYS = ("allOf", "anyOf", "oneOf", "enum", "not")


def sanitize_tool_schemas(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a sanitized deep copy of OpenAI-format tool schemas."""
    if not tools:
        return tools
    return [_sanitize_single_tool(tool) for tool in tools]


def _sanitize_single_tool(tool: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(tool)
    fn = out.get("function") if isinstance(out, dict) else None
    if not isinstance(fn, dict):
        return out

    params = fn.get("parameters")
    if not isinstance(params, dict):
        fn["parameters"] = {"type": "object", "properties": {}}
        return out

    fn["parameters"] = _sanitize_node(params, path=fn.get("name", "<tool>"))
    top = fn["parameters"]
    if not isinstance(top, dict):
        fn["parameters"] = {"type": "object", "properties": {}}
    else:
        if top.get("type") != "object":
            top["type"] = "object"
        if not isinstance(top.get("properties"), dict):
            top["properties"] = {}

    fn["parameters"] = strip_nullable_unions(
        fn["parameters"],
        keep_nullable_hint=True,
    )
    fn["parameters"] = _strip_top_level_combinators(
        fn["parameters"],
        path=fn.get("name", "<tool>"),
    )
    return out


def strip_nullable_unions(
    schema: Any,
    *,
    keep_nullable_hint: bool = True,
) -> Any:
    """Collapse nullable ``anyOf`` / ``oneOf`` unions to the non-null branch."""
    if isinstance(schema, list):
        return [
            strip_nullable_unions(item, keep_nullable_hint=keep_nullable_hint)
            for item in schema
        ]
    if not isinstance(schema, dict):
        return schema

    stripped = {
        key: strip_nullable_unions(value, keep_nullable_hint=keep_nullable_hint)
        for key, value in schema.items()
    }
    for key in ("anyOf", "oneOf"):
        variants = stripped.get(key)
        if not isinstance(variants, list):
            continue
        non_null = [
            item
            for item in variants
            if not (isinstance(item, dict) and item.get("type") == "null")
        ]
        if len(non_null) == 1 and len(non_null) != len(variants):
            replacement = dict(non_null[0]) if isinstance(non_null[0], dict) else {}
            if keep_nullable_hint:
                replacement.setdefault("nullable", True)
            for meta_key in ("title", "description", "default", "examples"):
                if meta_key in stripped and meta_key not in replacement:
                    replacement[meta_key] = stripped[meta_key]
            return strip_nullable_unions(
                replacement,
                keep_nullable_hint=keep_nullable_hint,
            )
    return stripped


def _sanitize_node(node: Any, path: str) -> Any:
    if isinstance(node, str):
        if node in {"object", "string", "number", "integer", "boolean", "array", "null"}:
            if node == "object":
                return {"type": "object", "properties": {}}
            return {"type": node}
        logger.debug(
            "schema_sanitizer[%s]: replacing non-schema string %r",
            path,
            node,
        )
        return {"type": "object", "properties": {}}

    if isinstance(node, list):
        return [_sanitize_node(item, f"{path}[{idx}]") for idx, item in enumerate(node)]

    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, list):
            non_null = [item for item in value if item != "null"]
            if len(non_null) == 1 and isinstance(non_null[0], str):
                out["type"] = non_null[0]
                if "null" in value:
                    out.setdefault("nullable", True)
                continue
            first_type = next(
                (item for item in value if isinstance(item, str) and item != "null"),
                None,
            )
            out["type"] = first_type or "object"
            continue

        if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
            out[key] = {
                sub_key: _sanitize_node(sub_value, f"{path}.{key}.{sub_key}")
                for sub_key, sub_value in value.items()
            }
        elif key in {"items", "additionalProperties"}:
            out[key] = (
                value
                if isinstance(value, bool)
                else _sanitize_node(value, f"{path}.{key}")
            )
        elif key in {"anyOf", "oneOf", "allOf"} and isinstance(value, list):
            out[key] = [
                _sanitize_node(item, f"{path}.{key}[{idx}]")
                for idx, item in enumerate(value)
            ]
        elif key in {"required", "enum", "examples"}:
            out[key] = copy.deepcopy(value) if isinstance(value, (list, dict)) else value
        else:
            out[key] = (
                _sanitize_node(value, f"{path}.{key}")
                if isinstance(value, (dict, list))
                else value
            )

    if out.get("type") == "object" and not isinstance(out.get("properties"), dict):
        out["properties"] = {}

    if out.get("type") == "object" and isinstance(out.get("required"), list):
        props = out.get("properties") or {}
        valid = [item for item in out["required"] if isinstance(item, str) and item in props]
        if not valid:
            out.pop("required", None)
        elif len(valid) != len(out["required"]):
            out["required"] = valid

    return out


def _strip_top_level_combinators(
    params: dict[str, Any],
    *,
    path: str = "<tool>",
) -> dict[str, Any]:
    if not isinstance(params, dict):
        return params
    out = dict(params)
    for key in _TOP_LEVEL_FORBIDDEN_KEYS:
        if key in out:
            logger.debug(
                "schema_sanitizer[%s]: stripped top-level %s",
                path,
                key,
            )
            out.pop(key, None)
    return out
