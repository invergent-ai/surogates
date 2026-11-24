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
# Display math: ``$$ ... $$`` and ``\[ ... \]``.  Only a closed one counts.
_DISPLAY_MATH_RE: Final = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]", re.DOTALL)
# Inline math on one line: ``\( ... \)`` and ``$x$`` (no blank just inside
# the dollars, no digit right after the closing one, so "$5 and $10" is
# prose).
_INLINE_MATH_RE: Final = re.compile(
    r"\\\(.+?\\\)|(?<![\\$])\$(?=\S)[^$\n]+?(?<=\S)\$(?![$\d])"
)
_BACKTICKS_RE: Final = re.compile(r"`+")
_TOKEN_RE: Final = re.compile(r"\S+")
# Punctuation around a token that is not part of the name it may hold.
_TOKEN_EDGES: Final = "()[]{}<>\"'`„”“«»,;:!?.*_"
_FILE_EXT_RE: Final = re.compile(r".\.[A-Za-z0-9]{2,8}$")
_LIST_ITEM_RE: Final = re.compile(r"[ ]{0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")


def _is_literal_token(token: str) -> bool:
    """Whether *token* (no whitespace) names something outside the reply.

    URLs, e-mail addresses, Windows / absolute / home / dot-relative paths,
    a directory ending in ``/``, a path of three or more parts whose first
    and last parts are ASCII (``docs/ţară/raport``), and anything ending in
    a file extension.  A bare ``a/b`` ("și/sau") and a slash list of words
    (``oţet/țelină/lămâie``) are prose.
    """
    if "://" in token or "@" in token or "\\" in token:
        return True
    core = token.strip(_TOKEN_EDGES)
    if not core:
        return False
    if core.startswith(("/", "./", "../", "~")) or core.endswith("/"):
        return True
    if core.count("/") >= 2:
        # ``docs/ţară/raport`` is a path, ``oţet/țelină/lămâie`` a list of
        # words: a path's first and last parts are plain ASCII names.
        parts = core.split("/")
        if parts[0].isascii() and parts[-1].isascii():
            return True
    if "/" in token and "." in core.rsplit("/", 1)[-1]:
        return True
    return bool(_FILE_EXT_RE.search(core))


def _block_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of fenced code, thinking blocks and display math.

    Each pattern's next match is cached and searched again only once the
    scan has passed it, so the text is scanned once per pattern however
    many blocks it holds.
    """
    spans: list[tuple[int, int]] = []
    n = len(text)
    patterns = (_FENCE_OPEN_RE, _THINK_OPEN_RE, _DISPLAY_MATH_RE)
    nexts: list[re.Match[str] | None] = [p.search(text) for p in patterns]
    pos = 0
    while pos < n:
        for k, pattern in enumerate(patterns):
            match = nexts[k]
            if match is not None and match.start() < pos:
                nexts[k] = pattern.search(text, pos)
        live = [(m.start(), k) for k, m in enumerate(nexts) if m is not None]
        if not live:
            break
        start, k = min(live)
        first = nexts[k]
        assert first is not None
        if k == 0:
            marker = first.group(1)
            # The closing fence: same character, at least as long, alone on
            # its line (any indent, trailing blanks).  None: to the end.
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
        elif k == 1:
            close_tag = f"</{first.group(1)}>"
            close_at = text.find(close_tag, first.end())
            end = n if close_at == -1 else close_at + len(close_tag)
        else:
            end = first.end()
        spans.append((start, end))
        pos = max(end, start + 1)
    return spans


def _indent_width(line: str) -> int:
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _indented_code_spans(
    text: str, blocks: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """``(start, end)`` of Markdown indented code blocks outside lists.

    A run of lines indented by four columns or more (a tab counts to the
    next multiple of four) that starts after a blank line, the start of the
    text or a block in *blocks*, and is not inside a list item (an indented
    line under ``- item`` / ``1. item`` is the item's continuation, as in
    Markdown).  Blank lines inside the run belong to it.
    """
    spans: list[tuple[int, int]] = []
    block_iter = iter(blocks)
    block = next(block_iter, None)
    prev_blank = True
    in_list = False
    code_start: int | None = None
    code_end = 0
    pos = 0
    for line in text.splitlines(keepends=True):
        start, pos = pos, pos + len(line)
        while block is not None and block[1] <= start:
            block = next(block_iter, None)
        if block is not None and block[0] <= start:
            # Inside a protected block: it ends any run, and counts as a
            # break before what follows it.
            if code_start is not None:
                spans.append((code_start, code_end))
                code_start = None
            prev_blank = True
            continue
        body = line.rstrip("\r\n")
        blank = not body.strip()
        indent = _indent_width(body)
        if code_start is not None:
            if blank or indent >= 4:
                if not blank:
                    code_end = start + len(body)
                continue
            spans.append((code_start, code_end))
            code_start = None
        if blank:
            prev_blank = True
            continue
        if indent >= 4:
            if prev_blank and not in_list:
                code_start, code_end = start, start + len(body)
            prev_blank = False
            continue
        if _LIST_ITEM_RE.match(body):
            in_list = True
        elif prev_blank:
            in_list = False
        prev_blank = False
    if code_start is not None:
        spans.append((code_start, code_end))
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
    """``(start, end)`` of the prose of *text*: what is outside fenced and
    indented code, thinking blocks, math, inline code, URLs, e-mail
    addresses, paths and file names.  Sorted, non-overlapping.
    """
    blocks = _block_spans(text)
    protected = sorted(blocks + _indented_code_spans(text, blocks))
    outer = _subtract(0, len(text), protected)
    result: list[tuple[int, int]] = []
    for s, e in outer:
        segment = text[s:e]
        code = [(s + a, s + b) for a, b in _inline_code_spans(segment)]
        for cs, ce in _subtract(s, e, code):
            math = [(cs + m.start(), cs + m.end()) for m in _INLINE_MATH_RE.finditer(text[cs:ce])]
            for ps, pe in _subtract(cs, ce, math):
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

#: Session config key: an "off" value (see :func:`enabled_for`) turns the
#: substitution off for the session; absent, the worker default applies.
CONFIG_KEY: Final[str] = "romanian_comma_below"
#: Key of a stored ``llm.response`` payload holding the reply as the model
#: wrote it, set only when the substitution changed it.
RAW_CONTENT_KEY: Final[str] = "raw_content"
#: Key of a stored ``llm.response`` payload holding how many letters differ
#: between :data:`RAW_CONTENT_KEY` and the stored content.
COUNT_KEY: Final[str] = "romanian_comma_below"

_OFF: Final = frozenset({"false", "0", "off", "no"})
_ON: Final = frozenset({"true", "1", "on", "yes"})


def enabled_for(config: dict | None, default: bool = True) -> bool:
    """Whether a session with *config* gets the substitution.

    ``False``, ``0`` and the strings "false", "0", "off", "no" (any case)
    turn it off; ``True``, non-zero numbers and "true", "1", "on", "yes"
    turn it on.  Absent or anything else: *default* (the worker setting).
    """
    value = (config or {}).get(CONFIG_KEY)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _OFF:
            return False
        if word in _ON:
            return True
    return default


def normalize_assistant_message(
    message: dict, config: dict | None = None, *, default: bool = True,
) -> int:
    """Apply :func:`normalize_reply_text` to *message*'s ``content`` in place.

    Only a string ``content`` is touched.  ``tool_calls`` (their arguments),
    ``reasoning`` / ``reasoning_content`` / ``reasoning_details`` and every
    other key are left as they are.  Returns how many letters were replaced
    (0 when *config* / *default* turn it off).
    """
    if not enabled_for(config, default):
        return 0
    content = message.get("content")
    if not isinstance(content, str):
        return 0
    new, replaced = normalize_reply_text(content)
    if replaced:
        message["content"] = new
    return replaced


def _same_text_but_the_swap(raw: object, content: object) -> bool:
    return (
        isinstance(raw, str)
        and isinstance(content, str)
        and len(raw) == len(content)
        and fold_cedillas(raw) == fold_cedillas(content)
    )


def settle_response_payload(
    payload: dict, config: dict | None = None, *, default: bool = True,
) -> None:
    """Fix the reply of an ``llm.response`` *payload* in place; idempotent.

    May run more than once on one payload (before a step that copies the
    reply elsewhere, and again when it is stored, after the content may
    have been revised).  Afterwards:

    * ``payload["message"]["content"]`` is the fixed text;
    * :data:`RAW_CONTENT_KEY` holds the text the model wrote and
      :data:`COUNT_KEY` how many letters differ, both only when they differ;
      a raw text that no longer matches the content (it was replaced by
      other text) is dropped.

    A content that is not a string (a reply moved into a tool call) leaves
    the payload as it is.
    """
    message = payload.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, str):
        return
    raw = payload.get(RAW_CONTENT_KEY)
    if not _same_text_but_the_swap(raw, content):
        raw = content
    normalize_assistant_message(message, config, default=default)
    fixed = message["content"]
    if raw != fixed:
        payload[RAW_CONTENT_KEY] = raw
        payload[COUNT_KEY] = sum(1 for a, b in zip(raw, fixed) if a != b)
    else:
        payload.pop(RAW_CONTENT_KEY, None)
        payload.pop(COUNT_KEY, None)


def sampled_content(payload: dict) -> object:
    """The content of a stored ``llm.response`` *payload* as the model wrote it.

    :data:`RAW_CONTENT_KEY` when it is present and differs from the stored
    content only by the swap, else the stored content.  For readers that
    need the model's own tokens (training export, RL rewards).
    """
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    raw = payload.get(RAW_CONTENT_KEY)
    return raw if _same_text_but_the_swap(raw, content) else content
