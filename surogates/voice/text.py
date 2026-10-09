"""Text for the phone voice: what the TTS should say, and what the caller just said.

The language rules (the agent's "I'm on it" line, goodbyes, asking for details) exist for Romanian
and English. A line in any other language gets none of them: its calls still end through the
agent's end_call tool, and the background simply never picks up a pen.

Ported from an earlier Twilio prototype. It shapes speech
only: the session keeps the agent's text exactly as written.
"""
from __future__ import annotations

import asyncio
import difflib
import re
import unicodedata
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from dataclasses import dataclass

BRAND = re.compile(r"\bsurogate\b", re.I)
GLUED_NUMBER = re.compile(r"\b([A-Za-z]+)(\d{2,})\b")  # "A220" is a name and a number, not a code to dictate
MODEL_VARIANT = re.compile(r"\b([A-Za-z]+\d{2,})-\d{2,}\b")  # "A220-300": Amami dictates the dash form
HAS_WORDS = re.compile(r"\w")


def say_as(text: str, table: Mapping[str, str], language: str = "ro") -> str:
    """Spell names the way a caller says them. Table keys match whole words, case-sensitively."""
    if language == "ro":
        text = BRAND.sub("Surogheit", text)  # hard g; "Surogeit" is wrong
    if table:
        keys = "|".join(map(re.escape, sorted(table, key=len, reverse=True)))
        text = re.sub(rf"(?<![\w&])({keys})(?![\w&])", lambda m: table[m.group(1)], text)
    return GLUED_NUMBER.sub(r"\1 \2", MODEL_VARIANT.sub(r"\1", text))


def clean(text: str) -> str:
    text = re.sub(r"\s*[—–]\s*", ", ", text)  # a dash is where a speaker pauses
    text = re.sub(r"\b([\w-]+)\.(ro|com|net|org|eu)\b", r"\1", text)  # sources are said by name
    return re.sub(r"[*#_`>]", "", text).strip()


# A sentence ends at punctuation + space, or where a preamble and the answer arrive glued ("acum.Azi").
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[a-zăâîșț0-9]{2}[.!?…])(?=[A-ZĂÂÎȘȚ])")
# ...but not after "nr.", "str.", "Mr." or an initial ("S.A.", "M. Eminescu"). The initial is
# capital-only: "Prețul e." still ends a sentence. Romanian and English abbreviations both: a
# Romanian line quotes English names and the other way round.
ABBREVIATION_END = re.compile(
    r"((?i:\b(?:nr|dl|dna|dra|str|tel|prof|dr|ing|art|alin|pct|etc|ex|sf|bd|jud|mun"
    r"|mr|mrs|ms|st|vs|jr|sr|inc|ltd|dept|approx))|\b[A-ZĂÂÎȘȚ])\.$")


class SentenceSplitter:
    """Cuts the agent's streamed text into sentences, as soon as each one is complete."""

    def __init__(self) -> None:
        self._buf = ""

    def push(self, delta: str) -> list[str]:
        *done, self._buf = SENTENCE_END.split(self._buf + delta)
        out, carry = [], ""
        for piece in done:
            piece = f"{carry} {piece}" if carry else piece
            if ABBREVIATION_END.search(piece):
                carry = piece
                continue
            carry = ""
            if HAS_WORDS.search(sentence := clean(piece)):
                out.append(sentence)
        if carry:
            self._buf = f"{carry} {self._buf}"
        return out

    def flush(self) -> str:
        tail, self._buf = clean(self._buf), ""
        return tail if HAS_WORDS.search(tail) else ""

    def ends_sentence(self) -> bool:
        """The buffer holds a finished sentence that only lacks the space after its full stop."""
        tail = self._buf.rstrip()
        return bool(tail) and tail[-1] in ".!?…" and not ABBREVIATION_END.search(tail)


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def fold(text: str) -> str:
    """Lowercase without diacritics: speech-to-text spells "știri" and "stiri" both ways."""
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


@dataclass(frozen=True)
class Rules:
    # The agent's "I'm on it" line. Once per answer is natural; the model sometimes says it again after
    # the tool returns ("O clipă, mă uit…" then "Imediat, verific…"), and twice sounds broken.
    preamble: re.Pattern
    # The caller is done, hang up after the agent's answer: a goodbye anywhere, "that's all" at the end, or
    # a thanks that is almost all they said ("thanks" closes ordinary requests too: "…opening hours, thanks").
    bye: re.Pattern
    farewell: re.Pattern  # a goodbye word in the agent's reply; is_farewell also wants it short, no question
    details: re.Pattern  # words of something a person writes down (a name, a number…)


RULES = {
    "ro": Rules(
        preamble=re.compile(r"^(o clipă|imediat|stai puțin|un moment|o secundă|mă uit|verific|caut|acum verific)\b"
                            r".{0,80}(verific|mă uit|caut|văd|iau)", re.I),
        # "pa" only as the last word: "Papa Francisc…"
        bye=re.compile(r"\b(la revedere|o zi bună)\b"
                       r"|\b(mulțumesc,? atât|asta e tot|gata,? mulțumesc|nimic altceva)\b[^?]{0,15}$"
                       r"|^\W*(\w+\W+){0,2}(pa[ -]?pa|pa|mersi|ciao|bye|mulțumesc( frumos)?)\W*$", re.I),
        farewell=re.compile(r"\b(pa|la revedere|o zi bună|spor|numai bine|toate cele bune)\b", re.I),
        details=re.compile(r"\b(nume|numele|prenume|telefon|număr|numărul|e-?mail|adres[aă]|data|cnp|cod|ziua|ora)\b",
                           re.I),
    ),
    "en": Rules(
        preamble=re.compile(r"^(one moment|just a moment|one second|just a second|hold on|let me|i'll|i will)\b"
                            r".{0,80}(check|look|find|see|search|pull)", re.I),
        bye=re.compile(r"\b(goodbye|good bye|bye[ -]?bye|have a (nice|good|great) day)\b"
                       r"|\b(that'?s all|that is all|nothing else|that'?s it)\b[^?]{0,15}$"
                       r"|^\W*(\w+\W+){0,2}(bye|thanks|thank you|cheers)( (so|very) much)?\W*$", re.I),
        farewell=re.compile(r"\b(bye|goodbye|good bye|have a (nice|good|great) day|take care|all the best)\b", re.I),
        details=re.compile(r"\b(name|surname|phone|number|e-?mail|address|date|code|day|time|postcode|zip)\b", re.I),
    ),
}


def rules(language: str) -> Rules | None:
    return RULES.get(language.split("-")[0])


def is_preamble(sentence: str, language: str = "ro") -> bool:
    r = rules(language)
    return r is not None and bool(r.preamble.search(sentence)) and len(words(sentence)) <= 14


FLUSH_AFTER = 0.35  # seconds without new text after a full stop: the sentence is finished, say it


async def spoken_sentences(deltas: AsyncIterable[str], language: str = "ro") -> AsyncIterator[str]:
    """One answer's sentences in speaking order, without a second preamble.

    A sentence that ends in a full stop and gets no more text for ``FLUSH_AFTER`` is spoken without
    waiting for the next one: the agent says "O clipă, verific." and then runs a tool, and filters
    upstream (LiveKit's markdown filter) drop the trailing space a split would need.
    """
    splitter, preambled = SentenceSplitter(), False

    def keep(sentence: str) -> bool:
        nonlocal preambled
        if not is_preamble(sentence, language):
            return True
        first, preambled = not preambled, True
        return first

    it = deltas.__aiter__()
    pending = asyncio.ensure_future(it.__anext__())  # never cancelled on a timeout: that would end the stream
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=FLUSH_AFTER if splitter.ends_sentence() else None)
            if not done:
                if (sentence := splitter.flush()) and keep(sentence):
                    yield sentence
                continue
            try:
                delta = pending.result()
            except StopAsyncIteration:
                break
            pending = asyncio.ensure_future(it.__anext__())
            for sentence in splitter.push(delta):
                if keep(sentence):
                    yield sentence
    finally:
        pending.cancel()
    if (tail := splitter.flush()) and keep(tail):
        yield tail


def is_echo(heard: str, recent: list[str], *, while_speaking: bool = False) -> bool:
    """Is this "caller" text our own voice coming back (speakerphone)? STT garbles the echo, so compare words."""
    h = words(heard)
    if while_speaking and len(h) >= 2:
        ours = {w for s in recent for w in words(s)}
        content = [w for w in h if len(w) >= 3]
        loose = lambda w: w in ours or any(o.startswith(w) or w.startswith(o) for o in ours if len(o) >= 4)  # noqa: E731
        if content and sum(map(loose, content)) >= 0.6 * len(content):
            return True
    if len(h) < 3:
        return False
    for s in recent:
        m = difflib.SequenceMatcher(None, h, words(s), autojunk=False)
        # echo keeps runs of our words; a caller reusing a few of them does not
        if sum(b.size for b in m.get_matching_blocks() if b.size >= 2) >= 0.45 * len(h):
            return True
    return False


def caller_says_goodbye(text: str, language: str = "ro") -> bool:
    r = rules(language)
    return r is not None and bool(r.bye.search(text.strip()))


def is_farewell(reply: str, language: str = "ro") -> bool:
    """A reply that says goodbye and nothing more: short, no question, a goodbye word in it ("thank you"
    alone is not one: "Thank you, John. Your appointment is confirmed."). The call can end after it."""
    r, reply = rules(language), reply.strip()
    return r is not None and "?" not in reply and len(words(reply)) <= 15 and bool(r.farewell.search(reply))


def asks_for_details(sentence: str, language: str = "ro") -> bool:
    """The agent asked for something a person would write down (a name, a number, an address…)."""
    s, r = sentence.strip(), rules(language)
    return r is not None and s.endswith("?") and bool(r.details.search(s))
