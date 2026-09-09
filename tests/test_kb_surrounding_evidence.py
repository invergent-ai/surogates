"""Expansion keeps exact source coordinates and obeys its reading budget."""

import json

import pytest

from surogates.tools.builtin.kb_evidence import (
    CONTEXT_LIMIT_MAX,
    render_surrounding,
    surrounding_spans,
)


def pdf(pages):
    return json.dumps([{"page": n, "content": body} for n, body in pages]).encode()


def test_table_header_and_following_clause_are_returned_verbatim():
    raw = pdf(
        [
            (1, "Excluded risks:"),
            (2, "Table columns\n" + "row\n" * 50),
            (3, "Continued exclusion: freezing."),
        ]
    )
    spans = surrounding_spans(raw, page_number=2, start=80, end=100, budget=500)
    assert [s.page_number for s in spans] == [1, 2, 3]
    assert spans[1].content.startswith("Table columns\n")
    assert spans[2].content == "Continued exclusion: freezing."
    out = render_surrounding(spans)
    assert "Page 1" in out and "Page 3" in out


@pytest.mark.parametrize(
    "start,end,budget", [(0, 40, 100), (460, 510, 80), (960, 1000, 100)]
)
@pytest.mark.parametrize("is_pdf", [True, False])
def test_budget_window_retains_anchor_at_start_middle_and_end(
    start, end, budget, is_pdf
):
    text = "ș" * 1000
    raw = pdf([(1, text)]) if is_pdf else text.encode()
    spans = surrounding_spans(
        raw, page_number=1 if is_pdf else None, start=start, end=end, budget=budget
    )
    assert sum(len(s.content) for s in spans) == budget
    assert spans[0].start <= start < end <= spans[0].end
    assert spans[0].content == text[spans[0].start : spans[0].end]
    assert "text omitted]" in render_surrounding(spans)


def test_neighbor_tails_and_heads_share_leftover_budget():
    raw = pdf([(1, "before" * 100), (2, "anchor"), (3, "after" * 100)])
    spans = surrounding_spans(raw, page_number=2, start=0, end=6, budget=106)
    assert [(s.page_number, s.start, s.end) for s in spans] == [
        (1, 550, 600),
        (2, 0, 6),
        (3, 0, 50),
    ]
    assert sum(len(s.content) for s in spans) == 106


def test_missing_page_is_not_replaced_with_next_list_entry():
    spans = surrounding_spans(
        pdf([(1, "anchor"), (9, "unrelated")]),
        page_number=1,
        start=0,
        end=6,
        budget=100,
    )
    assert [s.page_number for s in spans] == [1]


def test_one_neighbor_receives_unused_budget_from_other():
    spans = surrounding_spans(
        pdf([(1, "anchor"), (2, "next" * 100)]),
        page_number=1,
        start=0,
        end=6,
        budget=100,
    )
    assert [len(s.content) for s in spans] == [6, 94]


def test_huge_requested_budget_is_capped():
    spans = surrounding_spans(
        b"x" * 100_000, page_number=None, start=60_000, end=62_400, budget=1_000_000
    )
    assert sum(len(s.content) for s in spans) == CONTEXT_LIMIT_MAX


@pytest.mark.parametrize("start,end,budget", [(0, 10, 5), (-1, 2, 20), (0, 40, 50)])
def test_invalid_offsets_or_insufficient_budget_fail(start, end, budget):
    with pytest.raises(ValueError):
        surrounding_spans(
            b"a" * 30, page_number=None, start=start, end=end, budget=budget
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"broken",
        pdf([(1, "a"), (1, "b")]),
        pdf([(5, "a")]),
        b'[{"page":1,"content":4}]',
    ],
)
def test_invalid_pdf_evidence_fails(raw):
    with pytest.raises(ValueError):
        surrounding_spans(raw, page_number=1, start=0, end=1)
