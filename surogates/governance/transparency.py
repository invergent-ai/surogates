"""EU AI Act Art. 13/50 disclosure copy.

The levels and their texts; the disclosure itself is delivered by
``surogates.runtime.governance`` (which resolves the level per agent)
and rendered by the channels and the public ``GET /transparency``
endpoint.
"""

from __future__ import annotations

from enum import Enum


class TransparencyLevel(str, Enum):
    """Disclosure level per EU AI Act risk classification."""

    NONE = "none"
    BASIC = "basic"
    ENHANCED = "enhanced"
    FULL = "full"


# Disclosure texts deliberately do NOT self-classify the system (the
# earlier drafts claimed "high-risk AI system", which is a legal
# classification under AI Act Art. 6 that a patient-communication or
# assistant agent does not carry — asserting it in end-user copy is
# both wrong and harmful). The levels scale detail, not risk claims.
#
# The notice goes out before the agent's own introduction, so it has to be
# in the user's language; each translation cites Art. 50(1) the way that
# language's edition of the Regulation does.
DISCLOSURE_TRANSLATIONS: dict[str, dict[TransparencyLevel, str]] = {
    "en": {
        TransparencyLevel.BASIC: (
            "You are interacting with an AI assistant. Replies are "
            "machine-generated and may contain errors. (EU AI Act Art. 50(1))"
        ),
        TransparencyLevel.ENHANCED: (
            "You are interacting with an AI assistant. Replies are "
            "machine-generated, may contain errors, and are logged. The AI "
            "follows the operator's usage policy and you can always ask for "
            "a human contact. (EU AI Act Art. 50(1))"
        ),
        TransparencyLevel.FULL: (
            "You are interacting with an AI assistant operated on the "
            "Surogate platform. Replies are machine-generated, may contain "
            "errors, and are logged and policy-governed; a human operator "
            "reviews escalations and you can request a human contact at any "
            "time. This notice is provided under EU AI Act Art. 50(1); "
            "further information is available from the operator."
        ),
    },
    "ro": {
        TransparencyLevel.BASIC: (
            "Vorbiți cu un asistent bazat pe inteligență artificială. "
            "Răspunsurile sunt generate automat și pot conține erori. "
            "(Regulamentul UE privind IA, art. 50 alin. (1))"
        ),
        TransparencyLevel.ENHANCED: (
            "Vorbiți cu un asistent bazat pe inteligență artificială. "
            "Răspunsurile sunt generate automat, pot conține erori și sunt "
            "înregistrate. Asistentul respectă politica de utilizare a "
            "operatorului, iar dumneavoastră puteți cere oricând să vorbiți "
            "cu o persoană. (Regulamentul UE privind IA, art. 50 alin. (1))"
        ),
        TransparencyLevel.FULL: (
            "Vorbiți cu un asistent bazat pe inteligență artificială, operat "
            "pe platforma Surogate. Răspunsurile sunt generate automat, pot "
            "conține erori, sunt înregistrate și respectă politicile "
            "operatorului; un operator uman analizează cazurile escaladate, "
            "iar dumneavoastră puteți cere oricând să vorbiți cu o persoană. "
            "Această informare este furnizată în temeiul art. 50 alin. (1) "
            "din Regulamentul UE privind IA; mai multe informații puteți "
            "obține de la operator."
        ),
    },
    "hu": {
        TransparencyLevel.BASIC: (
            "Ön egy mesterséges intelligencián alapuló asszisztenssel "
            "beszélget. A válaszok automatikusan készülnek, és hibákat "
            "tartalmazhatnak. (EU MI-rendelet, 50. cikk (1) bekezdés)"
        ),
        TransparencyLevel.ENHANCED: (
            "Ön egy mesterséges intelligencián alapuló asszisztenssel "
            "beszélget. A válaszok automatikusan készülnek, hibákat "
            "tartalmazhatnak, és rögzítjük őket. Az asszisztens az "
            "üzemeltető felhasználási szabályzatát követi, és Ön bármikor "
            "kérheti, hogy egy emberrel beszélhessen. (EU MI-rendelet, "
            "50. cikk (1) bekezdés)"
        ),
        TransparencyLevel.FULL: (
            "Ön egy mesterséges intelligencián alapuló asszisztenssel "
            "beszélget, amelyet a Surogate platformon üzemeltetnek. A "
            "válaszok automatikusan készülnek, hibákat tartalmazhatnak, "
            "rögzítjük őket, és szabályzatok szerint működnek; a "
            "továbbított eseteket emberi munkatárs vizsgálja meg, és Ön "
            "bármikor kérheti, hogy egy emberrel beszélhessen. Ezt a "
            "tájékoztatást az EU MI-rendelet 50. cikkének (1) bekezdése "
            "alapján nyújtjuk; további információt az üzemeltető ad."
        ),
    },
    "de": {
        TransparencyLevel.BASIC: (
            "Sie kommunizieren mit einem KI-Assistenten. Die Antworten "
            "werden automatisch erzeugt und können Fehler enthalten. "
            "(EU-KI-Verordnung, Art. 50 Abs. 1)"
        ),
        TransparencyLevel.ENHANCED: (
            "Sie kommunizieren mit einem KI-Assistenten. Die Antworten "
            "werden automatisch erzeugt, können Fehler enthalten und werden "
            "protokolliert. Der Assistent hält sich an die "
            "Nutzungsrichtlinie des Betreibers, und Sie können jederzeit "
            "einen menschlichen Ansprechpartner verlangen. "
            "(EU-KI-Verordnung, Art. 50 Abs. 1)"
        ),
        TransparencyLevel.FULL: (
            "Sie kommunizieren mit einem KI-Assistenten, der auf der "
            "Plattform Surogate betrieben wird. Die Antworten werden "
            "automatisch erzeugt, können Fehler enthalten, werden "
            "protokolliert und unterliegen Richtlinien; weitergeleitete "
            "Anliegen prüft ein Mensch, und Sie können jederzeit einen "
            "menschlichen Ansprechpartner verlangen. Dieser Hinweis erfolgt "
            "gemäß Art. 50 Abs. 1 der EU-KI-Verordnung; weitere "
            "Informationen erhalten Sie beim Betreiber."
        ),
    },
    "fr": {
        TransparencyLevel.BASIC: (
            "Vous échangez avec un assistant d'intelligence artificielle. "
            "Les réponses sont générées automatiquement et peuvent contenir "
            "des erreurs. (Règlement européen sur l'IA, art. 50, par. 1)"
        ),
        TransparencyLevel.ENHANCED: (
            "Vous échangez avec un assistant d'intelligence artificielle. "
            "Les réponses sont générées automatiquement, peuvent contenir "
            "des erreurs et sont enregistrées. L'assistant respecte la "
            "politique d'utilisation de l'opérateur et vous pouvez à tout "
            "moment demander à parler à une personne. (Règlement européen "
            "sur l'IA, art. 50, par. 1)"
        ),
        TransparencyLevel.FULL: (
            "Vous échangez avec un assistant d'intelligence artificielle "
            "exploité sur la plateforme Surogate. Les réponses sont générées "
            "automatiquement, peuvent contenir des erreurs, sont enregistrées "
            "et encadrées par des règles d'utilisation ; un opérateur humain "
            "examine les demandes transmises et vous pouvez à tout moment "
            "demander à parler à une personne. Cette information est fournie "
            "en application de l'article 50, paragraphe 1, du règlement "
            "européen sur l'IA ; l'opérateur peut vous fournir des "
            "informations complémentaires."
        ),
    },
    "it": {
        TransparencyLevel.BASIC: (
            "Sta interagendo con un assistente basato sull'intelligenza "
            "artificiale. Le risposte sono generate automaticamente e "
            "possono contenere errori. (Regolamento UE sull'IA, art. 50, "
            "par. 1)"
        ),
        TransparencyLevel.ENHANCED: (
            "Sta interagendo con un assistente basato sull'intelligenza "
            "artificiale. Le risposte sono generate automaticamente, "
            "possono contenere errori e vengono registrate. L'assistente "
            "segue la politica di utilizzo dell'operatore e Lei può "
            "chiedere in qualsiasi momento di parlare con una persona. "
            "(Regolamento UE sull'IA, art. 50, par. 1)"
        ),
        TransparencyLevel.FULL: (
            "Sta interagendo con un assistente basato sull'intelligenza "
            "artificiale, gestito sulla piattaforma Surogate. Le risposte "
            "sono generate automaticamente, possono contenere errori, "
            "vengono registrate e sono soggette a politiche di utilizzo; un "
            "operatore umano esamina i casi segnalati e Lei può chiedere in "
            "qualsiasi momento di parlare con una persona. Questa "
            "informativa è fornita ai sensi dell'art. 50, par. 1, del "
            "Regolamento UE sull'IA; ulteriori informazioni sono "
            "disponibili presso l'operatore."
        ),
    },
    "es": {
        TransparencyLevel.BASIC: (
            "Está interactuando con un asistente de inteligencia artificial. "
            "Las respuestas se generan automáticamente y pueden contener "
            "errores. (Reglamento de IA de la UE, art. 50, apdo. 1)"
        ),
        TransparencyLevel.ENHANCED: (
            "Está interactuando con un asistente de inteligencia artificial. "
            "Las respuestas se generan automáticamente, pueden contener "
            "errores y quedan registradas. El asistente sigue la política "
            "de uso del operador y puede pedir en cualquier momento hablar "
            "con una persona. (Reglamento de IA de la UE, art. 50, apdo. 1)"
        ),
        TransparencyLevel.FULL: (
            "Está interactuando con un asistente de inteligencia artificial "
            "gestionado en la plataforma Surogate. Las respuestas se generan "
            "automáticamente, pueden contener errores, quedan registradas y "
            "se rigen por políticas de uso; un operador humano revisa los "
            "casos escalados y puede pedir en cualquier momento hablar con "
            "una persona. Este aviso se facilita conforme al art. 50, "
            "apdo. 1, del Reglamento de IA de la UE; el operador puede "
            "proporcionarle más información."
        ),
    },
}

DEFAULT_DISCLOSURE_LANGUAGE = "en"

#: The ``transparency.language`` values with built-in copy; surogate-ops
#: mirrors this list in Studio.
DISCLOSURE_LANGUAGES = tuple(DISCLOSURE_TRANSLATIONS)

DISCLOSURE_TEXTS = DISCLOSURE_TRANSLATIONS[DEFAULT_DISCLOSURE_LANGUAGE]


def disclosure_text_for(
    level: TransparencyLevel, language: str = DEFAULT_DISCLOSURE_LANGUAGE,
) -> str:
    """The disclosure copy for a level, falling back to BASIC and to English.

    *language* is an ISO 639-1 code; a region suffix (``ro-RO``) is ignored.
    """
    code = str(language or "").strip().lower().replace("_", "-").split("-")[0]
    texts = DISCLOSURE_TRANSLATIONS.get(code, DISCLOSURE_TEXTS)
    return texts.get(level, texts[TransparencyLevel.BASIC])
