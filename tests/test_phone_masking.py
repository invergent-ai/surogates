"""Phone-like numbers never reach the model whole, however they are written."""

import pytest

from surogates.channels.identity import mask_readable_phones


@pytest.mark.parametrize("text", [
    "call +40 746 148 303 now", "0746-148-303", "40746148303.", "(0746) 148 303",
    "+40 (746) 148303",
])
def test_seven_or_more_digits_are_masked(text):
    masked = mask_readable_phones(text)
    assert sum(c.isdigit() for c in masked) < 7 and "…303" in masked


@pytest.mark.parametrize("text", [
    "on 2026-10-14 08:30", "[10-14 08:30] them", "code 123456",
    "user 12345678-1234-5678-9abc-def012345678",
])
def test_dates_short_codes_and_ids_are_left_alone(text):
    assert mask_readable_phones(text) == text
