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
    assert "before deciding" in section.lower()
    assert "own knowledge" in section.lower()
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


def test_citation_contract_is_grounding_only(tmp_path: Path):
    """Quote-your-evidence belongs to the authoritative level only.

    Reference-level answers may legitimately come from the model's own
    knowledge, so demanding a page citation for every claim there would
    push it toward declining -- the exact failure that level exists to
    avoid. Asserted in both directions because leaking the contract into
    the reference block is silent: the prompt still renders fine.
    """
    grounded = PromptBuilder(
        _make_tenant(tmp_path), available_kbs=[GROUNDING_KB],
    )._kb_section()
    assert "Quote verbatim" in grounded

    referenced = PromptBuilder(
        _make_tenant(tmp_path), available_kbs=[REFERENCE_KB],
    )._kb_section()
    assert "Quote verbatim" not in referenced


# --- grounding_nocite -----------------------------------------------------
#
# Some deployments want a clean prose answer with no inline quotations. That
# is a presentation choice, and it must not silently become a grounding
# choice: the KB stays authoritative and an uncovered question is still
# declined. Requiring citations was measured at 0.94 -> 0.96 grounded-answer
# accuracy (inside run-to-run spread), so what is traded away is
# verifiability, not correctness.

NOCITE_KB = {
    "id": "kb-nocite", "name": "eurolife", "display_name": "Eurolife",
    "description": "Insurance conditions", "mode": "grounding_nocite",
}


def _section(tmp_path, kbs):
    return PromptBuilder(_make_tenant(tmp_path), available_kbs=kbs)._kb_section()


def test_nocite_drops_the_quote_contract(tmp_path):
    out = _section(tmp_path, [NOCITE_KB])
    assert "Quote verbatim" not in out
    assert "quoted words from the page" not in out
    assert "without inline citations" in out


def test_nocite_is_still_authoritative_and_still_declines(tmp_path):
    out = _section(tmp_path, [NOCITE_KB])
    # Listed as authoritative, not as optional reference material.
    assert "## Authoritative (consult first)" in out
    assert "Reference (consult when relevant)" not in out
    # The decline instruction and the anti-over-decline counterweight both
    # survive -- dropping either is the measured regression.
    assert "does not address the question" in out
    assert "do not reach for that too quickly" in out
    # And grounding itself is unchanged.
    assert "must still come from a page you actually read" in out


def test_grounding_still_cites(tmp_path):
    out = _section(tmp_path, [GROUNDING_KB])
    assert "Quote verbatim" in out


def test_one_citing_kb_keeps_citations_for_the_turn(tmp_path):
    # Mixed attachment: the contract is per-turn, not per-page, and the
    # agent cannot cite one KB while silently not citing another. Presence
    # of any citing KB keeps the contract on.
    out = _section(tmp_path, [NOCITE_KB, GROUNDING_KB])
    assert "Quote verbatim" in out
    assert "without inline citations" not in out
