"""The fixed lines a voice line speaks without its agent: the greeting, the filler while a tool runs,
"are you still there?", and the apologies for when the agent cannot answer at all.

They are fixed on purpose: the greeting must play the instant the call connects and the apologies
play when the agent's model is down, so none of them can be written by the model during the call.
A line's owner edits them in Studio (Studio fills in a translation for any other language when the
line's language is set); the defaults below cover a line that never set them.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any


@dataclass(frozen=True)
class Lines:
    greeting: str
    filler: str  # said for the agent when it starts a tool without a word
    still_there: str  # the caller has been silent a while
    goodbye: str  # the call reached its time limit, or the caller stayed silent
    sorry: str  # the call cannot start (the agent's session failed to open)
    sorry_turn: str  # one answer failed or took too long; the call goes on
    unavailable: str  # the number has no agent
    busy: str  # every line is taken
    choices: str  # how an ask_user_question's choices are read out; {} is the list


DEFAULTS: dict[str, Lines] = {
    "ro": Lines(
        greeting="Bună ziua! Sunt asistentul virtual. Cu ce vă pot ajuta?",  # says it is an AI (EU AI Act art. 50)
        filler="O clipă, verific.",
        still_there="Mai sunteți acolo?",
        goodbye="Vă mulțumesc că ați sunat. O zi bună!",
        sorry="Îmi pare rău, am o problemă tehnică. Vă rog să sunați puțin mai târziu.",
        sorry_turn="Îmi pare rău, nu am reușit să răspund acum. Vă rog să mai întrebați o dată.",
        unavailable="Acest număr nu este disponibil momentan.",
        busy="Toate liniile sunt ocupate. Vă rog să reveniți în câteva minute.",
        choices="Variante: {}.",
    ),
    "en": Lines(
        greeting="Hello! I'm the virtual assistant. How can I help you?",
        filler="One moment, let me check.",
        still_there="Are you still there?",
        goodbye="Thank you for calling. Have a nice day!",
        sorry="I'm sorry, I have a technical problem. Please call again a little later.",
        sorry_turn="I'm sorry, I couldn't answer just now. Please ask me again.",
        unavailable="This number is not available at the moment.",
        busy="All our lines are busy. Please call again in a few minutes.",
        choices="The options are: {}.",
    ),
}
FALLBACK = "en"  # a language with no defaults: the owner's own lines, else English ones
# What the agent is told the call's language is. A tag missing here is said as itself ("sw"): models
# know the codes, the names just read better.
NAMES = {"ro": "Romanian", "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
         "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "hu": "Hungarian", "cs": "Czech", "sk": "Slovak",
         "bg": "Bulgarian", "el": "Greek", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "sv": "Swedish",
         "da": "Danish", "no": "Norwegian", "fi": "Finnish", "ar": "Arabic", "he": "Hebrew", "hi": "Hindi",
         "ja": "Japanese", "ko": "Korean", "zh": "Chinese"}


def default_lines(language: str) -> Lines:
    return DEFAULTS.get(language.split("-")[0], DEFAULTS[FALLBACK])


def language_name(language: str) -> str:
    return NAMES.get(language.split("-")[0], language)


def lines_from_routing(language: str, cfg: Any) -> Lines:
    """The line's own lines over its language's defaults. A missing or non-text line keeps its default,
    and a choices line without its ``{}`` would drop the choices, so it keeps its default too."""
    cfg = cfg if isinstance(cfg, dict) else {}
    own = cfg.get("lines") if isinstance(cfg.get("lines"), dict) else {}
    own = {**own, "greeting": cfg.get("greeting")}  # the greeting predates the other lines: it sits on the row
    base = default_lines(language)
    changes = {f.name: own[f.name].strip() for f in fields(Lines)
               if isinstance(own.get(f.name), str) and own[f.name].strip()}
    if "{}" not in changes.get("choices", "{}"):
        del changes["choices"]
    return replace(base, **changes)
