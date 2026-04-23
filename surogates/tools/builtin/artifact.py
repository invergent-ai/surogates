"""Builtin ``create_artifact`` tool.

Routes through :class:`HarnessAPIClient` because artifacts live in the
session bucket, which is tenant-gated resource the worker accesses via
the API server.  Same wiring as the ``memory`` and ``skills`` tools.

Handler-side validation: we parse the spec locally with
:class:`ArtifactSpec` before the HTTP round-trip.  Catching malformed
arguments here means the LLM gets a compact, actionable error message
scoped to the exact missing/misplaced field, instead of the verbose
pydantic JSON dump the API would otherwise return.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from surogates.artifacts.models import ArtifactKind, ArtifactSpec
from surogates.tools.registry import ToolRegistry, ToolSchema


def register(registry: ToolRegistry) -> None:
    """Register the ``create_artifact`` tool."""

    registry.register(
        name="create_artifact",
        schema=ToolSchema(
            name="create_artifact",
            description=(
                "Render an inline artifact in the chat: a chart, table, "
                "standalone markdown document, interactive HTML preview, "
                "or SVG image. Invoke this tool — do NOT produce the JSON "
                "as part of your response text. See the parameter schema "
                "for the exact shape.\n\n"
                "Kinds:\n"
                "- markdown: standalone markdown document (reports, specs, "
                "study guides).\n"
                "- table: tabular data; rows are objects keyed by column.\n"
                "- chart: a Vega-Lite spec with inline data "
                "(`data.values`, never `data.url`).\n"
                "- html: self-contained HTML rendered in a sandboxed "
                "iframe. Scripts run but cannot reach the parent page. "
                "Use for interactive demos, widgets, single-page tools.\n"
                "- svg: standalone SVG image. Scripts inside SVG do not "
                "execute.\n\n"
                "Use for: visuals, interactive previews, or long "
                "standalone documents the user will want to see, copy, "
                "save, or refer back to.\n\n"
                "Do NOT use for: short answers, files the user is editing "
                "on disk (use write_file instead), or data the user asked "
                "for as raw JSON/CSV (return as a code block in the "
                "message).\n\n"
                "create_artifact vs write_file: intent decides, not "
                "wording. 'Single file' does NOT mean disk. If the user "
                "wants to see and interact with the output in this chat "
                "(a calculator to click, a widget to try), this tool. If "
                "the user is editing a project on disk, write_file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Short, descriptive title for the artifact "
                            "(shown as the card header). 1-120 characters."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["markdown", "table", "chart", "html", "svg"],
                        "description": (
                            "Rendering kind. Determines which fields of "
                            "`spec` are required."
                        ),
                    },
                    "spec": {
                        "type": "object",
                        "description": (
                            "The artifact's content. Include ONLY the "
                            "fields required for the declared `kind` "
                            "(listed below). `caption` is optional for "
                            "table, chart, html, and svg."
                        ),
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": (
                                    "REQUIRED when kind='markdown'. The "
                                    "full markdown body of the document."
                                ),
                            },
                            "columns": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "REQUIRED when kind='table'. Ordered "
                                    "list of column names."
                                ),
                            },
                            "rows": {
                                "type": "array",
                                "items": {"type": "object"},
                                "description": (
                                    "REQUIRED when kind='table'. Each row "
                                    "is an object keyed by column name."
                                ),
                            },
                            "vega_lite": {
                                "type": "object",
                                "description": (
                                    "REQUIRED when kind='chart'. A complete "
                                    "Vega-Lite spec with inline "
                                    "`data.values` (data.url is blocked)."
                                ),
                            },
                            "html": {
                                "type": "string",
                                "description": (
                                    "REQUIRED when kind='html'. A complete, "
                                    "self-contained HTML document "
                                    "(<!doctype html>...). Rendered in a "
                                    "sandboxed iframe."
                                ),
                            },
                            "svg": {
                                "type": "string",
                                "description": (
                                    "REQUIRED when kind='svg'. A complete "
                                    "<svg>...</svg> element."
                                ),
                            },
                            "caption": {
                                "type": "string",
                                "description": (
                                    "Optional short caption for table, "
                                    "chart, html, or svg kinds."
                                ),
                            },
                        },
                    },
                },
                "required": ["name", "kind", "spec"],
            },
        ),
        handler=_create_artifact_handler,
        toolset="artifact",
    )



# Keys that belong inside ``spec`` per kind.  When the LLM flattens one
# of these to the top level (alongside ``name``/``kind``) instead of
# nesting under ``spec``, we catch it with a focused error rather than
# letting pydantic emit a generic "field required" message.
_SPEC_KEYS_BY_KIND: dict[str, tuple[str, ...]] = {
    "markdown": ("content",),
    "table": ("columns", "rows"),
    "chart": ("vega_lite",),
    "html": ("html",),
    "svg": ("svg",),
}

# Minimal, copy-pasteable shape per kind — emitted in error messages so
# the LLM's retry has the exact structure baked in.
_SHAPE_EXAMPLE_BY_KIND: dict[str, str] = {
    "markdown": (
        '{"name": "…", "kind": "markdown", '
        '"spec": {"content": "# heading\\n\\nbody"}}'
    ),
    "table": (
        '{"name": "…", "kind": "table", '
        '"spec": {"columns": ["col1", "col2"], '
        '"rows": [{"col1": "…", "col2": "…"}]}}'
    ),
    "chart": (
        '{"name": "…", "kind": "chart", '
        '"spec": {"vega_lite": {"mark": "bar", '
        '"data": {"values": [{"x": 1, "y": 2}]}, '
        '"encoding": {"x": {"field": "x"}, "y": {"field": "y"}}}}}'
    ),
    "html": (
        '{"name": "…", "kind": "html", '
        '"spec": {"html": "<!doctype html><body>…</body>"}}'
    ),
    "svg": (
        '{"name": "…", "kind": "svg", '
        '"spec": {"svg": "<svg viewBox=\'0 0 10 10\'>…</svg>"}}'
    ),
}


def _error(message: str, *, hint: str | None = None) -> str:
    """Format a compact, LLM-friendly error response."""
    body: dict[str, Any] = {"success": False, "error": message}
    if hint:
        body["hint"] = hint
    return json.dumps(body, ensure_ascii=False)


def _format_validation_error(exc: ValidationError, kind: str) -> str:
    """Turn a pydantic ValidationError into a terse, actionable message.

    Pydantic's default dump is a list of error dicts with URLs and
    echoes of the bad input — too noisy for a retry-guiding tool
    result.  We keep only the field path and message, then append the
    correct shape for the declared kind.
    """
    issues: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "invalid")
        issues.append(f"{loc}: {msg}" if loc else msg)
    summary = "; ".join(issues) or "invalid spec"
    shape = _SHAPE_EXAMPLE_BY_KIND.get(kind)
    return _error(
        f"Invalid spec for kind '{kind}': {summary}.",
        hint=f"Expected shape: {shape}" if shape else None,
    )


async def _create_artifact_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Validate locally, then delegate to the session-scoped API client."""
    api_client = kwargs.get("api_client")
    if api_client is None:
        return _error(
            "Artifacts require an API client; none is wired for this "
            "runtime (likely a unit-test harness).",
        )

    name = arguments.get("name", "")
    kind = arguments.get("kind", "")
    spec = arguments.get("spec")

    if not name or not kind:
        return _error("name and kind are required.")

    if kind not in _SPEC_KEYS_BY_KIND:
        valid = ", ".join(sorted(_SPEC_KEYS_BY_KIND))
        return _error(f"Unknown kind '{kind}'. Valid kinds: {valid}.")

    expected_keys = _SPEC_KEYS_BY_KIND[kind]

    # Guard 1 — spec missing entirely or not an object.  The LLM
    # sometimes calls the tool with only name+kind, expecting to supply
    # content later.  Not supported: one call must carry the full spec.
    if not isinstance(spec, dict) or not spec:
        misplaced = [k for k in expected_keys if k in arguments]
        if misplaced:
            # Flattened spec — keys ended up at the top level.
            joined = ", ".join(misplaced)
            return _error(
                f"Keys {joined} must be nested under `spec`, not at the "
                f"top level.",
                hint=f"Expected shape: {_SHAPE_EXAMPLE_BY_KIND[kind]}",
            )
        # Truly missing spec.
        required = ", ".join(f"spec.{k}" for k in expected_keys)
        return _error(
            f"Missing required field(s): {required}. The artifact must "
            f"be created in a single call with its full content.",
            hint=f"Expected shape: {_SHAPE_EXAMPLE_BY_KIND[kind]}",
        )

    # Guard 2 — kind-specific validators.  Parse locally so the retry
    # message is scoped to the exact failing field, without a wasted
    # HTTP round-trip.
    try:
        validated = ArtifactSpec(name=name, kind=ArtifactKind(kind), spec=spec)
        validated.validate_spec()
    except ValidationError as exc:
        return _format_validation_error(exc, kind)
    except ValueError as exc:
        return _error(str(exc), hint=f"Expected shape: {_SHAPE_EXAMPLE_BY_KIND[kind]}")

    return await api_client.create_artifact(name=name, kind=kind, spec=spec)
