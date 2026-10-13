"""The xlsx skill's recalc.py flags formulas saved without their leading '='."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/productivity/xlsx/scripts"))
from recalc import TEXT_FORMULA_RE  # noqa: E402


@pytest.mark.parametrize("text", [
    "'Monthly Model'!$S$39",
    "D9-('Assumptions'!$B$12+'Assumptions'!$B$13)",
    "SUM(B6:B8)",
    "Inputs!B4*2",
])
def test_formula_without_equals_is_flagged(text):
    assert TEXT_FORMULA_RE.search(text)


@pytest.mark.parametrize("text", [
    "FY2024-25", "Q1 - Q4 total", "Year 1", "NPV (10%)", "Hello!", "See note 3!",
    "Revenue = sum of months", "USD/month", "A1", "-",
])
def test_label_is_not_flagged(text):
    assert not TEXT_FORMULA_RE.search(text)
