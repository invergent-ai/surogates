"""Romanian comma-below letters in assistant replies.

Romanian writes s and t with a comma below: ``ș ț Ș Ț`` (U+0219, U+021B,
U+0218, U+021A).  Models trained on older web text often write the cedilla
letters ``ş ţ Ş Ţ`` (U+015F, U+0163, U+015E, U+0162) instead, sometimes both
forms in one reply ("scoate cartuşul, înlocuiește").  The cedilla letters
are wrong in Romanian but correct in Turkish, Azerbaijani, Gagauz, Kurdish
and other languages, so the substitution is applied only to a reply this
module recognises as Romanian.

:func:`normalize_reply_text` is the entry point.  It is deliberately
conservative: when in doubt it returns the text unchanged.

What changes
    Only the four letters above, each to its comma-below twin.  The mapping
    is one code point to one code point, so the text keeps its length and
    every other character, whitespace included, is untouched (no NFC pass).

What is never changed
    * fenced code blocks (````` ``` ````` or ``~~~``; an unclosed fence runs
      to the end of the text) and inline code spans (backtick runs);
    * ``<think>`` / ``<thinking>`` / ``<reasoning>`` / ``<thought>`` /
      ``<REASONING_SCRATCHPAD>`` blocks, should any still be in the text
      (an unclosed one runs to the end);
    * URLs, e-mail addresses, file paths and file names (a whitespace-
      separated token holding ``/``, ``\\``, ``@``, ``://`` or a ``.ext``
      suffix), since they name things outside the reply that must keep
      their exact spelling;
    * a reply that is not Romanian (see :func:`looks_romanian`), which
      comes back as the very same string.

The caller decides which text is "the reply": the harness passes only the
assistant message's ``content`` -- never tool-call arguments, tool results,
reasoning, user or system messages.
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# The substitution
# ---------------------------------------------------------------------------

#: Cedilla letter -> Romanian comma-below letter.  Nothing else is mapped.
CEDILLA_TO_COMMA_BELOW: Final[dict[str, str]] = {
    "ş": "ș",  # ş -> ș
    "ţ": "ț",  # ţ -> ț
    "Ş": "Ș",  # Ş -> Ș
    "Ţ": "Ț",  # Ţ -> Ț
}
_TRANSLATE: Final = str.maketrans(CEDILLA_TO_COMMA_BELOW)
_CEDILLA_RE: Final = re.compile("[şţŞŢ]")


def fold_cedillas(text: str) -> str:
    """*text* with every cedilla s/t replaced by its comma-below twin.

    No language check: callers use it to compare two texts that may differ
    only by this substitution (it keeps the length of the text).
    """
    return text.translate(_TRANSLATE)


# ---------------------------------------------------------------------------
# The Romanian detector
# ---------------------------------------------------------------------------

#: Letters Romanian never uses and Turkic / Kurdish orthographies use often
#: (Turkish ğ ı İ ö ü ç, Azerbaijani ə, Gagauz ä ê, Kurdish ê û).  One of them
#: anywhere in the prose means "not Romanian": the reply is left alone.
TURKIC_VETO_LETTERS: Final[frozenset[str]] = frozenset(
    "ğĞıİəƏöÖüÜäÄêÊûÛçÇ"
)

#: Letters that mark Romanian: ă ș ț (no Turkic language writes them) and
#: â î (Turkish uses them rarely; the veto letters above guard against
#: Turkish anyway).
ROMANIAN_LETTERS: Final[frozenset[str]] = frozenset("ăĂșțȘȚâÂîÎ")

#: Frequent Romanian function words that are not Turkish words.  Matched
#: after folding cedillas, so "şi" counts as "și".  Words that are also
#: frequent in Turkish ("de", "da", "o", "ne", "mi") are left out.
ROMANIAN_WORDS: Final[frozenset[str]] = frozenset({
    "și", "în", "este", "sunt", "care", "pentru", "să", "cu", "pe", "din",
    "la", "nu", "un", "una", "unei", "unui", "mai", "sau", "dacă", "acest",
    "această", "aceasta", "acesta", "aceste", "acești", "fost", "fi", "poate",
    "trebuie", "prin", "după", "până", "către", "despre", "foarte", "lui",
    "ale", "cel", "cea", "cele", "cei", "fiecare", "dar", "iar", "deci",
    "astfel", "între", "aici", "acum", "doar", "ați",
    "vă", "vom", "veți", "avem", "aveți", "există", "trebui", "putea",
    "deja", "încă", "tot", "toate", "toți", "nici", "cum", "când",
    "unde", "ce", "cine", "cât", "câte", "mult", "multe", "puțin",
})

#: Frequent Turkish function words that are not Romanian words.
TURKISH_WORDS: Final[frozenset[str]] = frozenset({
    "ve", "bir", "bu", "șu", "için", "ile", "değil", "olarak", "daha", "çok",
    "gibi", "ama", "veya", "var", "yok", "olan", "kadar", "sonra", "șey",
    "nasıl", "ancak", "göre", "ise", "ben", "sen", "biz", "siz", "onlar",
    "her", "hiç", "bunu", "șimdi", "değildir", "yani", "evet",
    "hayır", "çünkü", "eğer",
})

#: A reply is Romanian only with at least this many Romanian function words,
MIN_ROMANIAN_WORDS: Final[int] = 3
#: at least this many Romanian letters (ă ș ț â î in either case),
MIN_ROMANIAN_LETTERS: Final[int] = 2
#: Romanian function words making at least this share of its words,
MIN_ROMANIAN_WORD_SHARE: Final[float] = 0.10
#: and at least this many Romanian function words per Turkish one.
MIN_ROMANIAN_TO_TURKISH: Final[int] = 4

_WORD_RE: Final = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")


def looks_romanian(prose: str) -> bool:
    """Whether *prose* (text with code already removed) is Romanian.

    All must hold:

    * no letter of :data:`TURKIC_VETO_LETTERS`;
    * at least :data:`MIN_ROMANIAN_WORDS` Romanian function words, making at
      least :data:`MIN_ROMANIAN_WORD_SHARE` of the words;
    * at least :data:`MIN_ROMANIAN_TO_TURKISH` Romanian function words per
      Turkish function word;
    * at least :data:`MIN_ROMANIAN_LETTERS` Romanian letters.

    It prefers leaving a Romanian reply alone to touching anything else.
    """
    if not prose or any(ch in TURKIC_VETO_LETTERS for ch in prose):
        return False
    folded = fold_cedillas(prose)
    if sum(1 for ch in folded if ch in ROMANIAN_LETTERS) < MIN_ROMANIAN_LETTERS:
        return False
    romanian = turkish = total = 0
    for match in _WORD_RE.finditer(folded.lower()):
        word = match.group(0)
        total += 1
        if word in ROMANIAN_WORDS:
            romanian += 1
        elif word in TURKISH_WORDS:
            turkish += 1
        elif "-" in word:
            # Romanian joins clitics with hyphens ("s-a", "într-un", "și-a"):
            # count each joined part.
            for part in word.split("-"):
                if part in ROMANIAN_WORDS:
                    romanian += 1
                elif part in TURKISH_WORDS:
                    turkish += 1
    if romanian < MIN_ROMANIAN_WORDS:
        return False
    if romanian < MIN_ROMANIAN_WORD_SHARE * total:
        return False
    return romanian >= MIN_ROMANIAN_TO_TURKISH * turkish


# ---------------------------------------------------------------------------
# Prose / protected segments
# ---------------------------------------------------------------------------

# Any indent: a fence nested in a list item is still code (CommonMark would
# read a deeply indented one as an indented code block -- code either way).
_FENCE_OPEN_RE: Final = re.compile(r"^[ \t]*(`{3,}|~{3,})", re.MULTILINE)
_THINK_OPEN_RE: Final = re.compile(
    r"<(think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>"
)
_BACKTICKS_RE: Final = re.compile(r"`+")
_TOKEN_RE: Final = re.compile(r"\S+")
# Punctuation around a token that is not part of the name it may hold.
_TOKEN_EDGES: Final = "()[]{}<>\"'`„”“«»,;:!?.*_"
_FILE_EXT_RE: Final = re.compile(r".\.[A-Za-z0-9]{2,8}$")


def _is_literal_token(token: str) -> bool:
    """Whether *token* (no whitespace) names something outside the reply.

    URLs, e-mail addresses, Windows / absolute / home / dot-relative paths,
    a directory ending in ``/``, and anything ending in a file extension.
    A bare ``a/b`` ("și/sau") is prose.
    """
    if "://" in token or "@" in token or "\\" in token:
        return True
    core = token.strip(_TOKEN_EDGES)
    if not core:
        return False
    if core.startswith(("/", "./", "../", "~")) or core.endswith("/"):
        return True
    if "/" in token and "." in core.rsplit("/", 1)[-1]:
        return True
    return bool(_FILE_EXT_RE.search(core))


def _block_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of fenced code blocks and thinking blocks in *text*."""
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while pos < n:
        fence = _FENCE_OPEN_RE.search(text, pos)
        think = _THINK_OPEN_RE.search(text, pos)
        first = min(
            (m for m in (fence, think) if m is not None),
            key=lambda m: m.start(),
            default=None,
        )
        if first is None:
            break
        start = first.start()
        if first is fence:
            marker = fence.group(1)
            # The closing fence: same character, at least as long, alone on
            # its line (any indent, trailing blanks).
            close_re = re.compile(
                r"^[ \t]*" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*$",
                re.MULTILINE,
            )
            line_end = text.find("\n", first.end())
            if line_end == -1:
                end = n
            else:
                close = close_re.search(text, line_end + 1)
                end = close.end() if close else n
        else:
            close_tag = f"</{first.group(1)}>"
            close_at = text.find(close_tag, first.end())
            end = n if close_at == -1 else close_at + len(close_tag)
        spans.append((start, end))
        pos = max(end, start + 1)
    return spans


def _inline_code_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of inline code spans in *text* (one prose segment).

    A run of N backticks opens a span that the next run of exactly N
    backticks closes; a run with no closer is a literal backtick run.
    """
    spans: list[tuple[int, int]] = []
    runs = list(_BACKTICKS_RE.finditer(text))
    i = 0
    while i < len(runs):
        opener = runs[i]
        width = len(opener.group(0))
        for j in range(i + 1, len(runs)):
            if len(runs[j].group(0)) == width:
                spans.append((opener.start(), runs[j].end()))
                i = j + 1
                break
        else:
            i += 1
    return spans


def _subtract(
    start: int, end: int, spans: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """The parts of ``[start, end)`` outside the sorted *spans*."""
    parts: list[tuple[int, int]] = []
    cursor = start
    for s, e in spans:
        if e <= cursor or s >= end:
            continue
        if s > cursor:
            parts.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < end:
        parts.append((cursor, end))
    return parts


def prose_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of the prose of *text*: what is outside fenced code,
    thinking blocks, inline code, URLs, e-mail addresses, paths and file
    names.  Sorted, non-overlapping.
    """
    outer = _subtract(0, len(text), _block_spans(text))
    result: list[tuple[int, int]] = []
    for s, e in outer:
        segment = text[s:e]
        code = [(s + a, s + b) for a, b in _inline_code_spans(segment)]
        for ps, pe in _subtract(s, e, code):
            piece = text[ps:pe]
            literal = [
                (ps + m.start(), ps + m.end())
                for m in _TOKEN_RE.finditer(piece)
                if _is_literal_token(m.group(0))
            ]
            result.extend(_subtract(ps, pe, literal))
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def normalize_reply_text(text: str) -> tuple[str, int]:
    """*text* with cedilla s/t in its Romanian prose turned comma-below.

    Returns ``(new_text, replaced)``.  When nothing is replaced -- no cedilla
    letter in the prose, or the prose is not Romanian -- ``new_text`` is the
    very object passed in.
    """
    if not isinstance(text, str) or not _CEDILLA_RE.search(text):
        return text, 0
    spans = prose_spans(text)
    prose = " ".join(text[s:e] for s, e in spans)
    if not _CEDILLA_RE.search(prose) or not looks_romanian(prose):
        return text, 0
    out: list[str] = []
    cursor = 0
    replaced = 0
    for s, e in spans:
        out.append(text[cursor:s])
        piece = text[s:e]
        replaced += len(_CEDILLA_RE.findall(piece))
        out.append(piece.translate(_TRANSLATE))
        cursor = e
    out.append(text[cursor:])
    if not replaced:
        return text, 0
    return "".join(out), replaced


# ---------------------------------------------------------------------------
# Harness call site
# ---------------------------------------------------------------------------

#: Session config key; ``False`` turns the substitution off for the session.
CONFIG_KEY: Final[str] = "romanian_comma_below"


def enabled_for(config: dict | None) -> bool:
    """Whether a session with *config* gets the substitution (default on)."""
    return (config or {}).get(CONFIG_KEY, True) is not False


def normalize_assistant_message(message: dict, config: dict | None = None) -> int:
    """Apply :func:`normalize_reply_text` to *message*'s ``content`` in place.

    Only a string ``content`` is touched.  ``tool_calls`` (their arguments),
    ``reasoning`` / ``reasoning_content`` / ``reasoning_details`` and every
    other key are left as they are.  Returns how many letters were replaced
    (0 when the session's *config* turns it off).
    """
    if not enabled_for(config):
        return 0
    content = message.get("content")
    if not isinstance(content, str):
        return 0
    new, replaced = normalize_reply_text(content)
    if replaced:
        message["content"] = new
    return replaced
