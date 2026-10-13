"""A titled memory entry is one record that adding again keeps current."""

from surogates.api.routes.memory import _titled_entry

RECORD = "FIȘA\nPacient: Vasile Ionescu\nRăspunde: pacientul"


def test_adding_a_record_with_the_same_title_finds_the_old_one():
    entries = ["Prefers mornings", RECORD]
    assert _titled_entry("FIȘA\nPacient: Vasile Ionescu\nRăspunde: fiica", entries) == RECORD


def test_a_different_title_is_a_new_entry():
    assert _titled_entry("NOTE\nsomething", [RECORD]) is None


def test_a_long_first_line_is_prose_not_a_title():
    prose = "The user said they prefer to be contacted in the morning after breakfast, never late"
    assert _titled_entry(prose + "\nmore", [prose + "\nolder"]) is None


def test_empty_content_has_no_title():
    assert _titled_entry("   ", [RECORD]) is None
