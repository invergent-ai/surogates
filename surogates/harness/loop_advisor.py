"""Hidden advisor helpers for AgentHarness.

The advisor is a stronger model (the platform's Pro tier) consulted
before hard executor work so its strategy can shape the cheaper
executor's plan. Three invariants keep it correct:

* Guidance is **buffered**, never appended to the live ``messages`` list
  from the background task — the loop flushes the buffer at iteration
  boundaries, so guidance can never split an assistant ``tool_calls``
  message from its tool results.
* Consults happen only on an **LLM classifier** verdict. The regex
  fallback both over-fires on English keywords and never fires on
  non-English text, so it is not a good enough signal to spend a
  pro-tier call on.
* Injected guidance is tagged (``_advisor`` + a recognizable prefix) so
  later wakes never mistake it for the human's message and ask the
  advisor to advise on its own output.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from surogates.harness.auxiliary_client import AuxiliaryLLM
from surogates.harness.expert_routing import (
    classify_hard_task_async,
    is_advisor_guidance_message,
)
from surogates.harness.loop_vision import _collapse_text_parts, _extract_response_text
from surogates.session.events import EventType

logger = logging.getLogger(__name__)

# The advisor sees the task statement verbatim up to this many chars —
# beyond it we truncate rather than ship (for example) a base64 image
# repr to a pro-tier model.
_ADVISOR_TASK_CHARS = 4000


class AdvisorMixin:
    async def _maybe_consult_required_advisor(
        self,
        session: Session,
        messages: list[dict],
        all_events: list[Event],
        system_prompt: str = "",
        consulted_categories: set[str] | None = None,
    ) -> bool:
        """Ask the hidden advisor for guidance before hard executor work.

        Returns True when guidance was produced and buffered. The caller
        (the iteration loop) flushes ``self._pending_advisor_messages``
        at the next safe boundary.
        """
        consulted_categories = consulted_categories if consulted_categories is not None else set()
        last_user = self._last_user_message(messages)
        if last_user is None:
            return False
        if not self._advisor_available():
            return False

        user_content = self._message_text(last_user)
        # Prefer the session's summary client: it is resolved from the
        # agent's runtime config, so it points at a billed per-agent
        # proxy route and runs on the cheap summary model. The
        # settings-level fallback inside the classifier depends on
        # ``llm.base_url`` serving chat completions, which it does not
        # when that URL is the proxy root.
        classifier_aux: AuxiliaryLLM | None = None
        if self._summary_client is not None and self._summary_model:
            classifier_aux = AuxiliaryLLM(
                client=self._summary_client, model=self._summary_model,
            )
        classification = await classify_hard_task_async(
            messages,
            tenant=self._tenant,
            aux=classifier_aux,
        )
        if not classification.required or classification.category is None:
            return False
        if classification.source != "llm":
            # Regex verdicts are too noisy to spend a pro-tier consult
            # on — and their false negatives (non-English) mean the
            # advisor would fire on keyword luck, not task difficulty.
            logger.debug(
                "Session %s: skipping advisor consult on non-LLM "
                "classifier verdict (%s/%s)",
                session.id, classification.source, classification.category,
            )
            return False

        if (
            classification.category in consulted_categories
            or classification.category in self._advisor_categories_after_latest_user(
                all_events,
            )
        ):
            return False

        result = await self._consult_advisor_for_category(
            session=session,
            messages=messages,
            system_prompt=system_prompt,
            category=classification.category,
            task=user_content,
            consulted_categories=consulted_categories,
        )
        if not result:
            return False

        # Buffer — the loop flushes at an iteration boundary. Appending
        # here (from a background task) could land between an assistant
        # tool_calls message and its tool results, an ordering strict
        # providers reject.
        self._pending_advisor_messages.append({
            "role": "user",
            "_advisor": True,
            "content": self._format_advisor_context(
                category=classification.category,
                content=result,
            ),
        })
        return True

    def _advisor_available(self) -> bool:
        return self._advisor_client is not None and bool(self._advisor_model)

    def _flush_pending_advisor_messages(self, messages: list[dict]) -> bool:
        """Move buffered guidance into ``messages`` at a safe boundary."""
        if not self._pending_advisor_messages:
            return False
        messages.extend(self._pending_advisor_messages)
        self._pending_advisor_messages = []
        return True

    async def _consult_advisor_for_category(
        self,
        *,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        category: str,
        task: str,
        consulted_categories: set[str],
    ) -> str | None:
        if not self._advisor_available():
            return None
        if len(consulted_categories) >= self._advisor_max_calls_per_turn:
            return None
        if category in consulted_categories:
            return None

        consulted_categories.add(category)
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
            # cross-wake dedup sees this category as attempted — the
            # upstream call likely completed (and billed) anyway.
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
    def _last_user_message(messages: list[dict]) -> dict | None:
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            # Advisor guidance rides the user role for provider
            # compatibility but is harness output — treating it as "the
            # user's message" would make the advisor advise on itself.
            if is_advisor_guidance_message(msg):
                continue
            return msg
        return None

    @staticmethod
    def _advisor_categories_after_latest_user(events: list[Event]) -> set[str]:
        latest_user_event_id = 0
        for event in events:
            if event.type != EventType.USER_MESSAGE.value or event.id is None:
                continue
            # Synthetic nudges (mission kickoffs, deep-research nudges)
            # are USER_MESSAGE events too; they are not a new human turn
            # and must not reset the advisor dedup window — the replay
            # builder makes the same distinction.
            if (event.data or {}).get("synthetic"):
                continue
            latest_user_event_id = max(latest_user_event_id, event.id)
        categories: set[str] = set()
        for event in events:
            if event.id is None or event.id <= latest_user_event_id:
                continue
            if event.type in {
                EventType.ADVISOR_RESULT.value,
                EventType.ADVISOR_FAILURE.value,
            }:
                category = event.data.get("category")
                if category:
                    categories.add(str(category))
        return categories

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
