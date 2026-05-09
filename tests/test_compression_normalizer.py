"""Unit tests for ``ContextCompressor._normalize_plan_sections``.

These tests run without an LLM — they exercise the post-processor that
rewrites free-prose plan sections as numbered lists and reserves
indices observed in a previous summary.  Companion suite to the
live-LLM integration tests in
``tests/test_compression_with_real_llm.py``.

Covers:

  - free prose → indexed
  - already-indexed input preserved verbatim
  - column-0 bullets re-indexed
  - iterative compaction reserves previous indices
  - mixed indexed + bullets
  - non-plan sections untouched
  - status-tag de-duplication
  - indented sub-bullets pass through as continuations (NOT re-indexed)
  - multi-line prose continuations preserved
  - indented "N." lines not promoted to top-level items
  - tab-indented continuations
"""

from __future__ import annotations

from surogates.harness.context import ContextCompressor


def _norm(text: str, *, previous_summary: str | None = None) -> str:
    return ContextCompressor._normalize_plan_sections(
        text, previous_summary=previous_summary,
    )


def _top_level_indices(text: str) -> list[int]:
    """Return the indices of column-0 numbered items in ``text``."""
    out: list[int] = []
    for line in text.splitlines():
        if not line:
            continue
        if line[0].isdigit() and ". " in line[:6]:
            try:
                out.append(int(line.split(".", 1)[0]))
            except ValueError:
                pass
    return out


def test_free_prose_first_compaction_gets_indexed() -> None:
    free = (
        "### Done\n"
        "Built OIDC client.\n"
        "Wrote tests.\n"
        "\n"
        "### In Progress\n"
        "Currently fixing the auth bug; finished schema migration\n"
        "\n"
        "## Remaining Work\n"
        "Wire dispatch.\n"
    )
    out = _norm(free)
    assert "1. Built OIDC client. [Done]" in out
    assert "2. Wrote tests. [Done]" in out
    # The In Progress prose gets the next index (3) with the In-Progress tag.
    assert "3. Currently fixing the auth bug; finished schema migration [In Progress]" in out
    # Remaining Work has no status tag.
    assert "4. Wire dispatch." in out


def test_already_indexed_lines_are_preserved_verbatim() -> None:
    indexed = (
        "### Done\n"
        "1. Built OIDC client [Done]\n"
        "2. Wrote tests [Done]\n"
        "\n"
        "### In Progress\n"
        "3. Run integration tests [In Progress]\n"
    )
    out = _norm(indexed)
    assert _top_level_indices(out) == [1, 2, 3]
    assert "1. Built OIDC client [Done]" in out
    assert "2. Wrote tests [Done]" in out
    assert "3. Run integration tests [In Progress]" in out


def test_column_zero_bullets_get_reindexed() -> None:
    bullets = (
        "### Done\n"
        "- Built OIDC client\n"
        "- Wrote tests\n"
        "* Verified Auth0\n"
    )
    out = _norm(bullets)
    assert _top_level_indices(out) == [1, 2, 3]
    assert "1. Built OIDC client [Done]" in out
    assert "2. Wrote tests [Done]" in out
    assert "3. Verified Auth0 [Done]" in out


def test_iterative_compaction_reserves_previous_indices() -> None:
    prev = (
        "### Done\n"
        "1. Built OIDC client [Done]\n"
        "\n"
        "### In Progress\n"
        "2. Run integration tests [In Progress]\n"
        "\n"
        "## Remaining Work\n"
        "3. Wire dispatch\n"
        "4. Add cache invalidation\n"
    )
    new = "### In Progress\nA new step that lacks an index\n"
    out = _norm(new, previous_summary=prev)
    new_line = next(l for l in out.splitlines() if "new step" in l.lower())
    new_index = int(new_line.split(".", 1)[0])
    # Indices 1..4 were observed in the previous summary — new index must
    # be 5 or higher to avoid collision.
    assert new_index >= 5


def test_mixed_indexed_and_bullets_assigns_unused_index_to_bullet() -> None:
    mixed = (
        "### Done\n"
        "1. Built OIDC client\n"
        "- Wrote tests\n"
        "3. Verified Auth0\n"
    )
    out = _norm(mixed)
    bullet_line = next(l for l in out.splitlines() if "Wrote tests" in l)
    bullet_idx = int(bullet_line.split(".", 1)[0])
    assert bullet_idx not in (1, 3)
    # 1 and 3 must still appear unchanged.
    assert "1. Built OIDC client" in out
    assert "3. Verified Auth0" in out


def test_non_plan_sections_are_left_alone() -> None:
    other = (
        "## Key Decisions\n"
        "- Use PyJWT[crypto] over python-jose\n"
        "- Token expiry must be configurable\n"
    )
    out = _norm(other)
    # Non-plan bullets must stay as bullets, NOT be re-numbered.
    assert "- Use PyJWT[crypto] over python-jose" in out
    assert "- Token expiry must be configurable" in out
    assert _top_level_indices(out) == []


def test_status_tag_is_not_double_appended() -> None:
    already_tagged = "### Done\n- Built OIDC client [Done]\n"
    out = _norm(already_tagged)
    assert out.count("[Done]") == 1


def test_indented_sub_bullets_pass_through_as_continuations() -> None:
    """The bug this test guards: the normaliser was re-indexing every
    indented sub-bullet of a richly-described step into its own (likely
    colliding) index.  Indented lines should be treated as continuations
    of the item above them and pass through verbatim."""
    nested = (
        "### Done\n"
        "1. Build OIDC handler [Done]\n"
        "   - depends on: PyJWT, authlib\n"
        "   - tested with Auth0 staging\n"
        "2. Wire dispatch [Done]\n"
    )
    out = _norm(nested)
    assert _top_level_indices(out) == [1, 2]
    assert "1. Build OIDC handler [Done]" in out
    assert "   - depends on: PyJWT, authlib" in out
    assert "   - tested with Auth0 staging" in out
    assert "2. Wire dispatch [Done]" in out


def test_indented_prose_continuations_preserved() -> None:
    prose = (
        "### In Progress\n"
        "1. Implementing PKCE state-store [In Progress]\n"
        "   The Redis TTL is currently 600s but should match auth_code lifetime.\n"
        "   Wiring through the lifetime config now.\n"
        "2. Writing tests [In Progress]\n"
    )
    out = _norm(prose)
    assert _top_level_indices(out) == [1, 2]
    assert "   The Redis TTL is currently 600s but should match auth_code lifetime." in out
    assert "   Wiring through the lifetime config now." in out


def test_indented_index_like_lines_are_not_promoted_to_items() -> None:
    """Sub-content of an item may itself contain things that LOOK like
    indices (e.g. a numbered checklist describing a step).  These must
    NOT be promoted to top-level items."""
    fake = (
        "### Done\n"
        "1. Build runbook [Done]\n"
        "   2. configures the rollout flag\n"
        "   3. documents rollback\n"
    )
    out = _norm(fake)
    assert _top_level_indices(out) == [1]
    # But the indented lines themselves must still be present.
    assert "   2. configures the rollout flag" in out
    assert "   3. documents rollback" in out


def test_tab_indented_continuations_are_preserved() -> None:
    tab = (
        "### Done\n"
        "1. Build [Done]\n"
        "\t- with tab indent\n"
        "2. Test [Done]\n"
    )
    out = _norm(tab)
    assert _top_level_indices(out) == [1, 2]
    assert "\t- with tab indent" in out


def test_normalizer_failure_is_swallowed_at_caller_layer() -> None:
    """Sanity check that the normaliser itself is not swallowing
    exceptions — it must raise on truly bad input so the caller's
    fail-soft try/except can log and skip.  This exercises the edge
    where ``summary`` is None."""
    import pytest
    with pytest.raises(AttributeError):
        _norm(None)  # type: ignore[arg-type]
