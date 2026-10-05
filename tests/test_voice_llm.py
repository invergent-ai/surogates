"""LiveKit's LLM slot speaks to the call's session: one utterance in, the turn's text out, a barge-in stops it."""
import asyncio

from livekit.agents import llm

from surogates.voice.llm import SurogatesLLM


class _Call:
    def __init__(self, pieces, hang=False):
        self.pieces, self.hang = pieces, hang
        self.sent, self.interrupted = [], False

    async def send(self, text):
        self.sent.append(text)
        return 7

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
    for role, text in turns:
        ctx.add_message(role=role, content=text)
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
