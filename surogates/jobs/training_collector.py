"""Training data export from the session event log.

Extracts successful conversation trajectories from completed sessions
and writes them as OpenAI-compatible JSONL files to the tenant's
storage bucket.  The platform's responsibility ends at the JSONL file;
training and fine-tuning happen externally.

Sources of training data:

1. **Expert delegations** -- successful ``consult_expert`` invocations
   (``expert.delegation`` followed by ``expert.result``, with no
   subsequent ``expert.override`` in the session).  Use this to
   *improve* an existing expert.
2. **Skill invocations** -- ``skill.invoked`` followed by the base
   LLM's actual answer.  Use this to *bootstrap* a new expert from a
   prompt-based skill that users are already invoking with
   ``/<skill> args``.  The skill is the class label.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.session.events import EventType

logger = logging.getLogger(__name__)


def _strip_skill_prefix(raw_message: str, skill_name: str) -> str:
    """Remove the leading ``/<skill_name>`` from *raw_message*.

    Returns the user's natural-language ask with the slash-command
    stripped.  When the message does not start with the expected
    prefix, returns it unchanged (callers may still get usable input
    if the user pasted the skill elsewhere).  Empty result falls back
    to the full raw message so the caller can still decide whether to
    keep the trajectory.
    """
    prefix = f"/{skill_name}"
    if raw_message.startswith(prefix):
        rest = raw_message[len(prefix):].lstrip()
        return rest or raw_message
    return raw_message


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

    async def collect_for_skill(
        self,
        skill_name: str,
        org_id: UUID,
        *,
        since: datetime | None = None,
        exclude_tainted: bool = True,
    ) -> list[TrainingExample]:
        """Bootstrap-path: extract trajectories from ``skill.invoked`` events.

        Graduates a prompt-based skill into a fine-tuned SLM (an
        "expert") by walking every ``skill.invoked`` in *org_id* for
        *skill_name*, collecting the base LLM's reply span (assistant
        turns, tool calls, tool results) up to the next trajectory
        boundary (next user message, next skill invocation, or session
        terminal event).

        Each invocation is its own class label — the user invoked
        ``/<skill> args``, so the subsequent answer is by definition
        "what the skill should do".  Use :meth:`collect_for_expert`
        instead once the expert is active and the base LLM is
        delegating to it.

        Parameters
        ----------
        skill_name:
            The skill to bootstrap.  The future expert will live at
            ``shared/skills/<skill_name>/`` so the training data lands
            in the same place (``training/``).
        org_id:
            The organisation UUID (scopes the query).
        since:
            Only include invocations recorded after this timestamp.
            Defaults to all time.
        exclude_tainted:
            When True (default), sessions with ``policy.denied``,
            ``harness.crash``, ``saga.compensate``, or
            ``expert.override`` events are skipped entirely.
        """
        invocations = await self._session_store.find_skill_invocations(
            org_id, skill_name, since=since,
        )
        if not invocations:
            logger.info(
                "No skill.invoked events for skill '%s' in org %s",
                skill_name, org_id,
            )
            return []

        # Group invocations by session so each session's events are
        # loaded (and tainted-check runs) at most once.
        by_session: dict[UUID, list[Any]] = {}
        for inv in invocations:
            by_session.setdefault(inv.session_id, []).append(inv)

        examples: list[TrainingExample] = []
        skipped_tainted = 0

        for session_id, session_invocations in by_session.items():
            if exclude_tainted:
                if await self._session_store.session_has_taint(session_id):
                    skipped_tainted += 1
                    continue

            events = await self._session_store.get_events(session_id)
            events_by_id = {e.id: i for i, e in enumerate(events)}

            for inv in session_invocations:
                start_idx = events_by_id.get(inv.id)
                if start_idx is None:
                    continue
                example = self._collect_skill_trajectory(
                    events, start_idx, skill_name, session_id, inv,
                )
                if example is not None:
                    examples.append(example)

        logger.info(
            "Collected %d training examples for skill '%s' "
            "(%d invocations, %d tainted sessions skipped)",
            len(examples), skill_name, len(invocations), skipped_tainted,
        )
        return examples

    def _collect_skill_trajectory(
        self,
        events: list[Any],
        start_idx: int,
        skill_name: str,
        session_id: UUID,
        skill_event: Any,
    ) -> TrainingExample | None:
        """Walk events from a ``skill.invoked`` through its trajectory.

        Returns a :class:`TrainingExample` when a complete trajectory
        is found (at least one assistant response with content).  The
        trajectory ends at the first of: next ``user.message``, next
        ``skill.invoked``, ``session.complete`` or ``session.fail``.
        """
        raw_message = skill_event.data.get("raw_message", "")
        user_text = _strip_skill_prefix(raw_message, skill_name)
        if not user_text:
            return None

        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_text},
        ]
        has_final_assistant_content = False

        for i in range(start_idx + 1, len(events)):
            event = events[i]
            etype = event.type

            if etype in (
                EventType.USER_MESSAGE.value,
                EventType.SKILL_INVOKED.value,
                EventType.SESSION_COMPLETE.value,
                EventType.SESSION_FAIL.value,
            ):
                break  # trajectory boundary

            if etype == EventType.LLM_RESPONSE.value:
                msg = event.data.get("message")
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") != "assistant":
                    continue
                # Copy the assistant message as-is; its ``tool_calls``
                # (if any) are already in the OpenAI-compatible shape.
                messages.append(dict(msg))
                if msg.get("content"):
                    has_final_assistant_content = True

            elif etype == EventType.TOOL_RESULT.value:
                messages.append({
                    "role": "tool",
                    "tool_call_id": event.data.get("tool_call_id", ""),
                    "content": str(event.data.get("content", ""))[:10_000],
                })
            # ``tool.call`` is intentionally skipped — the same tool
            # call is already present inline on the preceding
            # ``llm.response`` message's ``tool_calls`` field.

        if not has_final_assistant_content:
            return None

        return TrainingExample(
            messages=messages,
            session_id=session_id,
            expert_name=skill_name,
            created_at=getattr(skill_event, "created_at", None),
        )

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
