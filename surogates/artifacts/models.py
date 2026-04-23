"""Artifact data shapes and kind-specific spec validators.

An artifact is a named, kind-typed blob produced by the ``create_artifact``
tool.  Three kinds are supported at MVP:

- ``markdown`` — arbitrary GitHub-flavoured markdown rendered in-thread
- ``table``   — row/column data with optional column hints
- ``chart``   — a Vega-Lite spec with inline data only

Each kind parses/validates its spec here before the artifact is persisted,
so the API layer can reject malformed requests without touching storage.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

# Hard cap on the size of a single artifact payload (serialised spec).
# Anything larger should be a workspace file, not an artifact.
MAX_ARTIFACT_BYTES = 512_000  # 500 KB

# Hard cap on the number of artifacts per session.  Prevents a runaway
# loop from flooding the bucket and the event log.
MAX_ARTIFACTS_PER_SESSION = 200

# Max artifact name length (for display and key safety).
MAX_NAME_LEN = 120

# Max rows / columns for table artifacts.
MAX_TABLE_ROWS = 2_000
MAX_TABLE_COLS = 50


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------


class ArtifactKind(str, Enum):
    """Rendering kind.  The frontend picks a renderer by kind."""

    MARKDOWN = "markdown"
    TABLE = "table"
    CHART = "chart"
    HTML = "html"
    SVG = "svg"


# ---------------------------------------------------------------------------
# Kind-specific specs
# ---------------------------------------------------------------------------


class MarkdownSpec(BaseModel):
    """Markdown body rendered with the chat's existing markdown pipeline."""

    content: str

    @field_validator("content")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("markdown content must not be empty")
        return v


class TableSpec(BaseModel):
    """Tabular data with ordered columns and row objects.

    ``columns`` ordering determines display order; each ``row`` is a
    mapping keyed by column name.  Unknown keys in rows are dropped by
    the renderer — we keep them in storage for round-trip fidelity.
    """

    columns: list[str] = Field(..., min_length=1)
    rows: list[dict[str, Any]]
    caption: str | None = None

    @field_validator("columns")
    @classmethod
    def _columns_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("table columns must be unique")
        if len(v) > MAX_TABLE_COLS:
            raise ValueError(f"table cannot have more than {MAX_TABLE_COLS} columns")
        return v

    @field_validator("rows")
    @classmethod
    def _rows_bounded(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(v) > MAX_TABLE_ROWS:
            raise ValueError(f"table cannot have more than {MAX_TABLE_ROWS} rows")
        return v


class ChartSpec(BaseModel):
    """A Vega-Lite spec.  Inline data only — no external URL loading.

    We do not validate against the full Vega-Lite JSON schema here (too
    large to pull in); the frontend's ``react-vega`` renderer reports a
    clear error if the spec is malformed.  We *do* reject specs that
    reference external data sources, which is the main SSRF vector.
    """

    vega_lite: dict[str, Any]
    caption: str | None = None

    @field_validator("vega_lite")
    @classmethod
    def _no_external_data(cls, v: dict[str, Any]) -> dict[str, Any]:
        data = v.get("data")
        if isinstance(data, dict) and "url" in data:
            raise ValueError(
                "chart data.url is not allowed — provide inline data.values instead"
            )
        # Reject any nested data blocks that reference external URLs (e.g.
        # inside layered/faceted specs).
        _reject_urls(v)
        return v


def _reject_urls(node: Any) -> None:
    """Walk a Vega-Lite spec and raise if any ``data`` block references a URL."""
    if isinstance(node, dict):
        data = node.get("data")
        if isinstance(data, dict) and "url" in data:
            raise ValueError(
                "chart data.url is not allowed — provide inline data.values instead"
            )
        for value in node.values():
            _reject_urls(value)
    elif isinstance(node, list):
        for item in node:
            _reject_urls(item)


class HtmlSpec(BaseModel):
    """A self-contained HTML document rendered inside a sandboxed iframe.

    The document is rendered with ``<iframe sandbox="allow-scripts">`` —
    no same-origin, no forms, no top-level navigation.  Scripts run but
    cannot reach parent frame state, cookies, or storage.  The iframe
    sandbox is the load-bearing security boundary; we do not attempt to
    sanitise the HTML itself.
    """

    html: str
    caption: str | None = None

    @field_validator("html")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("html content must not be empty")
        return v


class SvgSpec(BaseModel):
    """An inline SVG document.

    Rendered via ``<img src="data:image/svg+xml;base64,...">`` — browsers
    do not execute scripts inside SVGs loaded through ``<img>``, which
    is what makes this safe without an explicit sanitisation step.
    """

    svg: str
    caption: str | None = None

    @field_validator("svg")
    @classmethod
    def _looks_like_svg(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("svg content must not be empty")
        lower = stripped.lower()
        # Cheap structural check so we catch obvious mistakes early; the
        # browser's parser will reject malformed SVG at render time.
        if "<svg" not in lower:
            raise ValueError("svg content must contain an <svg> element")
        return stripped


# ---------------------------------------------------------------------------
# Top-level spec
# ---------------------------------------------------------------------------


class ArtifactSpec(BaseModel):
    """The payload the LLM produces via ``create_artifact``.

    Validated on the API side before persistence.  ``spec`` is a union
    discriminated by ``kind``; we parse it into the matching kind-specific
    model to surface precise error messages.
    """

    name: str = Field(..., min_length=1, max_length=MAX_NAME_LEN)
    kind: ArtifactKind
    spec: dict[str, Any]

    @field_validator("name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be empty")
        # Reject path-separator characters so a name can't escape the
        # artifact's directory when used in storage keys.
        if any(ch in v for ch in ("/", "\\", "..", "\x00", "\n", "\r")):
            raise ValueError("name contains invalid characters")
        return v

    def validate_spec(self) -> None:
        """Parse ``spec`` against the kind's validator.

        Raises ``pydantic.ValidationError`` on malformed input.
        """
        match self.kind:
            case ArtifactKind.MARKDOWN:
                MarkdownSpec(**self.spec)
            case ArtifactKind.TABLE:
                TableSpec(**self.spec)
            case ArtifactKind.CHART:
                ChartSpec(**self.spec)
            case ArtifactKind.HTML:
                HtmlSpec(**self.spec)
            case ArtifactKind.SVG:
                SvgSpec(**self.spec)


# ---------------------------------------------------------------------------
# Metadata (return shape for the list/fetch endpoints)
# ---------------------------------------------------------------------------


class ArtifactMeta(BaseModel):
    """Artifact metadata as it travels over the wire and on the event log."""

    artifact_id: UUID
    session_id: UUID
    name: str
    kind: ArtifactKind
    version: int
    size: int
    created_at: datetime

    @classmethod
    def new(
        cls,
        *,
        artifact_id: UUID,
        session_id: UUID,
        name: str,
        kind: ArtifactKind,
        version: int,
        size: int,
    ) -> ArtifactMeta:
        """Build a fresh metadata record with ``created_at`` set to now."""
        return cls(
            artifact_id=artifact_id,
            session_id=session_id,
            name=name,
            kind=kind,
            version=version,
            size=size,
            created_at=datetime.now(timezone.utc),
        )
