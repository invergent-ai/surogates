"""Iteration and final-summary helpers for AgentHarness."""

from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.harness.loop_messages import _latest_user_message_text
from surogates.harness.message_utils import coerce_message_content
from surogates.harness.reasoning import extract_reasoning, strip_think_blocks
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


class IterationSummaryMixin:
    async def _maybe_summarize_iteration(
        self,
        *,
        session_id: UUID,
        turn_id: str,
        iteration_index: int,
        reasoning_text: str,
        tool_calls: list[dict[str, Any]],
        started_at: str,
        tool_results: list[dict[str, Any]] | None = None,
    ) -> None:
        """Fire-and-forget per-iteration summarization.

        Spawns a background task that calls the summarizer and emits an
        ``ITERATION_SUMMARY`` event when it resolves. Tracked in
        ``_pending_iteration_summary_tasks`` so :meth:`_complete_session`
        can drain it before emitting ``TURN_SUMMARY``. No-op when the
        harness has no summarizer or the iteration produced nothing
        worth summarizing.
        """
        if self._turn_summarizer is None:
            return
        if not reasoning_text and not tool_calls:
            return

        # Snapshot only summaries that have already resolved for earlier
        # iterations of this turn. Later summaries may still be pending;
        # awaiting them here would defeat the fire-and-forget design and
        # could deadlock the loop.
        prior_summaries = [
            self._completed_iteration_summaries[idx]
            for idx in sorted(self._completed_iteration_summaries)
            if idx < iteration_index
        ]
        tool_call_ids = [
            str(tc.get("id") or "") for tc in tool_calls
        ]

        async def _run() -> None:
            summary = await self._turn_summarizer.summarize_iteration(
                iteration_id=f"{turn_id}:{iteration_index}",
                reasoning=reasoning_text,
                tool_calls=tool_calls,
                prior_iteration_summaries=prior_summaries,
                tool_results=tool_results,
            )
            if summary is None:
                return
            self._completed_iteration_summaries[iteration_index] = summary
            try:
                await self._store.emit_event(
                    session_id,
                    EventType.ITERATION_SUMMARY,
                    {
                        "turn_id": turn_id,
                        "iteration_index": iteration_index,
                        "summary": summary,
                        "tool_call_ids": tool_call_ids,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to emit ITERATION_SUMMARY for %s iter %d",
                    session_id, iteration_index, exc_info=True,
                )

        task = asyncio.create_task(
            _run(), name=f"iteration-summary-{turn_id}-{iteration_index}",
        )
        # Track in two places: the per-turn dict keyed by
        # iteration_index lets _drain_and_emit_turn_summary await the
        # right tasks before generating the recap; _background_tasks
        # lets wake()'s finally drain anything still in flight when the
        # turn ends abnormally (cancellation, crash).
        self._pending_iteration_summary_tasks[iteration_index] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(
            lambda _t: self._pending_iteration_summary_tasks.pop(
                iteration_index, None,
            ),
        )
