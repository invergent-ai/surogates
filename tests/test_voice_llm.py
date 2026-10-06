"""LiveKit's LLM slot speaks to the call's session: one utterance in, the turn's text out, a barge-in stops it."""
import asyncio

from livekit.agents import llm

from surogates.voice.llm import SurogatesLLM


class _Call:
    def __init__(self, pieces, hang=False):
        self.pieces, self.hang = pieces, hang
        self.sent, self.interrupted, self.log = [], False, []

    async def send(self, text):
        self.sent.append(text)
        self.log.append(("send", text))
        return 7

    async def record_heard(self, heard):
        self.log.append(("heard", heard))

    async def stream(self, after):
        assert after == 7
        for p in self.pieces:
            yield p
        if self.hang:
            await asyncio.Event().wait()

    async def interrupt(self):
        self.interrupted = True


def _ctx(*turns):
    ctx = llm.ChatContext.empty()
    for role, text, *cut in turns:
        ctx.add_message(role=role, content=text, interrupted=bool(cut))
    return ctx


async def test_sends_only_the_latest_utterance_and_streams_the_answer():
    call = _Call(["Euro ", "e 4,97 lei."])
    ctx = _ctx(("assistant", "Bună ziua!"), ("user", "Salut"), ("assistant", "Salut!"), ("user", "Cât e euro?"))
    async with SurogatesLLM(call).chat(chat_ctx=ctx) as stream:
        text = "".join([c.delta.content async for c in stream if c.delta and c.delta.content])
    assert call.sent == ["Cât e euro?"] and text == "Euro e 4,97 lei." and not call.interrupted


async def test_closing_mid_answer_stops_the_agents_turn():
    call = _Call(["Prima știre "], hang=True)
    stream = SurogatesLLM(call).chat(chat_ctx=_ctx(("user", "Știrile?")))
    async for _ in stream:
        break
    await stream.aclose()
    assert call.interrupted


async def test_what_the_caller_heard_is_recorded_before_their_next_words():
    """LiveKit truncates an interrupted reply to what was played; the session learns it before the next turn."""
    call = _Call(["În 1659."])
    ctx = _ctx(("user", "Povestește-mi istoria Bucureștiului"), ("assistant", "Bucureștiul este menționat", "cut"),
               ("user", "Stop, spune-mi doar anul."))
    model = SurogatesLLM(call)
    async with model.chat(chat_ctx=ctx) as stream:
        [c async for c in stream]
    assert call.log == [("heard", "Bucureștiul este menționat"), ("send", "Stop, spune-mi doar anul.")]
    async with model.chat(chat_ctx=ctx) as stream:  # the same cut reply is reported once
        [c async for c in stream]
    assert [k for k, _ in call.log].count("heard") == 1


async def test_a_failed_turn_is_an_error_the_worker_answers_with_an_apology():
    """A crashed harness turn must reach LiveKit as an error (the worker then says SORRY_TURN), not as an empty
    answer the caller hears as silence."""
    import pytest
    from livekit.agents import APIError
    from surogates.voice.sessions import TurnFailed

    class _Failing(_Call):
        async def stream(self, after):
            raise TurnFailed("crash_loop_detected")
            yield  # an async generator

    errors = []
    llm_ = SurogatesLLM(_Failing([]))
    llm_.on("error", errors.append)
    with pytest.raises(APIError):
        async with llm_.chat(chat_ctx=_ctx(("user", "Alo?"))) as stream:
            async for _ in stream:
                pass
    assert errors and isinstance(errors[0].error, APIError) and not errors[0].recoverable
