"""The server settles ``is_other`` from the questions it stored.

Whether an answer went off the menu is a fact about what the agent
asked, not something a submitter is in a position to assert.  Every
surface used to decide it independently and agree only by inspection;
these pin the one definition that now overrides them.
"""

from surogates.session.interactive_input import derive_is_other

OPEN = [{"prompt": "What subjects do you like?"}]
MENU = [
    {
        "prompt": "Which region?",
        "choices": [{"label": "eu-west"}, {"label": "us-east"}],
    },
]


class TestDeriveIsOther:
    def test_open_question_is_never_other_however_it_was_submitted(self):
        # The bug this whole rule exists to prevent: an agent that asks
        # openly had every single reply flagged.
        [row] = derive_is_other(
            OPEN,
            [{"question": OPEN[0]["prompt"], "answer": "computers", "is_other": True}],
        )
        assert row["is_other"] is False
        assert row["answer"] == "computers"

    def test_menu_answer_matching_a_choice_is_not_other(self):
        [row] = derive_is_other(
            MENU,
            [{"question": "Which region?", "answer": "eu-west", "is_other": True}],
        )
        assert row["is_other"] is False

    def test_menu_answer_off_the_list_is_other(self):
        [row] = derive_is_other(
            MENU,
            [{"question": "Which region?", "answer": "frankfurt", "is_other": False}],
        )
        assert row["is_other"] is True

    def test_choice_match_ignores_case_and_padding(self):
        [row] = derive_is_other(
            MENU,
            [{"question": "Which region?", "answer": "  EU-West ", "is_other": True}],
        )
        assert row["is_other"] is False

    def test_empty_choices_list_is_treated_as_no_menu(self):
        [row] = derive_is_other(
            [{"prompt": "Why?", "choices": []}],
            [{"question": "Why?", "answer": "because", "is_other": True}],
        )
        assert row["is_other"] is False

    def test_answers_are_matched_per_question_in_a_batch(self):
        questions = [
            {"prompt": "Which region?", "choices": [{"label": "eu-west"}]},
            {"prompt": "Anything else?"},
        ]
        rows = derive_is_other(
            questions,
            [
                {"question": "Anything else?", "answer": "no", "is_other": True},
                {"question": "Which region?", "answer": "frankfurt", "is_other": False},
            ],
        )
        # Matched by prompt, so the submitted order does not matter.
        assert rows[0]["is_other"] is False
        assert rows[1]["is_other"] is True

    def test_falls_back_to_position_when_the_prompt_was_rewritten(self):
        [row] = derive_is_other(
            MENU,
            [{"question": "which region", "answer": "frankfurt", "is_other": False}],
        )
        assert row["is_other"] is True

    def test_unidentifiable_question_keeps_the_submitted_flag(self):
        # Refusing to guess beats overwriting a correct value: the
        # inbox row can be gone by the time a late answer arrives.
        [row] = derive_is_other(
            [], [{"question": "gone", "answer": "x", "is_other": True}],
        )
        assert row["is_other"] is True

    def test_does_not_mutate_the_caller_s_rows(self):
        submitted = [
            {"question": OPEN[0]["prompt"], "answer": "computers", "is_other": True},
        ]
        derive_is_other(OPEN, submitted)
        assert submitted[0]["is_other"] is True

    def test_preserves_fields_it_does_not_own(self):
        [row] = derive_is_other(
            OPEN,
            [
                {
                    "question": OPEN[0]["prompt"],
                    "answer": "computers",
                    "is_other": True,
                    "extra": "kept",
                },
            ],
        )
        assert row["extra"] == "kept"
