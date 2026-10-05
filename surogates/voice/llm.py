"""The call's agent session in LiveKit's LLM slot.

The session keeps the conversation server-side, so each turn sends only the caller's latest
words; LiveKit's own chat context is display only. Tools run in the harness, never here. A turn
cancelled by a barge-in stops the agent's turn too.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, APIConnectOptions, NotGivenOr, llm

from surogates.voice.sessions import CallSession


def latest_user_text(chat_ctx: llm.ChatContext) -> str:
    for item in reversed(chat_ctx.items):
        if getattr(item, "role", None) == "user":
            return (item.text_content or "").strip()
    return ""


class SurogatesLLM(llm.LLM):
    def __init__(self, call: CallSession) -> None:
        super().__init__()
        self._call = call

    @property
    def model(self) -> str:
        return "surogates-agent"

    @property
    def provider(self) -> str:
        return "surogates"

    def chat(self, *, chat_ctx: llm.ChatContext, tools: list[llm.Tool] | None = None,
             conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
             parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN, tool_choice: NotGivenOr[llm.ToolChoice] = NOT_GIVEN,
             extra_kwargs: NotGivenOr[dict] = NOT_GIVEN) -> SurogatesStream:
        # never retried: a retry would post the caller's words twice
        return SurogatesStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=replace(conn_options, max_retry=0))


class SurogatesStream(llm.LLMStream):
    async def _run(self) -> None:
        text = latest_user_text(self._chat_ctx)
        if not text:
            return
        call: CallSession = self._llm._call
        after = await call.send(text)
        finished = False
        try:
            async for piece in call.stream(after):
                self._event_ch.send_nowait(llm.ChatChunk(id=str(after),
                                                         delta=llm.ChoiceDelta(role="assistant", content=piece)))
            finished = True
        finally:
            if not finished:
                await asyncio.shield(call.interrupt())
