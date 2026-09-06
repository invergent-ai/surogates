"""Behavioural pin on the vendored official scorer.

Cases adapted from upstream's own test suite
(dabstep_benchmark/tests/test_scorer.py in the adyen/DABstep space) plus
the guideline formats the tasks actually use. If a re-vendored scorer
changes any of these verdicts, grading has changed and every prior run
needs re-scoring before comparison.
"""
from dabbench.official_scorer import question_scorer


def test_exact_strings():
    assert question_scorer("NL", "NL")
    assert question_scorer("  nl ", "NL")
    assert not question_scorer("BE", "NL")


def test_numeric_tolerance_and_formats():
    assert question_scorer("1,234.56", "1234.56")
    assert question_scorer("$5.13", "5.13")
    assert question_scorer("0.10001", "0.1")  # small numbers: 1e-4 tolerance
    assert not question_scorer("0.2", "0.1")
    assert question_scorer("765.34", "765.3432")  # rounded to fewer decimals
    assert not question_scorer("764.34", "765.34")


def test_lists_ignore_order_and_separator():
    assert question_scorer("A, B, C", "C; B; A")
    assert question_scorer("[A, B]", "A, B")
    assert not question_scorer("A, B", "A, B, C")


def test_empty_and_not_applicable():
    assert question_scorer("Not Applicable", "not applicable")
    assert not question_scorer("Not Applicable", "42")


def test_near_string_similarity():
    # SequenceMatcher > 0.95 accepts trivial punctuation drift only.
    assert question_scorer("GlobalCard", "globalcard")
    assert not question_scorer("GlobalCard", "TransactPlus")
