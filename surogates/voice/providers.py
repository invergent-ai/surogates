"""The speech providers a voice line can use: ours, or the owner's own account at another provider.

The voice channel is one engine (LiveKit, turn-taking, the background, pronunciations, call reports).
Only hearing (speech to text) and speaking (text to speech) are swapped, each on its own: a line can
hear with one provider and speak with another. This module is the catalog both sides read (Studio's
forms and ops' checks import it, the voice worker builds from it), plus the two account calls ops
makes with an owner's key: is it valid, and which voices can it use. Nothing here imports LiveKit;
building the plugins is ``surogates.voice.speech``.

Every provider is used through its official LiveKit plugin. ``defaults`` exist because several
plugins default to models their provider has retired: a model is always chosen explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

SUROGATE = "surogate"  # our own models: Romanian, voices "female" and "male", no key


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    languages: tuple[str, ...] = ()  # empty: many, see the provider's docs; Studio then lists every language


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    stt: tuple[Model, ...] = ()  # first is the recommended one
    tts: tuple[Model, ...] = ()
    needs_key: bool = True
    needs_url: bool = False  # a server of the owner's (Hugging Face endpoint, self-hosted, OpenAI-compatible)
    voices: str = "list"  # "list": the account's voices API; "fixed": the catalog's own list; "id": typed in
    fixed_voices: tuple[str, ...] = ()
    library: bool = False  # has a public voice library besides the account's own (Studio's "Library" tab)
    searches: bool = True  # filters voices by name and language itself; if not, list_voices does it here
    keys_url: str = ""  # where an owner creates a key (Studio links it)
    note: str = ""  # what the key needs, shown beside the key field


FLASH_LANGUAGES = ("en", "ja", "zh", "de", "hi", "fr", "ko", "pt", "it", "es", "id", "nl", "tr", "fil", "pl", "sv",
                   "bg", "ro", "ar", "cs", "el", "fi", "hr", "ms", "sk", "da", "ta", "uk", "ru", "hu", "no", "vi")

PROVIDERS: dict[str, Provider] = {p.id: p for p in (
    Provider(SUROGATE, "Surogate", needs_key=False, voices="fixed", fixed_voices=("female", "male"),
             stt=(Model("jackrabbit-110m-ro-streaming", "Surogate Romanian", ("ro",)),),
             tts=(Model("amami-110m-ro", "Surogate Romanian", ("ro",)),)),
    Provider("elevenlabs", "ElevenLabs",
             stt=(Model("scribe_v2_realtime", "Scribe v2 Realtime"),),
             tts=(Model("eleven_flash_v2_5", "Flash v2.5, fastest", FLASH_LANGUAGES),
                  Model("eleven_v4_turbo", "v4 Turbo, 90+ languages")),
             library=True,
             keys_url="https://elevenlabs.io/app/settings/api-keys",
             note="Enable Text to Speech, Speech to Text and Voices read."),
    Provider("cartesia", "Cartesia",
             stt=(Model("ink-whisper", "Ink Whisper"),),
             tts=(Model("sonic-3.6", "Sonic 3.6"),),
             library=True,
             keys_url="https://play.cartesia.ai/keys"),
    Provider("deepgram", "Deepgram",
             stt=(Model("nova-3", "Nova-3"), Model("flux-general-en", "Flux, English", ("en",))),
             # Deepgram's voices are its models ("aura-2-thalia-en"): the voice picked is the model built
             tts=(Model("aura-2", "Aura-2", ("en", "es", "de", "fr", "nl", "it", "ja")),),
             searches=False,
             keys_url="https://console.deepgram.com/"),
    Provider("openai", "OpenAI",
             stt=(Model("gpt-4o-transcribe", "GPT-4o Transcribe"), Model("gpt-4o-mini-transcribe", "GPT-4o mini Transcribe")),
             tts=(Model("gpt-4o-mini-tts", "GPT-4o mini TTS"), Model("tts-1", "TTS-1")),
             voices="fixed", fixed_voices=("alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage",
                                           "shimmer", "verse", "marin", "cedar"),
             keys_url="https://platform.openai.com/api-keys"),
    Provider("gradium", "Gradium",
             stt=(Model("default", "Gradium STT", ("en", "fr", "de", "es", "pt")),),
             tts=(Model("default", "Gradium TTS", ("en", "fr", "de", "es", "pt")),),
             library=True, searches=False,
             keys_url="https://gradium.ai/"),
    Provider("fishaudio", "Fish Audio",
             tts=(Model("s2.1-pro", "S2.1 Pro"), Model("s1", "S1")),
             library=True,
             keys_url="https://fish.audio/app/api-keys/"),
    # Any speech model behind an OpenAI-compatible API: a Hugging Face Inference Endpoint, Speaches,
    # Kokoro-FastAPI, vLLM. The model and voice are whatever that server calls them.
    Provider("openai_compat", "Hugging Face or your own server", needs_key=False, needs_url=True, voices="id",
             stt=(Model("", "Your speech to text model"),), tts=(Model("", "Your text to speech model"),),
             note="An OpenAI-compatible base URL, for example https://…/v1. A key only if the server needs one."),
)}


# --- the owner's account: is the key good, which voices can it use (ops calls these) -----------------

@dataclass(frozen=True)
class Status:
    ok: bool
    code: str = ""  # invalid_key | forbidden | no_credits | rate_limited | unreachable | error
    message: str = ""


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    gender: str = ""
    accent: str = ""
    languages: tuple[str, ...] = ()
    preview_url: str = ""
    custom: bool = False  # the owner's own (a clone, or one they designed)


@dataclass(frozen=True)
class Conn:
    """An owner's connection to a provider: the decrypted key (never logged) and their server's URL."""
    provider: str
    api_key: str = ""
    base_url: str = ""


MESSAGES = {
    401: ("invalid_key", "The provider did not accept this key."),
    402: ("no_credits", "The account has no credits left."),
    403: ("forbidden", "The key is missing a permission this needs."),
    429: ("rate_limited", "The provider is limiting requests. Try again in a minute."),
}


def _status(r: httpx.Response) -> Status:
    if r.is_success:
        return Status(True)
    code, message = MESSAGES.get(r.status_code, ("error", f"The provider answered {r.status_code}."))
    if r.status_code == 401 and "quota" in r.text.lower():  # ElevenLabs reports an empty quota as 401
        code, message = MESSAGES[402]
    return Status(False, code, message)


def _request(conn: Conn, voices: bool, query: str = "", language: str = "", library: bool = False,
             ) -> tuple[str, str, dict, dict]:
    """The provider's cheapest listing call: (method, url, headers, params). It costs nothing and needs the
    same permission as listing voices, so it doubles as the key check."""
    p, k, n = conn.provider, conn.api_key, 100 if voices else 1
    if p == "elevenlabs":
        if library:
            params = {"page_size": n, "search": query, "language": language}
            return "GET", "https://api.elevenlabs.io/v1/shared-voices", {"xi-api-key": k}, params
        return "GET", "https://api.elevenlabs.io/v2/voices", {"xi-api-key": k}, {"page_size": n, "search": query}
    if p == "cartesia":
        params = {"limit": n, "q": query, "language": language, "expand[]": "preview_file_url",
                  "is_owner": "true" if voices and not library else None}
        return "GET", "https://api.cartesia.ai/voices", {"Authorization": f"Bearer {k}",
                                                         "Cartesia-Version": "2026-08-14"}, params
    if p == "deepgram":
        return "GET", "https://api.deepgram.com/v1/models", {"Authorization": f"Token {k}"}, {}
    if p == "openai":
        return "GET", "https://api.openai.com/v1/models", {"Authorization": f"Bearer {k}"}, {}
    if p == "gradium":
        return "GET", "https://api.gradium.ai/api/voices/", {"x-api-key": k}, {"limit": n, "include_catalog": "true" if library else None}
    if p == "fishaudio":
        params = {"page_size": n, "title": query, "language": language, "self": "false" if library else "true"}
        return "GET", "https://api.fish.audio/model", {"Authorization": f"Bearer {k}"}, params
    if p == "openai_compat":
        return "GET", f"{conn.base_url.rstrip('/')}/models", {"Authorization": f"Bearer {k}"} if k else {}, {}
    raise ValueError(f"unknown provider {p!r}")


async def check(conn: Conn, client: httpx.AsyncClient) -> Status:
    """Can this connection be used? One free call; never raises."""
    if conn.provider == SUROGATE:
        return Status(True)
    try:
        method, url, headers, params = _request(conn, voices=False)
        r = await client.request(method, url, headers=headers, params={k: v for k, v in params.items() if v})
    except (httpx.HTTPError, ValueError) as e:
        return Status(False, "unreachable", f"Could not reach the provider ({type(e).__name__}).")
    return _status(r)


def _voices(pid: str, body) -> list[Voice]:
    """One provider's answer, in our shape."""
    if pid == "elevenlabs":
        out = []
        for v in body.get("voices", []):
            labels = v.get("labels") or {}
            langs = tuple({x.get("language") for x in v.get("verified_languages") or [] if x.get("language")}
                          or ({v["language"]} if v.get("language") else set()))
            out.append(Voice(v["voice_id"], v.get("name", ""), labels.get("gender") or v.get("gender", ""),
                             labels.get("accent") or v.get("accent", ""), langs, v.get("preview_url") or "",
                             v.get("category") in ("cloned", "professional", "generated")))
        return out
    if pid == "cartesia":
        return [Voice(v["id"], v.get("name", ""), v.get("gender") or "", "", (v["language"],) if v.get("language") else (),
                      v.get("preview_file_url") or "", bool(v.get("is_owner"))) for v in body.get("data", [])]
    if pid == "deepgram":
        return [Voice(m["canonical_name"], m.get("name", ""), "", (m.get("metadata") or {}).get("accent", ""),
                      tuple(m.get("languages") or ()), (m.get("metadata") or {}).get("sample", ""))
                for m in body.get("tts", [])]
    if pid == "gradium":
        return [Voice(v["uid"], v.get("name", ""), "", "", (v["language"],) if v.get("language") else (), "",
                      not v.get("is_catalog", False)) for v in body]
    if pid == "fishaudio":
        return [Voice(v["_id"], v.get("title", ""), "", "", tuple(v.get("languages") or ()),
                      next((s["audio"] for s in v.get("samples") or [] if s.get("audio")), ""))
                for v in body.get("items", [])]
    return []


async def list_voices(conn: Conn, client: httpx.AsyncClient, *, query: str = "", language: str = "",
                      library: bool = False) -> tuple[Status, list[Voice]]:
    """The voices this account can speak with: its own, or the provider's public library."""
    p = PROVIDERS.get(conn.provider)
    if p is None:
        return Status(False, "error", "Unknown provider."), []
    if p.voices == "fixed":
        return Status(True), [Voice(v, v.capitalize()) for v in p.fixed_voices]
    if p.voices == "id":
        return Status(True), []
    try:
        method, url, headers, params = _request(conn, voices=True, query=query, language=language, library=library)
        r = await client.request(method, url, headers=headers, params={k: v for k, v in params.items() if v})
    except httpx.HTTPError as e:
        return Status(False, "unreachable", f"Could not reach the provider ({type(e).__name__})."), []
    status = _status(r)
    if not status.ok:
        return status, []
    try:
        voices = _voices(conn.provider, r.json())
    except (ValueError, KeyError, TypeError, AttributeError):  # a proxy's page, a changed API
        return Status(False, "error", "The provider answered in a way we could not read."), []
    if query and not p.searches:
        q = query.lower()
        voices = [v for v in voices if q in v.name.lower() or q in v.id.lower()]
    if language and not p.searches:
        voices = [v for v in voices if not v.languages or any(x.split("-")[0] == language for x in v.languages)]
    return status, voices
