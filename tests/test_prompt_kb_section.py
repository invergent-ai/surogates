"""Tests for the mode-aware Knowledge Bases prompt section."""
from __future__ import annotations

from pathlib import Path
from uuid import UUID

from surogates.harness.prompt import PromptBuilder
from surogates.tenant.context import TenantContext


def _make_tenant(tmp_path: Path) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={
            "agent_name": "TestBot",
            "personality": "You are helpful.",
            "default_model": "gpt-4o",
        },
        user_preferences={},
        permissions=frozenset({"read"}),
        asset_root=str(tmp_path),
    )


GROUNDING_KB = {
    "id": "kb-g", "name": "platform-docs",
    "display_name": "Platform Docs",
    "description": "What Surogate does",
    "mode": "grounding",
    "pages_tree": "## index\n- `index.md` -- Index (1 KB)",
    "pages_total": 1,
}

REFERENCE_KB = {
    "id": "kb-r", "name": "extra-notes",
    "display_name": "Extra Notes",
    "description": "Optional notes",
    "mode": "reference",
    "pages_tree": "## summary\n- `notes.md` -- Notes (1 KB)",
    "pages_total": 1,
}


def test_no_kbs_renders_nothing(tmp_path: Path):
    builder = PromptBuilder(_make_tenant(tmp_path), available_kbs=[])
    assert builder._kb_section() == ""


def test_grounding_kb_gets_directive_header_and_tree(tmp_path: Path):
    builder = PromptBuilder(
        _make_tenant(tmp_path), available_kbs=[GROUNDING_KB],
    )
    section = builder._kb_section()
    assert "authoritative" in section.lower()
    assert "before answering" in section.lower()
    assert "do not answer from memory" in section.lower()
    assert "Platform Docs" in section
    assert "kb-g" in section
    assert "index.md" in section  # the ToC is rendered


def test_reference_kb_gets_soft_header_and_tree(tmp_path: Path):
    builder = PromptBuilder(
        _make_tenant(tmp_path), available_kbs=[REFERENCE_KB],
    )
    section = builder._kb_section()
    assert "when relevant" in section.lower()
    assert "authoritative" not in section.lower()
    assert "Extra Notes" in section
    assert "notes.md" in section


def test_mixed_modes_render_grounding_first(tmp_path: Path):
    builder = PromptBuilder(
        _make_tenant(tmp_path),
        available_kbs=[REFERENCE_KB, GROUNDING_KB],
    )
    section = builder._kb_section()
    assert section.index("Platform Docs") < section.index("Extra Notes")
    assert "authoritative" in section.lower()
    assert "when relevant" in section.lower()


def test_kb_dict_without_mode_defaults_to_grounding(tmp_path: Path):
    legacy = {
        "id": "kb-l", "name": "legacy", "display_name": "Legacy",
        "description": "",
    }
    builder = PromptBuilder(_make_tenant(tmp_path), available_kbs=[legacy])
    section = builder._kb_section()
    assert "authoritative" in section.lower()
    assert "Legacy" in section


def test_kb_section_appears_in_full_build(tmp_path: Path):
    builder = PromptBuilder(
        _make_tenant(tmp_path), available_kbs=[GROUNDING_KB],
    )
    prompt = builder.build()
    assert "Platform Docs" in prompt
    assert "kb_read_page" in prompt
