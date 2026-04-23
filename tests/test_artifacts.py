"""Tests for the artifacts subsystem.

Covers the spec validators, :class:`ArtifactStore` persistence, and the
``create_artifact`` tool handler (routed through a stub API client).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from surogates.artifacts.models import (
    MAX_ARTIFACT_BYTES,
    MAX_TABLE_COLS,
    MAX_TABLE_ROWS,
    ArtifactKind,
    ArtifactSpec,
    ChartSpec,
    HtmlSpec,
    MarkdownSpec,
    SvgSpec,
    TableSpec,
)
from surogates.artifacts.store import (
    ArtifactLimitError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from surogates.storage.backend import LocalBackend
from surogates.tools.builtin.artifact import _create_artifact_handler
from surogates.harness.prompt import ARTIFACT_GUIDANCE, PromptBuilder
from surogates.tenant.context import TenantContext


# =========================================================================
# Spec validators
# =========================================================================


class TestArtifactSpec:
    """ArtifactSpec top-level parsing + name safety."""

    def test_name_must_not_contain_path_separators(self):
        with pytest.raises(ValidationError):
            ArtifactSpec(name="foo/bar", kind=ArtifactKind.MARKDOWN, spec={"content": "x"})

    def test_name_rejects_newlines_and_nulls(self):
        # Trailing whitespace is stripped; we only need to reject characters
        # that survive the trim.
        for bad in ("foo\nbar", "foo\x00bar", "foo\\bar", ".."):
            with pytest.raises(ValidationError):
                ArtifactSpec(name=bad, kind=ArtifactKind.MARKDOWN, spec={"content": "x"})

    def test_name_trimmed(self):
        spec = ArtifactSpec(
            name="  Revenue 2025  ", kind=ArtifactKind.MARKDOWN, spec={"content": "x"},
        )
        assert spec.name == "Revenue 2025"

    def test_validate_spec_accepts_each_kind(self):
        # Valid specs for each kind pass without raising.
        ArtifactSpec(
            name="x", kind=ArtifactKind.MARKDOWN, spec={"content": "hi"},
        ).validate_spec()
        ArtifactSpec(
            name="x", kind=ArtifactKind.TABLE,
            spec={"columns": ["a", "b"], "rows": [{"a": 1, "b": 2}]},
        ).validate_spec()
        ArtifactSpec(
            name="x", kind=ArtifactKind.CHART,
            spec={"vega_lite": {"mark": "bar", "data": {"values": [{"a": 1}]}}},
        ).validate_spec()
        ArtifactSpec(
            name="x", kind=ArtifactKind.HTML,
            spec={"html": "<!doctype html><p>hi</p>"},
        ).validate_spec()
        ArtifactSpec(
            name="x", kind=ArtifactKind.SVG,
            spec={"svg": "<svg viewBox='0 0 10 10'><circle cx='5' cy='5' r='4'/></svg>"},
        ).validate_spec()


class TestMarkdownSpec:
    def test_empty_content_rejected(self):
        with pytest.raises(ValidationError):
            MarkdownSpec(content="   ")

    def test_valid(self):
        assert MarkdownSpec(content="# Hello").content == "# Hello"


class TestTableSpec:
    def test_valid(self):
        s = TableSpec(
            columns=["name", "value"],
            rows=[{"name": "a", "value": 1}, {"name": "b", "value": 2}],
        )
        assert len(s.rows) == 2

    def test_duplicate_columns_rejected(self):
        with pytest.raises(ValidationError):
            TableSpec(columns=["a", "a"], rows=[])

    def test_row_limit_enforced(self):
        with pytest.raises(ValidationError):
            TableSpec(
                columns=["a"], rows=[{"a": i} for i in range(MAX_TABLE_ROWS + 1)],
            )

    def test_column_limit_enforced(self):
        with pytest.raises(ValidationError):
            TableSpec(
                columns=[f"c{i}" for i in range(MAX_TABLE_COLS + 1)], rows=[],
            )


class TestChartSpec:
    def test_inline_values_allowed(self):
        c = ChartSpec(vega_lite={"mark": "bar", "data": {"values": [{"a": 1}]}})
        assert c.vega_lite["mark"] == "bar"

    def test_data_url_rejected_at_top_level(self):
        with pytest.raises(ValidationError):
            ChartSpec(vega_lite={"mark": "bar", "data": {"url": "https://evil.example.com/data.json"}})

    def test_data_url_rejected_when_nested(self):
        # Layered spec: each layer has its own data block.  We must catch
        # URLs anywhere in the tree, not just at the top level.
        with pytest.raises(ValidationError):
            ChartSpec(vega_lite={
                "layer": [
                    {"mark": "bar", "data": {"values": [{"a": 1}]}},
                    {"mark": "point", "data": {"url": "https://evil.example.com/data.json"}},
                ],
            })


class TestHtmlSpec:
    def test_valid(self):
        h = HtmlSpec(html="<!doctype html><body><p>hello</p></body>")
        assert "<p>" in h.html

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            HtmlSpec(html="   ")

    def test_script_tags_preserved(self):
        # HtmlSpec does not sanitise — the iframe sandbox is the security
        # boundary, so scripts are passed through verbatim.
        body = "<html><body><script>alert(1)</script></body></html>"
        h = HtmlSpec(html=body)
        assert "<script>" in h.html


class TestSvgSpec:
    def test_valid(self):
        s = SvgSpec(svg="<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'/>")
        assert "<svg" in s.svg

    def test_empty_rejected(self):
        with pytest.raises(ValidationError):
            SvgSpec(svg="")

    def test_non_svg_rejected(self):
        with pytest.raises(ValidationError):
            SvgSpec(svg="<div>not an svg</div>")

    def test_trims_whitespace(self):
        s = SvgSpec(svg="\n\n  <svg viewBox='0 0 1 1'/>  \n")
        assert s.svg.startswith("<svg")


# =========================================================================
# ArtifactStore
# =========================================================================


@pytest.fixture
def backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(base_path=str(tmp_path))


@pytest.fixture
async def bucket(backend: LocalBackend) -> str:
    name = "session-abc"
    await backend.create_bucket(name)
    return name


@pytest.fixture
def session_id() -> UUID:
    return uuid4()


@pytest.fixture
def store(
    backend: LocalBackend, bucket: str, session_id: UUID,
) -> ArtifactStore:
    return ArtifactStore(backend, session_id=session_id, bucket=bucket)


class TestArtifactStoreCreate:
    async def test_creates_metadata_and_payload(self, store, backend, bucket):
        meta = await store.create(
            name="chart1",
            kind=ArtifactKind.MARKDOWN,
            spec={"content": "# Hello"},
        )
        assert meta.name == "chart1"
        assert meta.version == 1
        assert meta.size > 0
        # meta.json exists
        raw = await backend.read_text(bucket, f"_artifacts/{meta.artifact_id}/meta.json")
        assert "chart1" in raw
        # v1 payload exists and contains the spec
        payload_raw = await backend.read_text(
            bucket, f"_artifacts/{meta.artifact_id}/v1.json",
        )
        parsed = json.loads(payload_raw)
        assert parsed["kind"] == "markdown"
        assert parsed["spec"] == {"content": "# Hello"}

    async def test_assigns_distinct_ids(self, store):
        a = await store.create(
            name="a", kind=ArtifactKind.MARKDOWN, spec={"content": "x"},
        )
        b = await store.create(
            name="b", kind=ArtifactKind.MARKDOWN, spec={"content": "y"},
        )
        assert a.artifact_id != b.artifact_id

    async def test_index_maintained_in_creation_order(self, store):
        a = await store.create(
            name="first", kind=ArtifactKind.MARKDOWN, spec={"content": "1"},
        )
        b = await store.create(
            name="second", kind=ArtifactKind.MARKDOWN, spec={"content": "2"},
        )
        entries = await store.list()
        assert [e.name for e in entries] == ["first", "second"]
        assert [e.artifact_id for e in entries] == [a.artifact_id, b.artifact_id]

    async def test_rejects_oversized_payload(self, store):
        huge = "x" * (MAX_ARTIFACT_BYTES + 1)
        with pytest.raises(ArtifactLimitError):
            await store.create(
                name="big", kind=ArtifactKind.MARKDOWN, spec={"content": huge},
            )

    async def test_rejects_when_session_at_cap(
        self, backend, bucket, session_id, monkeypatch,
    ):
        # Seed the index with MAX entries so the next create hits the cap.
        from surogates.artifacts import models as m
        fake_index = [
            {
                "artifact_id": str(uuid4()),
                "session_id": str(session_id),
                "name": f"a{i}",
                "kind": "markdown",
                "version": 1,
                "size": 10,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            for i in range(m.MAX_ARTIFACTS_PER_SESSION)
        ]
        await backend.write_text(
            bucket, "_artifacts/index.json", json.dumps(fake_index),
        )
        store = ArtifactStore(backend, session_id=session_id, bucket=bucket)
        with pytest.raises(ArtifactLimitError):
            await store.create(
                name="one-too-many",
                kind=ArtifactKind.MARKDOWN,
                spec={"content": "x"},
            )


class TestArtifactStoreRead:
    async def test_get_meta_and_payload(self, store):
        created = await store.create(
            name="m", kind=ArtifactKind.TABLE,
            spec={"columns": ["a"], "rows": [{"a": 1}]},
        )
        fetched = await store.get_meta(created.artifact_id)
        assert fetched.artifact_id == created.artifact_id
        assert fetched.kind == ArtifactKind.TABLE

        payload = await store.get_payload(created.artifact_id)
        assert payload["kind"] == "table"
        assert payload["spec"]["columns"] == ["a"]

    async def test_missing_artifact_raises(self, store):
        with pytest.raises(ArtifactNotFoundError):
            await store.get_meta(uuid4())
        with pytest.raises(ArtifactNotFoundError):
            await store.get_payload(uuid4())

    async def test_list_empty_when_no_artifacts(self, store):
        assert await store.list() == []


# =========================================================================
# create_artifact tool handler
# =========================================================================


class _StubAPIClient:
    """Records calls so tests can assert on the forwarded payload."""

    def __init__(self, response: str = '{"success": true, "artifact_id": "abc"}'):
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def create_artifact(
        self, *, name: str, kind: str, spec: dict,
    ) -> str:
        self.calls.append({"name": name, "kind": kind, "spec": spec})
        return self.response


class TestCreateArtifactHandler:
    async def test_missing_api_client_returns_error(self):
        out = await _create_artifact_handler(
            {"name": "x", "kind": "markdown", "spec": {"content": "y"}},
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "API client" in data["error"]

    async def test_forwards_to_api_client(self):
        client = _StubAPIClient()
        args = {"name": "x", "kind": "chart", "spec": {"vega_lite": {}}}
        out = await _create_artifact_handler(args, api_client=client)
        assert json.loads(out)["success"] is True
        assert client.calls == [args]

    async def test_missing_name_or_kind_rejected_locally(self):
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {"kind": "markdown", "spec": {"content": "y"}}, api_client=client,
        )
        assert json.loads(out)["success"] is False
        assert client.calls == []

    async def test_missing_spec_entirely_returns_shape_hint(self):
        # LLM called the tool with only name+kind.  The handler must
        # catch this BEFORE the HTTP round-trip and return a shape
        # example the model can copy.
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {"name": "SOLID", "kind": "table"}, api_client=client,
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "spec.columns" in data["error"]
        assert "spec.rows" in data["error"]
        # The hint carries the exact shape to retry with.
        assert "spec" in data["hint"]
        assert "columns" in data["hint"]
        assert client.calls == []

    async def test_unknown_kind_rejected_locally(self):
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {"name": "x", "kind": "bogus", "spec": {}}, api_client=client,
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "Unknown kind" in data["error"]
        assert client.calls == []

    async def test_invalid_kind_spec_caught_locally_with_shape_hint(self):
        # Pass a spec for the wrong kind (chart without vega_lite).  The
        # handler should catch it via ArtifactSpec.validate_spec() and
        # return a focused message rather than bouncing to the API.
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {"name": "x", "kind": "chart", "spec": {"not_vega": "oops"}},
            api_client=client,
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "chart" in data["error"]
        assert "vega_lite" in data["hint"]
        # Crucially: never hit the API.
        assert client.calls == []

    def test_description_omits_concrete_json_examples(self):
        # Verbose JSON call-shape examples in the description cause
        # smaller models (observed with gpt-5.4-mini) to emit the JSON
        # as assistant text instead of invoking the tool.  The richer
        # schema carries the shape info; the description must not
        # duplicate it as literal JSON.
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.builtin import artifact as artifact_module

        registry = ToolRegistry()
        artifact_module.register(registry)
        desc = registry._entries["create_artifact"].schema.description
        # No concrete JSON braces in the description — shape info lives
        # in the parameter schema, not here.
        assert "{\"name\":" not in desc
        assert "{\"kind\":" not in desc
        assert "copy these shapes" not in desc.lower()
        # Positive: description should explicitly direct the model to
        # invoke the tool rather than describe the call.
        assert "invoke this tool" in desc.lower()

    def test_schema_exposes_per_kind_spec_properties(self):
        # The schema must name every possible property of `spec` with
        # per-kind REQUIRED markers so the LLM has structural guidance —
        # not just an opaque `{"type": "object"}`.  Regression guard for
        # the original "LLM omits spec entirely" bug.
        from surogates.tools.registry import ToolRegistry
        from surogates.tools.builtin import artifact as artifact_module

        registry = ToolRegistry()
        artifact_module.register(registry)
        entry = registry._entries["create_artifact"]
        spec_schema = entry.schema.parameters["properties"]["spec"]
        assert spec_schema["type"] == "object"
        props = spec_schema["properties"]
        # Every kind's required field must appear as a named property.
        for required_field in (
            "content", "columns", "rows", "vega_lite", "html", "svg",
        ):
            assert required_field in props, f"schema missing {required_field}"
            assert "REQUIRED" in props[required_field]["description"]
        # Caption is optional, not required.
        assert "caption" in props
        assert "optional" in props["caption"]["description"].lower()

    async def test_chart_data_url_blocked_locally(self):
        # Data-URL SSRF guard also lives in the local validator.
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {
                "name": "x", "kind": "chart",
                "spec": {"vega_lite": {
                    "mark": "bar",
                    "data": {"url": "https://evil.example/d.json"},
                }},
            },
            api_client=client,
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "data.url" in data["error"]
        assert client.calls == []  # did not forward

    async def test_non_object_spec_rejected(self):
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {"name": "x", "kind": "markdown", "spec": "not an object"},
            api_client=client,
        )
        assert json.loads(out)["success"] is False
        assert client.calls == []

    async def test_flattened_table_spec_returns_targeted_hint(self):
        # The LLM sometimes puts columns/rows at the top level alongside
        # name/kind instead of nesting under spec.  We catch this before
        # calling the API and return a hint that names the fix.
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {
                "name": "SOLID",
                "kind": "table",
                "columns": ["a", "b"],
                "rows": [{"a": 1, "b": 2}],
            },
            api_client=client,
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "nested under `spec`" in data["error"]
        assert "columns" in data["error"]
        assert "rows" in data["error"]
        assert client.calls == []  # did not forward to API

    async def test_flattened_chart_spec_hint(self):
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {
                "name": "Revenue",
                "kind": "chart",
                "vega_lite": {"mark": "bar"},
            },
            api_client=client,
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "vega_lite" in data["error"]
        assert client.calls == []

    async def test_flattened_html_spec_hint(self):
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {"name": "Demo", "kind": "html", "html": "<p>hi</p>"},
            api_client=client,
        )
        data = json.loads(out)
        assert data["success"] is False
        assert "html" in data["error"]
        assert client.calls == []

    async def test_proper_nesting_still_forwards(self):
        # Regression: valid nested shape must still reach the API.
        client = _StubAPIClient()
        out = await _create_artifact_handler(
            {
                "name": "SOLID",
                "kind": "table",
                "spec": {"columns": ["a"], "rows": [{"a": 1}]},
            },
            api_client=client,
        )
        assert json.loads(out)["success"] is True
        assert len(client.calls) == 1


# =========================================================================
# Fenced-artifact promoter
# =========================================================================


class TestFencePromoter:
    """Regex + kind mapping for auto-promoting ``` fences into artifacts."""

    def test_matches_svg_fence(self):
        from surogates.harness.loop import _FENCE_RE, _PROMOTABLE_FENCES
        content = "Here you go:\n\n```svg\n<svg viewBox='0 0 10 10'/>\n```\n"
        m = _FENCE_RE.search(content)
        assert m is not None
        assert m.group(1) == "svg"
        assert m.group(1) in _PROMOTABLE_FENCES
        assert "<svg" in m.group(2)

    def test_matches_html_fence(self):
        from surogates.harness.loop import _FENCE_RE, _PROMOTABLE_FENCES
        content = "```html\n<!doctype html><p>hi</p>\n```"
        m = _FENCE_RE.search(content)
        assert m is not None
        assert m.group(1) == "html"
        assert m.group(1) in _PROMOTABLE_FENCES

    def test_unrelated_code_fence_ignored(self):
        # python / ts / shell fences are NOT promotable.
        from surogates.harness.loop import _FENCE_RE, _PROMOTABLE_FENCES
        content = "```python\nprint('hi')\n```"
        m = _FENCE_RE.search(content)
        assert m is not None
        assert m.group(1) == "python"
        assert m.group(1) not in _PROMOTABLE_FENCES

    def test_derive_name_uses_last_user_message(self):
        from surogates.harness.loop import _derive_artifact_name
        messages = [
            {"role": "user", "content": "first prompt"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": '"Draw a minimal SVG logo for Steam & Bean"'},
        ]
        assert _derive_artifact_name("svg", messages) == (
            "Draw a minimal SVG logo for Steam & Bean"
        )

    def test_derive_name_falls_back_when_no_user(self):
        from surogates.harness.loop import _derive_artifact_name
        assert _derive_artifact_name("svg", []) == "SVG artifact"
        assert _derive_artifact_name("html", []) == "HTML preview"
        assert _derive_artifact_name("unknown", []) == "Artifact"

    def test_derive_name_truncates_long_prompt(self):
        from surogates.harness.loop import _derive_artifact_name
        long = "a" * 200
        assert len(_derive_artifact_name("svg", [
            {"role": "user", "content": long},
        ])) == 80


# =========================================================================
# Workspace hides internal artifact storage
# =========================================================================


class TestWorkspaceHidesArtifacts:
    """``artifacts/`` prefix is server-side storage, must not surface in the
    workspace file browser nor be readable/writable through its API."""

    def test_is_reserved_matches_artifacts_prefix(self):
        from surogates.api.routes.workspace import _is_reserved
        assert _is_reserved("_artifacts/abc/meta.json") is True
        assert _is_reserved("_artifacts/index.json") is True
        # A plain ``artifacts/`` path (no leading underscore) must NOT
        # match — the underscore-prefix is the whole point of the rename.
        assert _is_reserved("artifacts/abc.json") is False
        # Files that merely start with the string must not match.
        assert _is_reserved("_artifacts.md") is False
        assert _is_reserved("src/_artifacts/logo.svg") is False
        assert _is_reserved("notes.txt") is False

    def test_validate_path_blocks_reserved_prefix(self):
        from fastapi import HTTPException
        from surogates.api.routes.workspace import _validate_path
        with pytest.raises(HTTPException) as exc:
            _validate_path("_artifacts/foo/meta.json")
        assert exc.value.status_code == 403
        # Normal paths still pass, including an ``artifacts/`` folder
        # a user might legitimately have in their project.
        _validate_path("src/main.py")
        _validate_path("docs/README.md")
        _validate_path("artifacts/my-thing.png")


# =========================================================================
# PromptBuilder — ARTIFACT_GUIDANCE injection
# =========================================================================


class TestArtifactGuidance:
    """ARTIFACT_GUIDANCE is injected only when create_artifact is available."""

    @pytest.fixture
    def tenant(self) -> TenantContext:
        return TenantContext(
            org_id=uuid4(),
            user_id=uuid4(),
            org_config={"default_model": "gpt-4o"},
            user_preferences={},
            permissions=frozenset(),
            asset_root="/tmp/test_assets",
        )

    def test_guidance_injected_when_tool_available(self, tenant):
        pb = PromptBuilder(
            tenant=tenant,
            available_tools={"create_artifact", "memory"},
        )
        assert ARTIFACT_GUIDANCE in pb._tool_guidance_section()

    def test_guidance_not_injected_without_tool(self, tenant):
        pb = PromptBuilder(
            tenant=tenant,
            available_tools={"memory"},
        )
        assert ARTIFACT_GUIDANCE not in pb._tool_guidance_section()
