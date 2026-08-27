"""Advisor helpers for AgentHarness.

The advisor is a stronger model (the platform's Pro tier) the executor
consults mid-task through the ``advisor`` tool. Timing is **model
driven**: the executor calls the tool when it is about to commit to an
approach, when it is stuck, or before declaring the task done — the
harness never decides on its behalf.

An earlier design classified every user turn with a cheap LLM call and
blocked the first request on the verdict. Measured over three months of
production traffic, that gate ran on 1,983 interactive turns, fired the
advisor on 113, and delivered guidance early enough to shape the first
iteration 14 times — while adding seconds of latency to every turn in
between. A rule-based gate is also the opposite of the documented
pattern, where the executor calls the advisor as a tool.

Two invariants remain:

* Guidance returns to the executor as a **tool result**, so it can never
  split an assistant ``tool_calls`` message from its tool results.
* Durable ``ADVISOR_RESULT`` events replay into later wakes tagged
  (``_advisor`` + a recognizable prefix) so a subsequent turn never
  mistakes past guidance for the human's message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from surogates.harness.loop_vision import _collapse_text_parts, _extract_response_text
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

# The advisor sees the task statement verbatim up to this many chars —
# beyond it we truncate rather than ship (for example) a base64 image
# repr to a pro-tier model.
_ADVISOR_TASK_CHARS = 4000


class AdvisorMixin:
    def _advisor_available(self) -> bool:
        return self._advisor_client is not None and bool(self._advisor_model)

    async def consult_advisor(
        self,
        *,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        category: str,
        task: str,
    ) -> str | None:
        """Run one advisor consult and return its guidance.

        Called from the ``advisor`` tool handler, so the executor decides
        the timing. Returns ``None`` when the advisor is not configured
        or the per-turn budget is spent; the handler turns that into a
        result the executor can act on.

        The budget counts *calls*, not distinct categories. The old
        classifier path deduped by category to stop itself re-firing;
        with the executor choosing, the documented pattern is two to
        three calls per task (before committing to an approach, and
        before declaring done), which a category dedup would block.
        """
        if not self._advisor_available():
            return None
        if self._advisor_calls_this_turn >= self._advisor_max_calls_per_turn:
            return None

        self._advisor_calls_this_turn += 1
        await self._emit_advisor_request(session, category)

        try:
            assert self._advisor_client is not None
            response = await self._advisor_client.chat.completions.create(
                model=self._advisor_model,
                messages=self._build_advisor_messages(
                    messages=messages,
                    system_prompt=system_prompt,
                    category=category,
                    task=task,
                ),
                temperature=0.2,
                max_tokens=self._advisor_max_tokens,
            )
            content = _extract_response_text(response)
            if not content:
                raise RuntimeError("advisor returned empty guidance")
            finish_reason = None
            choices = getattr(response, "choices", None) or []
            if choices:
                finish_reason = getattr(choices[0], "finish_reason", None)
            if finish_reason == "length":
                logger.warning(
                    "Session %s: advisor guidance for %s truncated at "
                    "max_tokens=%d; injecting the partial guidance",
                    session.id, category, self._advisor_max_tokens,
                )
            usage = getattr(response, "usage", None)
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_RESULT,
                {
                    "model": self._advisor_model,
                    "category": category,
                    "content": content,
                    "truncated": finish_reason == "length",
                    "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                },
            )
            return content
        except asyncio.CancelledError:
            # Drain-timeout cancellation. Record a terminal event so the
            # consult is visible in the timeline — the upstream call
            # likely completed (and billed) anyway.
            try:
                await asyncio.shield(self._store.emit_event(
                    session.id,
                    EventType.ADVISOR_FAILURE,
                    {
                        "model": self._advisor_model,
                        "category": category,
                        "error": "cancelled during background drain",
                    },
                ))
            except Exception:
                pass
            raise
        except Exception as exc:
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_FAILURE,
                {
                    "model": self._advisor_model,
                    "category": category,
                    "error": str(exc),
                },
            )
            logger.warning(
                "Session %s: advisor call failed for %s",
                session.id,
                category,
                exc_info=True,
            )
            return None

    def _build_advisor_messages(
        self,
        *,
        messages: list[dict],
        system_prompt: str,
        category: str,
        task: str,
    ) -> list[dict[str, str]]:
        transcript = self._build_advisor_context(messages)
        # "Carry the work" wording, A/B-tested against a "do not solve
        # outright" instruction: method-only advice failed to rescue the
        # executor on arithmetic/bookkeeping-heavy tasks, while guidance
        # carrying concrete intermediates and a proposed answer flipped
        # them to passes without degrading tasks the executor already
        # solved on its own.
        prompt = (
            "You are a strategic advisor for an agent harness. The executor "
            "model is cheaper and will continue the task after reading your "
            "guidance. Give concise, high-leverage advice under "
            f"{self._advisor_max_tokens} tokens.\n"
            "Work the problem as far as you can yourself: state the key "
            "intermediate results, the pitfalls, and — when you are "
            "confident — your own answer, plus how the executor should "
            "verify it. Method-only advice does not rescue a weaker model "
            "from error-prone arithmetic or bookkeeping; concrete numbers, "
            "enumerations, and near-code do. If the task needs tools you "
            "don't have, say exactly what to run and what output to expect.\n\n"
            f"Hard-task category: {category}\n\n"
            f"Current task or tool intent:\n{task}\n\n"
            f"Recent transcript:\n{transcript}"
        )
        if system_prompt:
            prompt = f"Executor system prompt:\n{system_prompt[-8000:]}\n\n{prompt}"
        return [{"role": "user", "content": prompt}]

    async def _emit_advisor_request(
        self,
        session: Session,
        category: str,
    ) -> None:
        try:
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_REQUEST,
                {
                    "model": self._advisor_model,
                    "category": category,
                },
            )
        except Exception:
            logger.debug(
                "Session %s: failed to emit advisor request",
                session.id,
                exc_info=True,
            )

    @staticmethod
    def _message_text(message: dict) -> str:
        """The message's text content, multimodal-safe and length-capped.

        ``str()`` on block-list content would embed base64 image payloads
        (a Python repr) into the advisor prompt at pro-tier pricing.
        """
        content = message.get("content") or ""
        if isinstance(content, list):
            content = _collapse_text_parts([
                part
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ])
        if not isinstance(content, str):
            content = str(content)
        return content[:_ADVISOR_TASK_CHARS]

    @staticmethod
    def _build_advisor_context(messages: list[dict]) -> str:
        fragments: list[str] = []
        for msg in messages[-12:]:
            role = msg.get("role", "unknown")
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = _collapse_text_parts([
                    part
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ])
            if not isinstance(content, str) or not content.strip():
                continue
            fragments.append(f"{role}: {content}")
        return "\n\n".join(fragments)[-16_000:]

    @staticmethod
    def _format_advisor_context(
        *,
        category: str,
        content: str,
    ) -> str:
        return (
            f"[Advisor guidance: {category}]\n"
            f"{content}\n\n"
            "Use this as strategic guidance. Verify with tools where "
            "appropriate and adapt if direct evidence contradicts it."
        )
