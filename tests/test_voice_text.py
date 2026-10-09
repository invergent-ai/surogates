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


async def test_a_finished_sentence_is_spoken_when_no_more_text_comes():
    """The agent says "O clipă, verific." and goes quiet while a tool runs. LiveKit's markdown filter
    drops the trailing space, so without a timer the sentence waited for the answer's first word."""
    import asyncio
    import time

    async def deltas():
        yield "O clipă, verific."
        await asyncio.sleep(5)  # the tool runs
        yield " Euro e 4,97 lei."

    started = time.monotonic()
    it = spoken_sentences(deltas()).__aiter__()
    assert await asyncio.wait_for(it.__anext__(), 2) == "O clipă, verific."
    assert time.monotonic() - started < 1.0


async def test_an_abbreviation_at_a_pause_is_not_a_sentence():
    import asyncio

    async def deltas():
        yield "Locuiesc pe str."
        await asyncio.sleep(0.6)
        yield " Mihai Eminescu nr. 5."

    assert [s async for s in spoken_sentences(deltas())] == ["Locuiesc pe str. Mihai Eminescu nr. 5."]


def test_english_lines_get_english_rules():
    from surogates.voice.text import asks_for_details, is_preamble

    assert caller_says_goodbye("Okay, that's all, thanks", "en")
    assert caller_says_goodbye("Bye", "en")
    assert not caller_says_goodbye("Byers Street, please", "en")
    assert is_farewell("Goodbye, have a nice day!", "en")
    assert not is_farewell("Thank you! Anything else?", "en")
    assert is_preamble("One moment, let me check that.", "en")
    assert asks_for_details("Could I have your phone number?", "en")
    assert not asks_for_details("Can I help with anything else?", "en")


def test_a_language_without_rules_never_guesses():
    """Romanian words in a German call are not a goodbye; its calls end through the end_call tool."""
    from surogates.voice.text import asks_for_details

    assert not caller_says_goodbye("La revedere", "de")
    assert not is_farewell("Auf Wiedersehen!", "de")
    assert not asks_for_details("Wie ist Ihre Telefonnummer?", "de")
    assert say_as("Surogate", {}, "de") == "Surogate"  # the Romanian spelling is for our Romanian voice


def test_thanks_in_the_middle_of_a_conversation_is_not_a_goodbye():
    # the agent hung up on these (review 2026-10-09): "thanks" opens or closes ordinary sentences
    for reply in ["Thank you, John. Your appointment is confirmed for Monday.",
                  "Thanks! I found your order, it ships tomorrow.",
                  "You're welcome. Your table is booked for eight."]:
        assert not is_farewell(reply, "en"), reply
    for said in ["Could you tell me your opening hours, thanks.", "Can you check order 4411 for me, thank you",
                 "That's it, thanks, but one more question about parking"]:
        assert not caller_says_goodbye(said, "en"), said
    assert not is_farewell("Mulțumesc, am notat programarea pentru marți.")
    assert not caller_says_goodbye("Îmi spuneți și programul de mâine, mersi")
    # a goodbye is still a goodbye
    assert is_farewell("You're welcome, have a nice day!", "en")
    assert is_farewell("Thank you for calling, goodbye!", "en")
    assert is_farewell("Cu plăcere, o zi bună.")
    for said in ["Thanks", "Okay, thank you.", "That's it, thanks.", "Nothing else, bye"]:
        assert caller_says_goodbye(said, "en"), said
    assert caller_says_goodbye("Bine, mersi")


def test_no_at_the_end_of_a_sentence_is_an_answer_not_an_abbreviation():
    s = SentenceSplitter()
    assert s.push("Unfortunately, no. We close at five today. ") == ["Unfortunately, no.", "We close at five today."]
