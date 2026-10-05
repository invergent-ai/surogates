"""What the phone voice says and hears: ported from the Twilio prototype, then fixed for Romanian abbreviations."""
from surogates.voice.text import (
    SentenceSplitter, caller_says_goodbye, clean, is_echo, is_farewell, say_as, spoken_sentences,
)


def test_say_as_spells_brand_and_agent_names():
    table = {"Nvidia": "Envidia", "DAX": "Dax"}
    assert say_as("Surogate spune că Nvidia și DAX cresc.", table) == "Surogheit spune că Envidia și Dax cresc."
    assert say_as("Avionul A220-300 aterizează.", {}) == "Avionul A 220 aterizează."
    assert say_as("Nvidiaa nu e în tabel.", table) == "Nvidiaa nu e în tabel."


def test_clean_drops_markdown_and_domains():
    assert clean("**Sursa** — goldring.ro") == "Sursa, goldring"


def test_splitter_streams_sentences_and_keeps_the_tail():
    s = SentenceSplitter()
    assert s.push("Bună ziua. Cu ce") == ["Bună ziua."]
    assert s.push(" vă pot ajuta?") == []
    assert s.flush() == "Cu ce vă pot ajuta?"


def test_splitter_does_not_cut_after_abbreviations_or_initials():
    s = SentenceSplitter()
    out = s.push("Locuiesc pe str. Mihai Eminescu nr. 5. Firma X S.A. are profit. Gata")
    assert out == ["Locuiesc pe str. Mihai Eminescu nr. 5.", "Firma X S.A. are profit."]
    assert s.flush() == "Gata"


def test_splitter_cuts_a_glued_preamble_and_answer():
    assert SentenceSplitter().push("Verific acum.Azi cursul e 4,97. ") == ["Verific acum.", "Azi cursul e 4,97."]


async def test_spoken_sentences_drops_a_repeated_preamble():
    async def deltas():
        for d in ("O clipă, verific cursul. ", "Imediat, verific din nou. ", "Euro e 4,97 lei."):
            yield d
    assert [s async for s in spoken_sentences(deltas())] == ["O clipă, verific cursul.", "Euro e 4,97 lei."]


def test_echo_of_our_own_sentence_is_recognised():
    said = ["Te pot ajuta cu întrebări legate de știrile zilei."]
    assert is_echo("întrebări legate de știrile zilei", said)
    assert not is_echo("care sunt știrile de azi", said)
    assert not is_echo("da", said)


def test_goodbye_and_farewell():
    assert caller_says_goodbye("Mulțumesc, atât.")
    assert caller_says_goodbye("Bine, pa")
    assert not caller_says_goodbye("Papa Francisc a spus ceva")
    assert is_farewell("La revedere, o zi bună!")
    assert not is_farewell("La revedere? Mai aveți o întrebare?")
