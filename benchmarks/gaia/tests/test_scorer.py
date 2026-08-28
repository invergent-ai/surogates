from gaia_bench.scorer import (
    extract_final_answer,
    lenient_scorer,
    question_scorer,
)


class TestStrictScorer:
    def test_exact_number(self):
        assert question_scorer("90", "90")

    def test_number_with_currency_and_separators(self):
        assert question_scorer("$89,706.00", "89706.00")

    def test_wrong_number(self):
        assert not question_scorer("91", "90")

    def test_string_case_and_punctuation_insensitive(self):
        assert question_scorer("White", "white")

    def test_wrong_string(self):
        assert not question_scorer("Black", "white")

    def test_list_in_order(self):
        assert question_scorer("a, b, c", "a, b, c")

    def test_list_length_mismatch_is_false(self):
        assert not question_scorer("a, b", "a, b, c")

    def test_mixed_list_with_number_element(self):
        assert question_scorer("White; 5876", "White; 5876")


class TestLenientScorer:
    def test_accepts_what_strict_accepts(self):
        assert lenient_scorer("90", "90")

    def test_ignores_leading_article(self):
        assert lenient_scorer("the White House", "White House")
        assert not question_scorer("the White House", "White House")

    def test_ignores_units_suffix(self):
        assert lenient_scorer("5876 minutes", "5876")
        assert not question_scorer("5876 minutes", "5876")

    def test_still_rejects_wrong_answers(self):
        assert not lenient_scorer("Black", "white")
        assert not lenient_scorer("91", "90")

    def test_accepts_the_right_answer_padded_with_words(self):
        # "correct content, wrong shape" -- a formatting loss, not a
        # capability one, and invisible to the strict scorer.
        assert lenient_scorer("The answer is White House", "White House")
        assert not question_scorer("The answer is White House", "White House")

    def test_accepts_a_shorter_phrasing_of_the_same_answer(self):
        assert lenient_scorer("Polybius Plaza", "in Polybius Plaza")

    def test_numeric_ground_truth_never_matches_by_containment(self):
        # The dangerous case: "6" is a substring of "16", and "found 7
        # crocodiles, 6 were native" contains "6". Counting either as a
        # near-miss would manufacture formatting headroom that is really
        # a wrong answer.
        assert not lenient_scorer("16", "6")
        assert not lenient_scorer("found 7 crocodiles, 6 were native", "6")
        assert not lenient_scorer("0.00022", "0.00033")

    def test_list_ground_truth_never_matches_by_containment(self):
        # For list answers the element count IS the answer -- the official
        # scorer returns False on a length mismatch. A truncated or
        # over-inclusive list is wrong, not mis-formatted, and containment
        # matches both trivially.
        assert not lenient_scorer(
            "Brunei, China, Morocco, Singapore, Venezuela",
            "Brunei, China, Morocco, Singapore",
        )
        assert not lenient_scorer(
            "3/4,1/4,3/4", "3/4,1/4,3/4,3/4,2/4,1/2",
        )

    def test_list_answers_still_pass_when_they_match(self):
        assert lenient_scorer("a, b, c", "a, b, c")

    def test_single_word_ground_truth_never_matches_by_containment(self):
        # One token is too weak a signal: "castle" appearing anywhere in a
        # long answer does not mean the answer is castle.
        assert not lenient_scorer(
            "the maze contained a castle and a river", "castle"
        )


class TestExtractFinalAnswer:
    def test_extracts_marked_answer(self):
        assert extract_final_answer("Reasoning.\nFINAL ANSWER: 42") == "42"

    def test_takes_the_last_marker(self):
        text = "FINAL ANSWER: 1\nmore work\nFINAL ANSWER: 2"
        assert extract_final_answer(text) == "2"

    def test_case_insensitive_marker(self):
        assert extract_final_answer("final answer: 42") == "42"

    def test_strips_trailing_punctuation_and_whitespace(self):
        assert extract_final_answer("FINAL ANSWER:  42  ") == "42"

    def test_returns_none_when_absent(self):
        assert extract_final_answer("I think it is 42.") is None

    def test_strips_markdown_emphasis_around_the_marker(self):
        # Observed in a real run: the model wrote "**FINAL ANSWER:** 8",
        # the regex matched inside the bold and captured "** 8", and a
        # correct answer scored as wrong.
        assert extract_final_answer("**FINAL ANSWER:** 8") == "8"

    def test_strips_emphasis_wrapping_the_whole_line(self):
        assert extract_final_answer("**FINAL ANSWER: 8**") == "8"

    def test_strips_emphasis_around_the_answer_only(self):
        assert extract_final_answer("FINAL ANSWER: **THE CASTLE**") == "THE CASTLE"

    def test_strips_backticks_and_underscores(self):
        assert extract_final_answer("FINAL ANSWER: `42`") == "42"
        assert extract_final_answer("_FINAL ANSWER:_ _42_") == "42"

    def test_does_not_eat_internal_punctuation(self):
        assert extract_final_answer("FINAL ANSWER: **a, b, c**") == "a, b, c"
