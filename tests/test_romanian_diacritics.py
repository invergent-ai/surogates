"""Romanian cedilla s/t -> comma-below in assistant replies.

Unit tests of :mod:`surogates.harness.romanian_diacritics`.  The call-site
tests (the harness loop, the OpenAI stream catch-up) are in
``tests/test_romanian_reply_call_site.py``.
"""

from __future__ import annotations

import pytest

from surogates.harness.romanian_diacritics import (
    CEDILLA_TO_COMMA_BELOW,
    enabled_for,
    fold_cedillas,
    looks_romanian,
    normalize_assistant_message,
    normalize_reply_text,
    prose_spans,
)

CEDILLAS = "şţŞŢ"  # ş ţ Ş Ţ
COMMAS = "șțȘȚ"    # ș ț Ș Ț


def _only_the_four_letters_changed(before: str, after: str) -> None:
    assert len(before) == len(after)
    for a, b in zip(before, after):
        if a != b:
            assert CEDILLA_TO_COMMA_BELOW[a] == b


def test_mapping_is_exactly_the_four_letters() -> None:
    assert CEDILLA_TO_COMMA_BELOW == dict(zip(CEDILLAS, COMMAS))
    assert fold_cedillas(CEDILLAS) == COMMAS
    assert fold_cedillas("şţŞŢ çğıöü ăâî") == "șțȘȚ çğıöü ăâî"


def test_romanian_reply_with_mixed_forms_is_fixed() -> None:
    text = (
        "Pentru cartuş/cartridge: scoate cartuşul, înlocuieşte-l şi verifică "
        "dacă imprimanta funcționează. Ţine apăsat butonul până când "
        "indicatorul se aprinde; în caz contrar, Şterge coada de imprimare."
    )
    fixed, replaced = normalize_reply_text(text)
    assert replaced == 6
    assert not any(ch in fixed for ch in CEDILLAS)
    assert "cartuș/cartridge" in fixed and "cartușul" in fixed
    assert "înlocuiește-l și verifică" in fixed
    assert "Ține apăsat" in fixed and "Șterge coada" in fixed
    # The comma-below letters already right stay as they were.
    assert "funcționează" in fixed
    _only_the_four_letters_changed(text, fixed)


def test_turkish_reply_is_byte_identical() -> None:
    text = (
        "Bu işlem için önce ayarları açın ve şifrenizi değiştirin. "
        "Şu anda sistem çalışıyor, ama değişiklik yapmak için yönetici "
        "izni gerekir. Teşekkürler!"
    )
    fixed, replaced = normalize_reply_text(text)
    assert replaced == 0
    assert fixed is text
    assert fixed.encode("utf-8") == text.encode("utf-8")


@pytest.mark.parametrize("text", [
    # Azerbaijani (ə), Gagauz (ä, ţ), Kurdish Kurmanji (ê, û): all keep ş / ţ.
    "Bu gün hava çox gözəldir və mən şəhərə gedirəm.",
    "Gagauz dilindä şkola hem universitet var, ţerkvi dä.",
    "Ez ê sibê biçim şaredariyê û karê xwe bikim.",
    # Turkish without ı / ğ, but with ö / ü.
    "Güneş doğdu; köprü üstünde şarkı söylüyoruz.",
])
def test_other_languages_with_cedilla_letters_are_untouched(text: str) -> None:
    fixed, replaced = normalize_reply_text(text)
    assert (fixed, replaced) == (text, 0)
    assert fixed is text


def test_romanian_reply_with_code_keeps_code_and_fixes_prose() -> None:
    text = (
        "Iată scriptul care şterge fişierele vechi din directorul de lucru:\n"
        "\n"
        "```python\n"
        "# şterge fişierele\n"
        "mesaj = 'Aţi şters totul'\n"
        "```\n"
        "\n"
        "Rulaţi-l cu `python sterge.py --ţintă=vechi` şi verificaţi "
        "rezultatul în jurnal; variabila ``ţară`` nu se schimbă.\n"
        "~~~\n"
        "şablon ţintă\n"
        "~~~\n"
        "Gata, aşa e mai bine pentru toţi."
    )
    fixed, replaced = normalize_reply_text(text)
    _only_the_four_letters_changed(text, fixed)
    # Code untouched.
    assert "# şterge fişierele\nmesaj = 'Aţi şters totul'\n" in fixed
    assert "`python sterge.py --ţintă=vechi`" in fixed
    assert "``ţară``" in fixed
    assert "~~~\nşablon ţintă\n~~~" in fixed
    # Prose fixed.
    assert fixed.startswith("Iată scriptul care șterge fișierele vechi")
    assert "Rulați-l cu" in fixed and "și verificați" in fixed
    assert fixed.endswith("Gata, așa e mai bine pentru toți.")
    assert replaced == 7


def test_unclosed_fence_protects_the_rest_of_the_reply() -> None:
    text = (
        "Aceasta este soluţia pentru problema cu fişierul:\n"
        "```bash\n"
        "echo 'Aţi terminat'  # şi gata\n"
    )
    fixed, _ = normalize_reply_text(text)
    assert fixed.startswith("Aceasta este soluția pentru problema cu fișierul:\n")
    assert fixed.endswith("```bash\necho 'Aţi terminat'  # şi gata\n")


def test_urls_paths_and_file_names_are_kept() -> None:
    text = (
        "Am salvat raportul în /home/user/rapoarte/situaţie.docx şi în "
        "raport_ţară.xlsx; sursa este https://exemplu.ro/ştiri/azi, iar "
        "adresa de contact este ştefan@exemplu.ro. Varianta şi/sau "
        "cealaltă sunt în ./date/ţări/ pentru toţi."
    )
    fixed, _ = normalize_reply_text(text)
    for literal in (
        "/home/user/rapoarte/situaţie.docx",
        "raport_ţară.xlsx;",
        "https://exemplu.ro/ştiri/azi,",
        "ştefan@exemplu.ro.",
        "./date/ţări/",
    ):
        assert literal in fixed
    assert "Am salvat raportul" in fixed and " și în " in fixed
    assert "Varianta și/sau" in fixed and "pentru toți." in fixed


def test_thinking_tags_left_in_content_are_untouched() -> None:
    text = (
        "<think>Utilizatorul vrea şi lista; mă gândesc la ţintă.</think>"
        "Iată lista cerută pentru echipă: mere, pere şi prune, în această ordine."
    )
    fixed, replaced = normalize_reply_text(text)
    assert fixed.startswith("<think>Utilizatorul vrea şi lista; mă gândesc la ţintă.</think>")
    assert fixed.endswith("pere și prune, în această ordine.")
    assert replaced == 1


def test_romanian_reply_quoting_turkish_is_left_alone() -> None:
    """Chosen behaviour: a Romanian reply that quotes Turkish (any Turkic
    letter such as ı ğ ö ü ç anywhere in its prose) is left entirely as it
    is.  Fixing it would turn the Turkish ş into a Romanian ș; leaving it
    keeps the Romanian ş wrong.  The second error is the cosmetic one."""
    text = (
        "În turcă, salutul „Günaydın, nasılsınız?” înseamnă „Bună dimineaţa, "
        "ce mai faceţi?”, iar „teşekkür ederim” este mulţumirea uzuală şi "
        "se foloseşte foarte des."
    )
    fixed, replaced = normalize_reply_text(text)
    assert (fixed, replaced) == (text, 0)
    assert fixed is text


def test_english_reply_is_unchanged() -> None:
    for text in (
        "The deployment finished. Run the tests and check the logs.",
        # English with a Turkish name holding ş: still not Romanian.
        "Contact Ayşe Kaya about the invoice; she is in the Istanbul office.",
        # English quoting one Romanian word: below the Romanian threshold.
        "The Romanian word for 'school' is şcoală, spelled with ș today.",
    ):
        fixed, replaced = normalize_reply_text(text)
        assert (fixed, replaced) == (text, 0)
        assert fixed is text


@pytest.mark.parametrize("text", ["", " ", "\n\t \n", "ş", "  ţ  "])
def test_empty_and_whitespace_replies(text: str) -> None:
    fixed, replaced = normalize_reply_text(text)
    assert (fixed, replaced) == (text, 0)


def test_whitespace_and_other_characters_are_preserved() -> None:
    text = "  Aceasta  este\tsoluţia\r\n\npentru  voi şi  pentru noi, în  fine.  \n"
    fixed, replaced = normalize_reply_text(text)
    assert replaced == 2
    assert fixed == "  Aceasta  este\tsoluția\r\n\npentru  voi și  pentru noi, în  fine.  \n"


def test_decomposed_and_other_diacritics_are_not_normalised() -> None:
    """No NFC pass: a decomposed ă (a + U+0306) stays decomposed, and the
    cedilla letters built from s + U+0327 are not the precomposed ones the
    mapping covers, so they stay too."""
    text = "Aceasta este o propoziţie pentru voi: măr si̧ păr, în fine."
    fixed, replaced = normalize_reply_text(text)
    assert replaced == 1
    assert "măr si̧ păr" in fixed
    assert "propoziție" in fixed


def test_short_romanian_reply_below_threshold_is_left() -> None:
    """Prefer false negatives: too little evidence, nothing changes."""
    text = "Mulţumesc!"
    assert normalize_reply_text(text) == (text, 0)


def test_looks_romanian() -> None:
    assert looks_romanian("Aceasta este o propoziţie în limba română, pentru voi.")
    assert not looks_romanian("Bu bir Türkçe cümledir ve şimdi bitiyor.")
    assert not looks_romanian("This is English, plain and simple.")
    assert not looks_romanian("")


def test_prose_spans_skip_code() -> None:
    text = "a `b` c\n```\nd\n```\ne"
    pieces = [text[s:e] for s, e in prose_spans(text)]
    assert "".join(pieces).replace(" ", "").replace("\n", "") == "ace"


def test_normalize_assistant_message_touches_only_content() -> None:
    message = {
        "role": "assistant",
        "content": "Am terminat raportul şi l-am trimis echipei pentru verificare, aşa cum aţi cerut.",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "write_file", "arguments": '{"text": "Aşa şi aşa, în ţară"}'},
        }],
        "reasoning_content": "Trebuie să scriu şi să trimit, aşa că fac asta.",
        "reasoning": "şi aşa",
    }
    replaced = normalize_assistant_message(message, {})
    assert replaced == 3
    assert message["content"] == (
        "Am terminat raportul și l-am trimis echipei pentru verificare, așa cum ați cerut."
    )
    assert message["tool_calls"][0]["function"]["arguments"] == '{"text": "Aşa şi aşa, în ţară"}'
    assert message["reasoning_content"] == "Trebuie să scriu şi să trimit, aşa că fac asta."
    assert message["reasoning"] == "şi aşa"


def test_config_switch() -> None:
    assert enabled_for(None) and enabled_for({}) and enabled_for({"romanian_comma_below": True})
    assert not enabled_for({"romanian_comma_below": False})
    content = "Aceasta este soluţia pentru voi şi pentru ei, în fine."
    message = {"role": "assistant", "content": content}
    assert normalize_assistant_message(message, {"romanian_comma_below": False}) == 0
    assert message["content"] is content
    for value in (None, ["list"], {"text": "ş"}):
        msg = {"role": "assistant", "content": value}
        assert normalize_assistant_message(msg, {}) == 0
        assert msg["content"] == value
