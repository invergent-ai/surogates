"""A key fact is found however the document writes it: a percentage in figures or in words, and nothing that only
contains it."""
import pytest

from obench.checks import Ctx, contains_all, has_text, number_words


@pytest.mark.parametrize("written", [
    "70% of them", "70 % of them", "70 percent of them", "70 per cent of them", "70 per-cent", "70 pct of them",
    "Seventy percent still can't sell online", "seventy per cent", "(70%)",
])
def test_a_percentage_is_found_in_figures_or_in_words(written):
    assert has_text(written, "70%")


@pytest.mark.parametrize("written", [
    "170% of them", "7% of them", "70.5% of them", "70,5 percent", "seven percent", "a hundred and seventy percent",
    "seventy of them", "70 of them", "1.70%",
])
def test_a_percentage_is_not_found_in_another_number(written):
    assert not has_text(written, "70%")


def test_number_words_up_to_a_hundred():
    assert number_words(7) == ["seven"]
    assert number_words(19) == ["nineteen"]
    assert number_words(40) == ["forty"]
    assert number_words(85) == ["eighty-five", "eighty five"]
    assert number_words(99) == ["ninety-nine", "ninety nine"]
    assert number_words(100) == ["a hundred", "one hundred"]
    assert number_words(101) == []


def test_spelled_percentages():
    assert has_text("Eighty-five percent renew", "85%")
    assert has_text("eighty five per cent renew", "85%")
    assert has_text("a hundred percent of stores", "100%")
    assert has_text("Two percent per sale", "2%")
    assert not has_text("seventy-two percent", "2%")
    assert not has_text("seventy two percent", "2%")
    assert not has_text("a hundred and two percent", "2%")
    assert not has_text("12% per sale", "2%")


def test_a_decimal_percentage():
    assert has_text("4.5% a year", "4.5%")
    assert has_text("4,5 percent a year", "4.5%")
    assert not has_text("14.5% a year", "4.5%")


def test_other_facts_are_read_as_before():
    assert has_text("below 10% by July", "below 10%")
    assert has_text("2,300 shops", 2300)
    assert has_text("The North region", "north")
    assert not has_text("The South region", "north")


def test_the_key_facts_check_reads_the_words():
    class Doc:
        def text(self):
            return "Seventy percent still can't sell online; we take 2 per cent of each sale."

    assert contains_all(["70%", "2%"]).run(Doc(), Ctx()).passed
