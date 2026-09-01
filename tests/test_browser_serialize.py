"""Tests for surogates.browser.serialize."""

from __future__ import annotations

from typing import Any

from surogates.browser.serialize import MAX_MARKDOWN_NODES, render_markdown


def _state(tree: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    state = {
        "url": "https://example.com/",
        "title": "Example",
        "viewport": {"width": 1280, "height": 800},
        "tree": tree,
    }
    state.update(overrides)
    return state


class TestHeader:
    def test_emits_title_url_and_viewport(self) -> None:
        out = render_markdown(_state([]))
        assert out.splitlines()[:3] == [
            "# Example",
            "https://example.com/",
            "viewport 1280x800",
        ]

    def test_survives_missing_fields(self) -> None:
        out = render_markdown({"tree": []})
        assert out.splitlines()[:3] == ["# ", "", "viewport 0x0"]


class TestHeadings:
    def test_heading_level_maps_to_hashes(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e1", "role": "heading", "name": "Results",
             "x": 0, "y": 0, "heading_level": 2, "text_block": "Results"},
        ]))
        assert "## Results" in out

    def test_level_is_clamped_to_two_through_six(self) -> None:
        tree = [
            {"ref": "@e1", "role": "heading", "name": "Top", "x": 0, "y": 0,
             "heading_level": 1, "text_block": "Top"},
            {"ref": "@e2", "role": "heading", "name": "Deep", "x": 0, "y": 0,
             "heading_level": 9, "text_block": "Deep"},
        ]
        out = render_markdown(_state(tree))
        assert "## Top" in out
        assert "###### Deep" in out

    def test_missing_level_defaults_to_two(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e1", "role": "heading", "name": "H",
             "x": 0, "y": 0, "text_block": "H"},
        ]))
        assert "## H" in out


class TestInteractiveNodes:
    def test_rendered_as_ref_lines(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e7", "role": "button", "name": "Search", "x": 0, "y": 0},
        ]))
        assert '- button @e7 "Search"' in out

    def test_rendered_even_without_text_block(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e7", "role": "checkbox", "name": "Free cancellation",
             "x": 0, "y": 0, "text_block": ""},
        ]))
        assert '- checkbox @e7 "Free cancellation"' in out

    def test_unnamed_interactive_still_addressable(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e9", "role": "button", "name": "", "x": 0, "y": 0},
        ]))
        assert '- button @e9 ""' in out


class TestTextBlocks:
    def test_text_block_emitted_as_plain_line(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e3", "role": "paragraph", "name": "ignored",
             "x": 0, "y": 0, "text_block": "£128 per night"},
        ]))
        assert "£128 per night" in out
        assert "ignored" not in out

    def test_container_owning_no_text_contributes_nothing(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e2", "role": "generic", "name": "whole page text",
             "x": 0, "y": 0, "text_block": ""},
        ]))
        assert "whole page text" not in out

    def test_generic_without_text_block_key_contributes_nothing(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e2", "role": "generic", "name": "leaked", "x": 0, "y": 0},
        ]))
        assert "leaked" not in out


class TestMotivatingCases:
    """The two shapes that drove the text-block rule.

    Both use the node lists the snapshot script produces for the markup in the
    docstrings, so they pin the contract between derivation and rendering.
    """

    def test_price_split_across_spans_reads_as_one_line(self) -> None:
        # <div class="price"><span>£</span><span>128</span></div>
        # The div is a text block; both spans are covered by it.
        tree = [
            {"ref": "@e1", "role": "generic", "name": "£128",
             "x": 0, "y": 0, "text_block": "£128"},
            {"ref": "@e2", "role": "generic", "name": "£",
             "x": 0, "y": 0, "text_block": ""},
            {"ref": "@e3", "role": "generic", "name": "128",
             "x": 0, "y": 0, "text_block": ""},
        ]
        out = render_markdown(_state(tree))
        body = out.split("viewport 1280x800\n")[1]
        assert body.strip() == "£128"

    def test_sentence_around_a_link_keeps_both_parts(self) -> None:
        # <p>Read our <a href="…">privacy policy</a> for details</p>
        # The p is not a text block (interactive descendant), so it emits its
        # own text runs.  The a IS a text block (pure-inline subtree), so it
        # carries its text in text_block — but the serializer ignores
        # text_block for interactive roles and renders the control line.
        tree = [
            {"ref": "@e1", "role": "paragraph", "name": "Read our privacy policy for details",
             "x": 0, "y": 0, "text_block": "Read our for details"},
            {"ref": "@e2", "role": "link", "name": "privacy policy",
             "x": 0, "y": 0, "text_block": "privacy policy"},
        ]
        out = render_markdown(_state(tree))
        assert "Read our for details" in out
        assert '- link @e2 "privacy policy"' in out


class TestOrdering:
    def test_consent_priority_from_get_state_survives_rendering(self) -> None:
        # ``get_state`` runs ``_prioritize_state_entries`` before the serializer
        # sees the tree, so consent actions arrive already hoisted -- out of
        # ref order.  The serializer must not re-sort them back.
        tree = [
            {"ref": "@e9", "role": "button", "name": "Accept all",
             "x": 0, "y": 0, "intent": "accept_consent"},
            {"ref": "@e5", "role": "link", "name": "Home", "x": 0, "y": 0},
        ]
        out = render_markdown(_state(tree))
        assert out.index("@e9") < out.index("@e5")

    def test_otherwise_document_order_is_preserved(self) -> None:
        tree = [
            {"ref": "@e1", "role": "link", "name": "First", "x": 0, "y": 0},
            {"ref": "@e2", "role": "link", "name": "Second", "x": 0, "y": 0},
        ]
        out = render_markdown(_state(tree))
        assert out.index("@e1") < out.index("@e2")


class TestNodeCap:
    def test_caps_emitted_nodes_and_reports_truncation(self) -> None:
        tree = [
            {"ref": f"@e{i}", "role": "link", "name": f"L{i}", "x": 0, "y": 0}
            for i in range(1, MAX_MARKDOWN_NODES + 51)
        ]
        out = render_markdown(_state(tree))
        assert out.count('- link @e') == MAX_MARKDOWN_NODES
        assert f"[truncated: {MAX_MARKDOWN_NODES} of {MAX_MARKDOWN_NODES + 50} nodes shown" in out
        assert "browser_evaluate" in out

    def test_no_truncation_line_when_under_cap(self) -> None:
        out = render_markdown(_state([
            {"ref": "@e1", "role": "link", "name": "Only", "x": 0, "y": 0},
        ]))
        assert "truncated" not in out


class TestEmptyPage:
    def test_empty_tree_yields_header_only(self) -> None:
        out = render_markdown(_state([]))
        assert "no visible elements" in out


class TestRoleSetSync:
    def test_interactive_roles_match_the_client(self) -> None:
        from surogates.browser.client import KernelBrowserClient
        from surogates.browser.serialize import INTERACTIVE_ROLES

        # The serializer keeps its own copy so it need not import the HTTP
        # client.  A role in one set and not the other renders a control as
        # plain text with no @eN: the model can read it and cannot click it.
        assert INTERACTIVE_ROLES == KernelBrowserClient._INTERACTIVE_ROLES

    def test_option_role_renders_addressable(self) -> None:
        out = render_markdown(_state([
            {"role": "option", "ref": "@e1", "name": "Standard delivery"},
        ]))
        assert '- option @e1 "Standard delivery"' in out
