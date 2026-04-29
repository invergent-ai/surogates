"""Unit tests for the markdown chunker."""

from __future__ import annotations

from surogates.storage.chunker import Chunk, chunk_markdown


def test_empty_text_returns_empty_list():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n   ") == []


def test_no_headings_yields_single_chunk_with_none_path():
    text = "Just a paragraph of text without any headings."
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].heading_path is None
    assert chunks[0].chunk_index == 0


def test_single_heading_yields_breadcrumb():
    text = "# Intro\n\nFirst paragraph.\n\nSecond paragraph."
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].heading_path == "Intro"
    assert "First paragraph" in chunks[0].content
    assert "Second paragraph" in chunks[0].content


def test_nested_headings_build_breadcrumb():
    text = (
        "# Top\n\nTop body.\n\n"
        "## Middle\n\nMiddle body.\n\n"
        "### Deep\n\nDeep body."
    )
    chunks = chunk_markdown(text)
    paths = [c.heading_path for c in chunks]
    assert paths == ["Top", "Top > Middle", "Top > Middle > Deep"]


def test_heading_at_same_level_resets_branch():
    """Two h1s in sequence → second is its own top-level branch, not nested."""
    text = "# A\n\nA body.\n\n# B\n\nB body."
    chunks = chunk_markdown(text)
    paths = [c.heading_path for c in chunks]
    assert paths == ["A", "B"]


def test_heading_pop_to_lower_level():
    text = (
        "# Top\n\nTop body.\n\n"
        "## Sub\n\nSub body.\n\n"
        "# Other\n\nOther body."
    )
    paths = [c.heading_path for c in chunk_markdown(text)]
    assert paths == ["Top", "Top > Sub", "Other"]


def test_pre_text_before_first_heading_gets_none_path():
    text = "Preamble paragraph.\n\n# Section\n\nSection body."
    chunks = chunk_markdown(text)
    assert chunks[0].heading_path is None
    assert chunks[0].content == "Preamble paragraph."
    assert chunks[1].heading_path == "Section"


def test_oversized_section_splits_with_overlap():
    body = "x" * 6000
    text = f"# Big\n\n{body}"
    chunks = chunk_markdown(text, max_chars=2000, overlap=200)
    # Body length 6000, max 2000, overlap 200 → step 1800 → 4 chunks
    assert len(chunks) == 4
    # All chunks belong to the 'Big' section.
    assert all(c.heading_path == "Big" for c in chunks)
    # Each chunk fits within the size bound.
    for c in chunks:
        assert len(c.content) <= 2000
    # Consecutive chunks share the overlap region (last 200 of prev ==
    # first 200 of next).
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev.content[-200:] == nxt.content[:200]


def test_chunk_indices_are_sequential():
    text = "# A\n\nA.\n\n# B\n\nB.\n\n# C\n\nC."
    chunks = chunk_markdown(text)
    assert [c.chunk_index for c in chunks] == [0, 1, 2]


def test_empty_section_is_skipped():
    """A heading with no body (followed immediately by another heading)
    should not produce a chunk."""
    text = "# Empty\n\n# Filled\n\nBody."
    chunks = chunk_markdown(text)
    paths = [c.heading_path for c in chunks]
    assert paths == ["Filled"]
