"""Hidden advisor helpers for AgentHarness."""

from __future__ import annotations

import logging
from typing import Any, Literal

from surogates.harness.expert_routing import classify_hard_task_async
from surogates.harness.loop_vision import _collapse_text_parts, _extract_response_text
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


class AdvisorMixin:
    async def _maybe_consult_required_advisor(
        self,
        session: Session,
        messages: list[dict],
        all_events: list[Event],
        system_prompt: str = "",
        consulted_categories: set[str] | None = None,
    ) -> bool:
        """Ask the hidden advisor for guidance before hard executor work."""
        consulted_categories = consulted_categories if consulted_categories is not None else set()
        last_user = self._last_user_message(messages)
        if last_user is None:
            return False
        if not self._advisor_available():
            return False

        user_content = str(last_user.get("content") or "")
        classification = await classify_hard_task_async(
            messages,
            tenant=self._tenant,
        )
        if not classification.required or classification.category is None:
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
            reason="early",
            consulted_categories=consulted_categories,
        )
        if not result:
            return False

        messages.append({
            "role": "user",
            "content": self._format_advisor_context(
                category=classification.category,
                content=result,
            ),
        })
        return True

    def _advisor_available(self) -> bool:
        return self._advisor_client is not None and bool(self._advisor_model)

    async def _consult_advisor_for_category(
        self,
        *,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        category: str,
        task: str,
        reason: Literal["early", "final_check"],
        consulted_categories: set[str],
    ) -> str | None:
        if not self._advisor_available():
            return None
        if len(consulted_categories) >= self._advisor_max_calls_per_turn:
            return None
        if category in consulted_categories:
            return None

        consulted_categories.add(category)
        await self._emit_advisor_request(session, reason, category)

        try:
            assert self._advisor_client is not None
            response = await self._advisor_client.chat.completions.create(
                model=self._advisor_model,
                messages=self._build_advisor_messages(
                    messages=messages,
                    system_prompt=system_prompt,
                    category=category,
                    task=task,
                    reason=reason,
                ),
                temperature=0.2,
                max_tokens=self._advisor_max_tokens,
            )
            content = _extract_response_text(response)
            if not content:
                raise RuntimeError("advisor returned empty guidance")
            usage = getattr(response, "usage", None)
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_RESULT,
                {
                    "model": self._advisor_model,
                    "reason": reason,
                    "category": category,
                    "content": content,
                    "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                },
            )
            return content
        except Exception as exc:
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_FAILURE,
                {
                    "model": self._advisor_model,
                    "reason": reason,
                    "category": category,
                    "error": str(exc),
                },
            )
            logger.debug(
                "Session %s: advisor call failed for %s/%s",
                session.id,
                reason,
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
        reason: str,
    ) -> list[dict[str, str]]:
        transcript = self._build_advisor_context(messages)
        prompt = (
            "You are a strategic advisor for an agent harness. The executor "
            "model is cheaper and will continue the task after reading your "
            "guidance. Give concise, high-leverage advice under "
            f"{self._advisor_max_tokens} tokens. Do not solve by writing the "
            "entire final answer unless that is the only useful guidance.\n\n"
            f"Advisor reason: {reason}\n"
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
        reason: str,
        category: str,
    ) -> None:
        try:
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_REQUEST,
                {
                    "model": self._advisor_model,
                    "reason": reason,
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
    def _last_user_message(messages: list[dict]) -> dict | None:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg
        return None

    @staticmethod
    def _advisor_categories_after_latest_user(events: list[Event]) -> set[str]:
        latest_user_event_id = 0
        for event in events:
            if event.type == EventType.USER_MESSAGE.value and event.id is not None:
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
