"""A voice line's fixed lines and language: the owner's own over the language's defaults."""
from dataclasses import fields

import pytest

pytest.importorskip("livekit.agents", reason="the voice extra is not installed")

from surogates.voice.agent import CallConfig  # noqa: E402
from surogates.voice.lines import DEFAULTS, Lines, default_lines, language_name, lines_from_routing  # noqa: E402
from surogates.voice.sessions import question_text  # noqa: E402


def test_every_language_has_every_line_and_choices_keep_their_slot():
    for lines in DEFAULTS.values():
        assert all(getattr(lines, f.name).strip() for f in fields(Lines))
        assert "{}" in lines.choices


def test_the_owners_lines_win_and_bad_ones_keep_the_default():
    cfg = {"greeting": "Hi, Ana here.", "lines": {"filler": "Hang on.", "busy": "  ", "goodbye": 5,
                                                   "choices": "Pick one."}}
    lines = lines_from_routing("en", cfg)
    assert (lines.greeting, lines.filler) == ("Hi, Ana here.", "Hang on.")
    en = default_lines("en")
    assert (lines.busy, lines.goodbye, lines.choices) == (en.busy, en.goodbye, en.choices)


def test_a_language_without_defaults_falls_back_to_english_lines():
    assert default_lines("sw") == default_lines("en") and default_lines("pt-BR") == default_lines("en")
    assert language_name("de") == "German" and language_name("sw") == "sw"


def test_call_config_reads_the_language_and_old_rows_stay_romanian():
    assert CallConfig.from_routing({}).language == "ro"
    assert CallConfig.from_routing({}).lines == default_lines("ro")
    en = CallConfig.from_routing({"language": "en", "greeting": "Hello there!"})
    assert (en.language, en.lines.greeting, en.lines.filler) == ("en", "Hello there!", default_lines("en").filler)
    assert CallConfig.from_routing({"language": "Romanian; drop table"}).language == "ro"


def test_question_choices_are_read_in_the_lines_language():
    q = {"questions": [{"prompt": "Which day?", "choices": [{"label": "Monday"}, {"label": "Tuesday"}]}]}
    assert question_text(q, "", default_lines("en").choices) == "Which day? The options are: Monday, Tuesday."
