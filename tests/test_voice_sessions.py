"""Who a call's session belongs to, and whose memory it may use. The store-backed turn tests are in
tests/integration/test_voice_call_session.py (the session store's SQL is Postgres-only)."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from surogates.voice import sessions as voice_sessions
from surogates.voice.lines import default_lines
from surogates.voice.sessions import CallSession, CallTarget, VoiceSessions, normalize_caller, question_text

SORRY_TURN = default_lines("ro").sorry_turn
FILLER = default_lines("ro").filler


@pytest.mark.parametrize("raw, expected", [("+40722000111", "40722000111"), ("40722000111", "40722000111"),
                                           ("", "anonymous"), (None, "anonymous"), ("anonymous", "anonymous")])
def test_normalize_caller(raw, expected):
    assert normalize_caller(raw) == expected


async def _open(monkeypatch, *, caller, remember):
    seen = {}

    async def identity(sf, *, platform, platform_user_id, org_id, display_name=""):
        seen["identity"] = (platform, platform_user_id)
        return type("Ident", (), {"user_id": uuid4()})()

    async def session(store, redis, *, session_key, config, channel, **kw):
        seen["session"] = (session_key, channel, config)
        return uuid4()

    monkeypatch.setattr(voice_sessions, "get_or_create_channel_identity", identity)
    monkeypatch.setattr(voice_sessions, "get_or_create_channel_session", session)
    vs = VoiceSessions(store=None, redis=None, session_factory=None)
    target = CallTarget(org_id=uuid4(), agent_id="agent-1", remember_callers=remember)
    call = await vs.open_call(target, call_id="SCL_1", called="+40300000001", caller=caller, greeting="Bună ziua!")
    return call, seen


async def test_open_call_isolates_each_call_by_default(monkeypatch):
    call, seen = await _open(monkeypatch, caller="+40722000111", remember=False)
    key, channel, config = seen["session"]
    assert (key, channel, config["memory_boundary"]) == ("agent:voice:call:SCL_1", "voice", "voice:call:SCL_1")
    # its own identity too: session_search is scoped by user, and a caller ID can be spoofed
    assert seen["identity"] == ("voice", "call:SCL_1")


async def test_open_call_remembers_a_known_number_when_the_agent_asks(monkeypatch):
    _, seen = await _open(monkeypatch, caller="+40722000111", remember=True)
    assert seen["session"][2]["memory_boundary"] == "phone:40722000111"
    assert seen["identity"] == ("voice", "40722000111")


async def test_open_call_withheld_number_never_remembers(monkeypatch):
    call, seen = await _open(monkeypatch, caller=None, remember=True)
    assert seen["identity"] == ("voice", "call:SCL_1")  # withheld callers never share one user
    assert seen["session"][2]["memory_boundary"] == "voice:call:SCL_1"
    assert call.caller == "anonymous"


def test_the_agents_question_is_read_from_the_real_tool_schema():
    """ask_user_question sends {"questions": [{"prompt", "choices": [{"label"}]}]} (tools/builtin/ask_user_question.py)."""
    one = {"questions": [{"prompt": "Ce zi vă convine?", "choices": [{"label": "luni"}, {"label": "marți"}]}]}
    assert question_text(json.dumps(one)) == "Ce zi vă convine? Variante: luni, marți."
    two = {"questions": [{"prompt": "Cum vă numiți?"}, {"prompt": "Pentru ce dată?", "choices": [{"label": "azi"}]}]}
    assert question_text(two) == "Cum vă numiți? Pentru ce dată? Variante: azi."
    assert question_text("not json") == "" and question_text({"questions": "x"}) == ""


class _Log:
    """An event log the test writes to: what the harness would have written so far."""

    def __init__(self, *events):
        self.events = [SimpleNamespace(id=i + 1, type=t, data=d) for i, (t, d) in enumerate(events)]

    async def get_events(self, session_id, after=None, **_):
        return [e for e in self.events if after is None or e.id > after]

    async def emit_event(self, session_id, event_type, data, **_):
        self.events.append(SimpleNamespace(id=len(self.events) + 1, type=event_type.value, data=data))
        return len(self.events)


class _Wire:
    def __init__(self):
        self.published = []

    def pubsub(self):
        return SimpleNamespace(subscribe=_noop, get_message=_noop, aclose=_noop)

    async def publish(self, channel, payload):
        self.published.append(json.loads(payload))


async def _noop(*_, **__):
    return None


def _call(log):
    return CallSession(store=log, redis=_Wire(), session_id=uuid4(), org_id=uuid4(), agent_id="a", user_id=uuid4(),
                       caller="40722000111")


async def test_a_turn_that_never_starts_ends_with_an_apology_and_a_stop():
    call = _call(_Log())  # the harness never picks the turn up
    assert [t async for t in call.stream(0, start_timeout=0.3)] == [SORRY_TURN]
    assert call.redis.published == [{"reason": "channel_stop"}]


async def test_a_turn_that_never_finishes_ends_with_an_apology():
    call = _call(_Log(("llm.request", {}), ("llm.delta", {"content": "O clipă, verific. "})))
    assert [t async for t in call.stream(0, turn_timeout=0.3)] == ["O clipă, verific. ", SORRY_TURN]


async def test_an_answer_written_without_streaming_is_still_spoken():
    call = _call(_Log(("llm.request", {}), ("llm.response", {"message": {"role": "assistant", "content": "Euro e 4,97 lei."}})))
    assert [t async for t in call.stream(0)] == ["Euro e 4,97 lei."]


async def test_cut_while_the_agent_runs_a_tool_does_not_repeat_its_preamble():
    """The preamble is already in the history as the reply that called the tool."""
    log = _Log(("user.message", {"content": "Cât e euro?"}), ("llm.request", {}),
               ("llm.response", {"message": {"role": "assistant", "content": "O clipă, verific.", "tool_calls": [{"id": "1"}]}}))
    call = _call(log)
    call._user_event = 1
    await call.record_heard("O clipă, verific.")
    assert [e.type for e in log.events].count("llm.response") == 1
    assert call.note.startswith("[The caller interrupted you")


async def test_cut_greeting_invents_no_reply_and_says_what_was_heard():
    call = _call(_Log())
    call.note = "[This call is in Romanian: speak only Romanian.] [You already answered the phone with: «Bună ziua! Sunt Ana, cu ce vă pot ajuta?».] "
    await call.record_heard("Bună ziua! Sunt")
    assert call.store.events == []
    assert call.note == ("[This call is in Romanian: speak only Romanian.] "
                         "[You answered the phone, but the caller interrupted you after: «Bună ziua! Sunt».] ")


async def test_a_question_the_agent_already_said_is_not_said_twice():
    """The model often writes the question as text and then calls ask_user_question with the same prompt."""
    args = {"questions": [{"prompt": "În ce zi doriți programarea?", "choices": [{"label": "luni"}, {"label": "marți"}]}]}
    call = _call(_Log(("llm.request", {}), ("llm.delta", {"content": "Sigur. În ce zi doriți programarea?"}),
                      ("tool.call", {"name": "ask_user_question", "arguments": json.dumps(args)})))
    assert [t async for t in call.stream(0)] == ["Sigur. În ce zi doriți programarea?", " Variante: luni, marți."]


async def test_a_paraphrased_question_is_not_asked_twice():
    """The model rarely repeats its own words exactly: "în ce zi doriți să vă programez?" then the tool's
    "În ce zi doriți programarea?". If what it just said ends in a question, only the choices are added."""
    args = {"questions": [{"prompt": "În ce zi doriți programarea?", "choices": [{"label": "luni"}, {"label": "marți"}]}]}
    call = _call(_Log(("llm.request", {}), ("llm.delta", {"content": "Sigur. În ce zi doriți să vă programez? "}),
                      ("tool.call", {"name": "ask_user_question", "arguments": json.dumps(args)})))
    assert [t async for t in call.stream(0)] == ["Sigur. În ce zi doriți să vă programez? ", " Variante: luni, marți."]


async def test_a_tool_started_in_silence_is_announced():
    """The model often calls a tool without a word first; the caller would hear only typing for seconds."""
    call = _call(_Log(("llm.request", {}), ("tool.call", {"name": "web_search", "arguments": "{}"}),
                      ("tool.result", {"name": "web_search"}), ("llm.request", {}),
                      ("llm.delta", {"content": "Euro e 4,97 lei."}),
                      ("llm.response", {"message": {"role": "assistant", "content": "Euro e 4,97 lei."}})))
    assert [t async for t in call.stream(0)] == [FILLER + " ", "Euro e 4,97 lei."]


async def test_no_filler_when_the_agent_already_spoke_or_calls_more_tools():
    call = _call(_Log(("llm.request", {}), ("llm.delta", {"content": "O clipă, caut. "}),
                      ("tool.call", {"name": "web_search", "arguments": "{}"}),
                      ("tool.call", {"name": "web_extract", "arguments": "{}"}),
                      ("llm.response", {"message": {"role": "assistant", "content": "Gata."}})))
    assert [t async for t in call.stream(0)] == ["O clipă, caut. "]


@pytest.mark.parametrize("raw", ["+40 721 000 000", "0721000000", "(0721) 000-000", "40721000000"])
def test_one_caller_is_one_number_however_it_is_written(raw):
    assert normalize_caller(raw) == "40721000000"


async def test_answering_the_agents_question_has_the_api_routes_safeguards(monkeypatch):
    from surogates.voice import sessions as vs
    seen = {}

    async def resolver(store, *, session_id, text, max_age_seconds=None):
        seen["max_age"] = max_age_seconds
        raise RuntimeError("db blip")

    monkeypatch.setattr(vs, "try_resolve_text_answer", resolver)
    log = _Log()
    log.get_session = _status("active")
    call = _call(log)
    call.redis.zadd = _noop
    await call.send("Marți.")
    assert seen["max_age"] == 30 * 60 - 60  # a question near its deadline is treated as expired
    assert [e.data["content"] for e in log.events if e.type == "user.message"] == ["Marți."]  # delivered anyway


def _status(status):
    async def get_session(session_id):
        return SimpleNamespace(status=status)
    return get_session


async def test_end_call_marks_the_call_as_ending_and_is_not_announced():
    call = _call(_Log(("llm.request", {}), ("tool.call", {"name": "end_call", "arguments": "{}"}),
                      ("llm.request", {}), ("llm.delta", {"content": "La revedere!"}),
                      ("llm.response", {"message": {"role": "assistant", "content": "La revedere!"}})))
    assert [t async for t in call.stream(0)] == ["La revedere!"]  # no "O clipă, verific." before a goodbye
    assert call.ending


async def test_the_filler_is_spoken_while_the_tool_still_runs():
    """The filler must leave the sentence splitter on its own: before, it waited in the buffer for the
    answer's first word, so the caller heard it only once the tool had returned."""
    from surogates.voice.text import SentenceSplitter

    call = _call(_Log(("llm.request", {}), ("tool.call", {"name": "web_search", "arguments": "{}"})))
    first = None
    async for piece in call.stream(0, turn_timeout=0.3):  # the tool never returns in this log
        first = piece
        break
    assert SentenceSplitter().push(first) == [FILLER]
