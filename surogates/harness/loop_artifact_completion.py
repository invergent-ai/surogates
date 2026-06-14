"""Artifact promotion, progress, summary, and completion helpers for AgentHarness."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.harness.loop_artifacts import (
    _FENCE_RE,
    _PROMOTABLE_FENCES,
    _coerce_modified_to_datetime,
    _coerce_tool_args,
    _derive_artifact_name,
)
from surogates.harness.loop_constants import _BACKGROUND_DRAIN_TIMEOUT_SECONDS
from surogates.harness.loop_messages import (
    _as_aware_utc,
    _last_assistant_message_excerpt,
    _latest_user_message_text,
    _seconds_since,
    _should_notify_parent_on_completion,
)
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


class ArtifactCompletionMixin:
    async def _promote_fenced_artifacts(
        self,
        session: Session,
        assistant_content: str,
        messages: list[dict],
    ) -> None:
        """Auto-create an artifact when the LLM emits a render-worthy
        fenced block instead of calling ``create_artifact``.

        Some smaller models (``gpt-5.4-mini`` observed) prefer a
        one-token ` ```svg ` fence over a multi-token tool call with an
        escaped SVG payload, even when the system prompt explicitly
        forbids it.  Rather than leave the user staring at raw source,
        we parse the final assistant content for known render-capable
        fences and promote the first one into an artifact via the API.

        Only fires when:
        - an API client is wired (``self._api_client``),
        - the content contains at least one promotable fence (svg/html),
        - the fence body parses as non-empty.

        At most ONE artifact is created per response, matching the
        guidance's one-artifact-per-response rule.  Failures are logged
        but swallowed — a failed auto-promotion must not derail the
        turn.
        """
        if self._api_client is None or not assistant_content:
            return

        match = _FENCE_RE.search(assistant_content)
        while match is not None:
            lang = match.group(1).lower()
            mapping = _PROMOTABLE_FENCES.get(lang)
            if mapping is None:
                match = _FENCE_RE.search(assistant_content, match.end())
                continue
            body = match.group(2).strip()
            if not body:
                match = _FENCE_RE.search(assistant_content, match.end())
                continue
            kind, spec_key = mapping
            name = _derive_artifact_name(kind, messages)
            try:
                await self._api_client.create_artifact(
                    name=name, kind=kind, spec={spec_key: body},
                )
                logger.info(
                    "Session %s: promoted ```%s fence to %s artifact",
                    session.id, lang, kind,
                )
            except Exception:
                logger.warning(
                    "Session %s: failed to auto-promote ```%s fence",
                    session.id, lang, exc_info=True,
                )
            return  # one artifact per response
    async def _end_turn(
        self,
        session: Session,
        lease: SessionLease,
        *,
        through_event_id: int,
    ) -> None:
        """End the current turn of a primary session.

        Advances the harness cursor to ``through_event_id`` so a future wake()
        replays from the right point, and returns.  The session stays in its
        current status (typically 'active') so the user can send a follow-up.
        The sandbox pod, memory manager, and cost tracker are deliberately
        left alive — they belong to the session, not the turn.  The lease is
        released by the outer wake() finally block.
        """
        try:
            await self._store.advance_harness_cursor(
                session.id, through_event_id, lease.lease_token,
            )
        except Exception:
            logger.warning(
                "Failed to advance cursor at end of turn for %s",
                session.id,
            )

    async def _drain_background_tasks(self, session_id: UUID) -> None:
        """Wait for fire-and-forget background tasks to finish before lease release.

        Bounded by ``_BACKGROUND_DRAIN_TIMEOUT_SECONDS`` so a hung task can't
        delay lease release indefinitely.  Anything still pending after the
        timeout is cancelled; exceptions are swallowed because these tasks are
        best-effort by design.

        Tasks are dropped from ``self._background_tasks`` here instead of
        relying on the per-task ``done_callback`` to run later — the callback
        is scheduled separately on the loop and may not have fired by the time
        the caller inspects the set.
        """
        if not self._background_tasks:
            return
        pending = list(self._background_tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=_BACKGROUND_DRAIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            still_pending = [task for task in pending if not task.done()]
            logger.warning(
                "Background drain timed out for session %s; cancelling %d task(s)",
                session_id,
                len(still_pending),
            )
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)
        finally:
            for task in pending:
                self._background_tasks.discard(task)
    async def _maybe_emit_progress_checkin(
        self,
        session: Session,
        messages: list[dict],
        *,
        iteration_count: int,
        last_tool: str | None = None,
    ) -> None:
        """Emit an inbox progress check-in when the configured interval elapses."""

        interval = (session.config or {}).get("inbox_checkin_interval_seconds")
        if not interval:
            return
        try:
            interval_seconds = int(interval)
        except (TypeError, ValueError):
            return
        if interval_seconds <= 0:
            return

        latest = await self._store.last_event_at(
            session.id,
            EventType.INBOX_PROGRESS_CHECKIN,
        )
        created_at = session.created_at
        reference = latest or created_at
        if not isinstance(reference, datetime):
            return

        now = datetime.now(timezone.utc)
        if (now - _as_aware_utc(reference)).total_seconds() < interval_seconds:
            return

        await self._store.emit_event(
            session.id,
            EventType.INBOX_PROGRESS_CHECKIN,
            {
                "progress_summary": _last_assistant_message_excerpt(messages),
                "iterations": iteration_count,
                "last_tool": last_tool or "",
                "elapsed_seconds": _seconds_since(created_at),
            },
        )

    async def _drain_and_emit_turn_summary(
        self,
        *,
        session_id: UUID,
        turn_id: str,
        user_message: str,
    ) -> None:
        """Drain pending iteration summaries, then emit TURN_SUMMARY.

        Soft 10s cap on the drain so a hung iteration-summary task
        can't stall session completion. Same 10s cap on the turn
        summary call. Any failure is logged and swallowed — the SDK
        falls back to the per-iteration view when TURN_SUMMARY is
        missing.
        """
        if self._turn_summarizer is None:
            return

        pending = list(self._pending_iteration_summary_tasks.values())
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "iteration summary drain timed out for turn %s", turn_id,
                )

        # Read back the resolved iteration summaries in order so the
        # turn summarizer sees the same recap thread the SDK will
        # render. We re-query the event log because some iteration
        # tasks may have failed silently (returned None).
        try:
            iter_events = await self._store.get_events(
                session_id,
                types=[EventType.ITERATION_SUMMARY],
            )
        except Exception:
            logger.warning(
                "Failed to read iteration summaries for turn %s; "
                "summarizing without them.",
                turn_id,
                exc_info=True,
            )
            iter_events = []
        ordered = sorted(
            (
                e for e in iter_events
                if (getattr(e, "data", None) or {}).get("turn_id") == turn_id
            ),
            key=lambda e: (getattr(e, "data", None) or {}).get(
                "iteration_index", 0,
            ),
        )
        iteration_summaries = [
            str((getattr(e, "data", None) or {}).get("summary") or "")
            for e in ordered
        ]
        candidate_artifacts = await self._collect_candidate_artifacts(
            session_id=session_id, turn_id=turn_id,
        )

        try:
            # Outer backstop sits above the summarizer's own 30s
            # timeout — the turn summary runs on the base model, which
            # is slower than the cheap auxiliary.
            result = await asyncio.wait_for(
                self._turn_summarizer.summarize_turn(
                    turn_id=turn_id,
                    user_message=user_message,
                    iteration_summaries=iteration_summaries,
                    candidate_artifacts=candidate_artifacts,
                ),
                timeout=35.0,
            )
        except asyncio.TimeoutError:
            logger.warning("turn summary call timed out for %s", turn_id)
            return
        except Exception:
            logger.warning(
                "turn summary call failed for %s", turn_id, exc_info=True,
            )
            return
        if result is None:
            return

        try:
            await self._store.emit_event(
                session_id,
                EventType.TURN_SUMMARY,
                {
                    "turn_id": turn_id,
                    "recap": result.recap,
                    "artifacts": [
                        {"kind": a.kind, "label": a.label, "ref": a.ref}
                        for a in result.artifacts
                    ],
                },
            )
        except Exception:
            logger.warning(
                "Failed to emit TURN_SUMMARY for %s", turn_id, exc_info=True,
            )

    async def _collect_candidate_artifacts(
        self,
        *,
        session_id: UUID,
        turn_id: str,
    ) -> list[Any]:
        """Pull downloadable artifact candidates emitted during this turn.

        Returns a list of ``TurnArtifact`` instances from
        :mod:`surogates.harness.turn_summarizer` — workspace files and
        created artifacts only. The summarizer curates this list down
        to the user's actual deliverables; this method's job is to
        surface every plausibly-relevant file so the LLM can pick.

        Invariant: this method MUST only be called at the end of the
        queried turn (i.e. from ``_drain_and_emit_turn_summary`` inside
        ``_complete_session``). Once we see the first event bearing
        ``turn_id``, every following event is treated as "in this
        turn" — TOOL_CALL events don't themselves carry ``turn_id``,
        so we rely on chronological adjacency to LLM events that do.
        Calling this method before the current turn ends, or for a
        turn that's not the LAST in the log, would incorrectly
        attribute later turns' tool calls to this one.
        """
        from surogates.harness.turn_summarizer import (
            TurnArtifact,
            _is_internal_workspace_path,
        )

        out: list[TurnArtifact] = []
        try:
            # Scoped to the event types we actually inspect — keeps the
            # query cheap on long-running sessions with deep event logs.
            events = await self._store.get_events(
                session_id,
                types=[EventType.TOOL_CALL, EventType.ARTIFACT_CREATED,
                       EventType.LLM_REQUEST, EventType.LLM_RESPONSE],
            )
        except Exception:
            logger.debug(
                "Failed to read events for candidate artifacts on %s",
                session_id, exc_info=True,
            )
            return out

        in_turn = False
        terminal_commands: list[str] = []
        for evt in events:
            data = evt.data or {}
            if data.get("turn_id") == turn_id:
                in_turn = True
            if not in_turn:
                continue

            etype_str = evt.type.value if hasattr(evt.type, "value") else evt.type

            if etype_str == EventType.TOOL_CALL.value:
                # Tool-call payloads carry ``name`` and ``arguments`` per
                # the harness's TOOL_CALL emit contract; ``arguments``
                # is JSON-encoded for some tools, a dict for others.
                name = str(data.get("name") or "")
                raw_args = data.get("arguments")
                args = _coerce_tool_args(raw_args)

                if name in {"write_file", "patch"}:
                    path = (
                        args.get("path")
                        or args.get("file_path")
                        or args.get("name")
                        or ""
                    )
                    if (
                        isinstance(path, str)
                        and path
                        and not _is_internal_workspace_path(path)
                    ):
                        out.append(
                            TurnArtifact(kind="file", label=path, ref=path),
                        )
                elif name == "create_artifact":
                    label = args.get("name") or args.get("path") or ""
                    if isinstance(label, str) and label:
                        out.append(
                            TurnArtifact(
                                kind="artifact", label=label, ref=label,
                            ),
                        )
                elif name == "terminal":
                    # Not a candidate itself — the summary card only
                    # presents downloadable artifacts — but commands
                    # are kept to flag files the agent wrote and ran
                    # (scaffolding) further down.
                    cmd = args.get("command") or ""
                    if isinstance(cmd, str) and cmd:
                        terminal_commands.append(cmd)
            elif etype_str == EventType.ARTIFACT_CREATED.value:
                artifact_id = str(
                    data.get("artifact_id") or data.get("id") or "",
                )
                name = str(data.get("name") or artifact_id or "")
                if artifact_id and name:
                    out.append(
                        TurnArtifact(
                            kind="artifact", label=name, ref=artifact_id,
                        ),
                    )

        # Workspace mtime scan — surfaces files created indirectly
        # (terminal scripts, execute_code) that don't show up in the
        # tool-call stream. Deduped against the paths already added
        # via write_file/patch so the same file isn't listed twice.
        try:
            workspace_candidates = await self._scan_workspace_for_new_files(
                session_id=session_id,
                already_seen_paths={
                    a.ref for a in out if a.kind == "file"
                },
            )
        except Exception:
            logger.debug(
                "Workspace mtime scan failed for %s",
                session_id, exc_info=True,
            )
            workspace_candidates = []
        out.extend(workspace_candidates)

        # Flag intermediate scripts: a file the agent wrote and then
        # ran via terminal is almost always scaffolding (e.g. a python
        # script used to generate the real deliverable), not a final
        # artifact the user wanted. Annotate so the summarizer LLM can
        # filter them out — we don't drop here because the user
        # occasionally does ask for code, and the LLM gets to make
        # that call against the user message.
        annotated: list[TurnArtifact] = []
        for art in out:
            if art.kind != "file":
                annotated.append(art)
                continue
            executed = any(art.ref in cmd for cmd in terminal_commands)
            if executed:
                meta = dict(art.meta or {})
                meta["executed_by_terminal"] = True
                annotated.append(TurnArtifact(
                    kind=art.kind,
                    label=art.label,
                    ref=art.ref,
                    meta=meta,
                ))
            else:
                annotated.append(art)
        return annotated

    async def _scan_workspace_for_new_files(
        self,
        *,
        session_id: UUID,
        already_seen_paths: set[str],
    ) -> list[Any]:
        """Return file candidates for workspace objects modified during
        the current turn (mtime >= ``self._turn_started_at``).

        Skips entries already surfaced via tool-call inspection
        (``already_seen_paths``) to avoid duplicates. Uses ``list_entries``
        so mtime/size come from the bulk list response — no per-key HEAD
        round trips.
        """
        from surogates.harness.turn_summarizer import (
            TurnArtifact,
            _is_internal_workspace_path,
        )
        from surogates.storage.tenant import prefixed_session_workspace_prefix

        storage = self._storage
        if storage is None or self._turn_started_at is None:
            return []

        try:
            session = await self._store.get_session(session_id)
        except Exception:
            return []
        bucket = (session.config or {}).get("storage_bucket")
        if not bucket:
            return []
        root_id = (
            (session.config or {}).get("sandbox_root_session_id")
            or str(session.id)
        )
        prefix = prefixed_session_workspace_prefix(session.config, str(root_id))

        try:
            entries = await storage.list_entries(bucket, prefix=prefix)
        except Exception:
            logger.debug(
                "Workspace list_entries failed for bucket %r prefix %r",
                bucket, prefix, exc_info=True,
            )
            return []

        out: list[TurnArtifact] = []
        turn_start = self._turn_started_at
        for entry in entries:
            key = entry["key"]
            rel = key[len(prefix):] if key.startswith(prefix) else key
            if not rel or rel in already_seen_paths:
                continue
            if _is_internal_workspace_path(rel):
                continue
            modified = _coerce_modified_to_datetime(entry.get("modified"))
            if modified is None or modified < turn_start:
                continue
            out.append(
                TurnArtifact(kind="file", label=rel, ref=rel),
            )
        return out

    async def _complete_session(
        self,
        session: Session,
        messages: list[dict],
        lease: SessionLease,
        *,
        reason: str,
        through_event_id: int | None = None,
        cost_tracker: SessionCostTracker | None = None,
        turn_id: str | None = None,
        user_message: str | None = None,
    ) -> None:
        """Emit SESSION_COMPLETE and advance the cursor.

        When ``turn_id`` is supplied AND the completion reason represents
        a successful turn end (``stop``/``done``/``complete``/``completed``),
        drains any in-flight iteration-summary tasks and emits a
        ``TURN_SUMMARY`` event before ``SESSION_COMPLETE`` so the SDK
        sees the recap in the same event stream as the closing message.
        """
        # Destroy the sandbox pod for this session.
        if self._sandbox_pool is not None:
            try:
                await self._sandbox_pool.destroy_for_session(str(session.id))
            except Exception:
                logger.debug("Sandbox cleanup failed for %s", session.id, exc_info=True)

        # Close any browser session the agent opened this turn. Best
        # effort — a leaked browser pod is worse than a failed cleanup,
        # so swallow errors and never block completion on it.
        if self._browser_pool is not None:
            try:
                await self._browser_pool.destroy_for_session(str(session.id))
            except Exception:
                logger.debug("Browser cleanup failed for %s", session.id, exc_info=True)

        # Notify memory manager of session end.
        if self._memory_manager is not None:
            try:
                self._memory_manager.on_session_end(messages=[])
            except Exception:
                logger.debug("Memory manager on_session_end failed", exc_info=True)

        # Emit TURN_SUMMARY (if applicable) BEFORE SESSION_COMPLETE so
        # late-arriving SSE subscribers see them in event-id order.
        #
        # Mission sessions skip it: a mission coordinator ends its turn
        # repeatedly across the orchestration loop (dispatch, wait,
        # harvest, decide), and a "Task complete" recap after each one
        # reads as the chat stopping when the mission is still running.
        # ``active_mission_id`` is set while a mission is live and cleared
        # when it reaches a terminal state.
        is_mission_session = bool((session.config or {}).get("active_mission_id"))
        if (
            turn_id is not None
            and self._turn_summarizer is not None
            and reason in {"stop", "done", "complete", "completed"}
            and not is_mission_session
        ):
            try:
                await self._drain_and_emit_turn_summary(
                    session_id=session.id,
                    turn_id=turn_id,
                    user_message=user_message
                    if user_message is not None
                    else _latest_user_message_text(messages),
                )
            except Exception:
                logger.exception(
                    "Turn summary drain failed for %s", session.id,
                )

        complete_data: dict[str, Any] = {
            "reason": reason,
            "worker_id": self._worker_id,
        }
        if cost_tracker is not None:
            complete_data["cost_summary"] = cost_tracker.summary()

        await self._store.emit_event(
            session.id,
            EventType.SESSION_COMPLETE,
            complete_data,
        )
        inbox_event_id = await self._store.emit_event(
            session.id,
            EventType.INBOX_TASK_COMPLETE,
            {
                "outcome": (
                    "success"
                    if reason in {"stop", "done", "complete", "completed"}
                    else reason
                ),
                "summary": _last_assistant_message_excerpt(messages),
                "duration_seconds": _seconds_since(session.created_at),
                "session_title": session.title or "Task complete",
                "error": None,
            },
        )
        try:
            await self._store.update_session_status(session.id, "completed")
        except Exception:
            logger.warning(
                "Failed to update session status to completed for %s",
                session.id,
                exc_info=True,
            )

        # Notify parent session if this is a worker (child) session.
        # Scheduled loop runs use parent_id for traceability in the session
        # tree, but should not wake the parent as if they were sub-agent work.
        if _should_notify_parent_on_completion(session):
            from surogates.harness.worker_notify import notify_parent_on_completion
            try:
                await notify_parent_on_completion(
                    session_store=self._store,
                    worker_session_id=session.id,
                    parent_session_id=session.parent_id,
                    org_id=str(session.org_id),
                    agent_id=session.agent_id,
                    redis=self._redis,
                    task_id=getattr(session, "task_id", None),
                    session_factory=self._session_factory,
                )
            except Exception:
                logger.warning(
                    "Failed to notify parent %s of worker %s completion",
                    session.parent_id, session.id,
                    exc_info=True,
                )

        await self._finalize_dynamic_loop_if_needed(session)

        # Advance cursor to the latest event.
        cursor_target = (
            through_event_id if through_event_id is not None else inbox_event_id
        )
        try:
            await self._store.advance_harness_cursor(
                session.id, cursor_target, lease.lease_token,
            )
        except Exception:
            logger.warning(
                "Failed to advance cursor after session completion for %s",
                session.id,
            )

    async def _finalize_dynamic_loop_if_needed(self, session: Session) -> None:
        if not session.config.get("scheduled_dynamic_loop"):
            return
        schedule_id_raw = session.config.get("scheduled_session_id")
        if not schedule_id_raw:
            return
        # Either the user or the service account that minted the schedule
        # may own the row.  Anonymous-channel sessions never reach here
        # (they cannot create schedules), but defensive check anyway.
        if self._tenant.user_id is None and self._tenant.service_account_id is None:
            return

        from surogates.scheduled.schedule import DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS
        from surogates.scheduled.store import ScheduledSessionStore

        try:
            schedule_id = UUID(str(schedule_id_raw))
        except ValueError:
            logger.warning("Invalid dynamic loop id in session config: %s", schedule_id_raw)
            return

        store = ScheduledSessionStore(self._session_factory)
        try:
            schedule = await store.get(schedule_id)
        except KeyError:
            return
        if schedule.next_run_at is not None or schedule.last_session_id != session.id:
            return

        await store.mark_dynamic_run_finished(
            schedule_id=schedule_id,
            org_id=self._tenant.org_id,
            user_id=self._tenant.user_id,
            service_account_id=self._tenant.service_account_id,
            agent_id=session.agent_id,
            session_id=session.id,
            delay_seconds=DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
            reason="The agent did not call loop_wait; using the fallback delay.",
        )
