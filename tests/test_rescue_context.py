"""A reply turned into a question shows the question once."""

from surogates.harness.loop import rescue_context


def test_the_question_drawn_from_a_reply_is_not_left_in_it():
    reply = ("Îmi pare rău că a ajuns așa.\n\nCa să trimit cazul mai departe, îmi puteți "
             "trimite două poze cu planta?")
    question = "Îmi puteți trimite două poze cu planta?"
    assert rescue_context(reply, question) == "Îmi pare rău că a ajuns așa.\n\nCa să trimit cazul mai departe,"


def test_a_question_worded_apart_from_the_reply_keeps_the_whole_reply():
    assert rescue_context(" Vă pot ajuta cu altceva? ", "Ce comandă aveți?") == "Vă pot ajuta cu altceva?"
    assert rescue_context(None, "Ce comandă aveți?") == ""
