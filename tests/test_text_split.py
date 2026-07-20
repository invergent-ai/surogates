"""Unit tests for the shared outbound message splitter."""

import pytest

from surogates.channels.text_split import split_text


def test_short_text_unchanged():
    assert split_text("hello", 100) == ["hello"]


def test_empty_text_yields_no_chunks():
    assert split_text("", 100) == []


def test_invalid_limit_raises():
    with pytest.raises(ValueError):
        split_text("x", 0)


def test_every_chunk_within_limit():
    text = ("word " * 500).strip()
    for chunk in split_text(text, 80):
        assert 0 < len(chunk) <= 80


def test_prefers_paragraph_boundary():
    text = "para one is here.\n\npara two follows and is fairly long too."
    chunks = split_text(text, 40)
    assert chunks[0] == "para one is here."


def test_prefers_line_boundary_over_word():
    text = "line one goes here\nline two follows here"
    chunks = split_text(text, 25)
    assert chunks[0] == "line one goes here"
    assert chunks[1] == "line two follows here"


def test_hard_cut_for_unbroken_run():
    text = "a" * 250
    chunks = split_text(text, 100)
    assert chunks == ["a" * 100, "a" * 100, "a" * 50]


def test_no_content_lost():
    text = "The quick brown fox jumps over the lazy dog. " * 40
    chunks = split_text(text.strip(), 90)
    reassembled = " ".join(chunks).split()
    assert reassembled == text.strip().split()


def test_no_empty_chunks_on_whitespace_runs():
    text = ("x" * 50) + "\n\n\n\n" + ("y" * 50)
    chunks = split_text(text, 52)
    assert all(c.strip() for c in chunks)
    assert "".join(chunks).count("x") == 50
    assert "".join(chunks).count("y") == 50
