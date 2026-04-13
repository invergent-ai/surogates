"""Training data export from the session event log.

Extracts successful conversation trajectories from completed sessions
and writes them as OpenAI-compatible JSONL files to the tenant's
storage bucket.  The platform's responsibility ends at the JSONL file;
training and fine-tuning happen externally.

Sources of training data:

1. **Expert delegations** -- successful ``consult_expert`` invocations
   (``expert.delegation`` followed by ``expert.result``, with no
   subsequent ``expert.override`` in the session).
2. **Base LLM trajectories** -- recurring task patterns that an admin
   has tagged as distillation targets for a specific expert.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.session.events import EventType

logger = logging.getLogger(__name__)


class TrainingExample:
    """A single training example in OpenAI fine-tuning format.

    Attributes
    ----------
    messages:
        A list of message dicts with ``role``, ``content``, and
        optionally ``tool_calls`` / ``tool_call_id``.
    session_id:
        The source session UUID.
    expert_name:
        The expert this example is for.
    created_at:
        When the source events were recorded.
    """

    __slots__ = ("messages", "session_id", "expert_name", "created_at")

    def __init__(
        self,
        messages: list[dict[str, Any]],
        session_id: UUID,
        expert_name: str,
        created_at: datetime | None = None,
    ) -> None:
        self.messages = messages
        self.session_id = session_id
        self.expert_name = expert_name
        self.created_at = created_at or datetime.now(timezone.utc)

    def to_jsonl_dict(self) -> dict[str, Any]:
        """Return the OpenAI fine-tuning format dict."""
        return {"messages": self.messages}

    def to_jsonl_line(self) -> str:
        """Return a single JSONL line."""
        return json.dumps(self.to_jsonl_dict(), ensure_ascii=False)


class TrainingDataCollector:
    """Scans completed sessions and extracts training pairs for export.

    Parameters
    ----------
    session_store:
        The :class:`~surogates.session.store.SessionStore` for reading
        session events.
    storage:
        The :class:`~surogates.storage.tenant.TenantStorage` for
        writing JSONL files.
    """

    def __init__(
        self,
        session_store: Any,
        storage: Any | None = None,
    ) -> None:
        self._session_store = session_store
        self._storage = storage

    async def collect_for_expert(
        self,
        expert_name: str,
        org_id: UUID,
        since: datetime | None = None,
    ) -> list[TrainingExample]:
        """Extract training examples for *expert_name* from completed sessions.

        Scans sessions belonging to *org_id* for successful expert
        delegations to the named expert.  Returns a list of
        :class:`TrainingExample` instances.

        Parameters
        ----------
        expert_name:
            The expert to collect training data for.
        org_id:
            The organisation UUID (scopes the query).
        since:
            Only include sessions completed after this timestamp.
            Defaults to all time.
        """
        examples: list[TrainingExample] = []

        # Get completed sessions for this org.
        sessions = await self._session_store.list_sessions(
            org_id=org_id,
            status="completed",
            since=since,
        )

        for session in sessions:
            session_examples = await self._extract_from_session(
                session_id=session.id,
                expert_name=expert_name,
            )
            examples.extend(session_examples)

        logger.info(
            "Collected %d training examples for expert '%s' from %d sessions",
            len(examples),
            expert_name,
            len(sessions),
        )
        return examples

    async def _extract_from_session(
        self,
        session_id: UUID,
        expert_name: str,
    ) -> list[TrainingExample]:
        """Extract training examples from a single session's events."""
        events = await self._session_store.get_events(session_id)

        examples: list[TrainingExample] = []
        overridden_experts: set[str] = set()

        # First pass: find which experts were overridden in this session.
        for event in events:
            if event.type == EventType.EXPERT_OVERRIDE.value:
                overridden_experts.add(event.data.get("expert", ""))

        if expert_name in overridden_experts:
            return []

        # Second pass: extract delegation → tool calls → result sequences.
        i = 0
        while i < len(events):
            event = events[i]
            if (
                event.type == EventType.EXPERT_DELEGATION.value
                and event.data.get("expert") == expert_name
            ):
                # Found a delegation to our expert.  Walk forward to
                # collect the expert's tool calls and final result.
                example = self._collect_trajectory(events, i, expert_name, session_id)
                if example is not None:
                    examples.append(example)
            i += 1

        return examples

    def _collect_trajectory(
        self,
        events: list[Any],
        start_idx: int,
        expert_name: str,
        session_id: UUID,
    ) -> TrainingExample | None:
        """Collect a delegation trajectory starting at *start_idx*.

        Returns a :class:`TrainingExample` if the trajectory ends with
        a successful result, otherwise ``None``.
        """
        delegation_event = events[start_idx]
        task = delegation_event.data.get("task", "")
        if not task:
            return None

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": task},
        ]

        # Walk forward to find tool calls and the final result.
        for i in range(start_idx + 1, len(events)):
            event = events[i]

            if event.type == EventType.TOOL_CALL.value:
                tool_call_data = event.data
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": tool_call_data.get("tool_call_id", f"tc_{i}"),
                        "type": "function",
                        "function": {
                            "name": tool_call_data.get("name", ""),
                            "arguments": json.dumps(
                                tool_call_data.get("arguments", {}),
                                ensure_ascii=False,
                            ),
                        },
                    }],
                })

            elif event.type == EventType.TOOL_RESULT.value:
                result_data = event.data
                messages.append({
                    "role": "tool",
                    "tool_call_id": result_data.get("tool_call_id", f"tc_{i}"),
                    "content": str(result_data.get("result", ""))[:10_000],
                })

            elif event.type == EventType.EXPERT_RESULT.value:
                if event.data.get("expert") == expert_name and event.data.get("success"):
                    # Found the successful result -- complete the example.
                    return TrainingExample(
                        messages=messages,
                        session_id=session_id,
                        expert_name=expert_name,
                        created_at=event.created_at if hasattr(event, "created_at") else None,
                    )
                return None

            elif event.type == EventType.EXPERT_FAILURE.value:
                if event.data.get("expert") == expert_name:
                    return None

            elif event.type in (
                EventType.EXPERT_DELEGATION.value,
                EventType.SESSION_COMPLETE.value,
                EventType.SESSION_FAIL.value,
            ):
                # Hit a new delegation or session end -- trajectory broken.
                return None

        return None

    async def export_jsonl(
        self,
        expert_name: str,
        examples: list[TrainingExample],
        org_id: UUID,
    ) -> str:
        """Write training examples to the tenant bucket as JSONL.

        Parameters
        ----------
        expert_name:
            The expert name (used in the storage key).
        examples:
            The training examples to export.
        org_id:
            The organisation UUID.

        Returns
        -------
        str
            The storage key where the JSONL file was written.
        """
        if not examples:
            return ""

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        key = f"shared/skills/{expert_name}/training/dataset_{timestamp}.jsonl"

        lines = [ex.to_jsonl_line() for ex in examples]
        content = "\n".join(lines) + "\n"

        if self._storage is not None:
            bucket = f"tenant-{org_id}"
            await self._storage.write(bucket, key, content.encode("utf-8"))
            logger.info(
                "Exported %d training examples for expert '%s' to %s/%s",
                len(examples),
                expert_name,
                bucket,
                key,
            )
        else:
            logger.warning(
                "No storage backend available; %d examples for '%s' not persisted",
                len(examples),
                expert_name,
            )

        return key
