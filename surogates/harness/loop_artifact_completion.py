"""Artifact promotion, progress, summary, and completion helpers for AgentHarness."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.channels.constants import DIRECT_UI_CHANNELS
from surogates.harness.loop_artifacts import (
    _FENCE_RE,
    _PROMOTABLE_FENCES,
    _coerce_modified_to_datetime,
    _coerce_tool_args,
    _derive_artifact_name,
)
from surogates.harness.delivery_manifest import (
    check_terminal_claim,
    reconcile,
)
from surogates.harness.loop_constants import _BACKGROUND_DRAIN_TIMEOUT_SECONDS
from surogates.harness.loop_messages import (
    _as_aware_utc,
    _is_scheduled_run,
    _last_assistant_message_excerpt,
    _latest_user_message_text,
    _seconds_since,
    _should_notify_parent_on_completion,
)
from surogates.harness.message_utils import extract_final_response
from surogates.session.events import EventType
from surogates.session.inbox_payload import raises_completion_inbox_item

logger = logging.getLogger(__name__)


def _should_take_reservations(session: Any, config_key: str) -> bool:
    """Whether the worker must pop ``config_key`` from the live session
    config at settle time.

    Channel gate, not a config gate: the wake-time session object is
    stale, and a hold appended after wake start must still be taken — only
    website sessions ever carry these, so every other channel skips the
    extra round trip unless the wake object already shows a hold. Shared by
    the commerce and per-user allowance settlements."""
    if getattr(session, "channel", None) == "website":
        return True
    return bool((session.config or {}).get(config_key))


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

    def _spawn_background(self, coro, *, name: str) -> None:
        """Run *coro* detached, but still inside the end-of-turn drain.

        Registering it in ``_background_tasks`` is the point: the work
        stops delaying SESSION_COMPLETE, yet the worker still waits for
        it (bounded) before releasing the lease, so a detached teardown
        cannot outlive the wake that started it.
        """
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _destroy_sandbox_quietly(
        self, sandbox_id: str | None, session_id: str,
    ) -> None:
        """Delete a detached sandbox; never raise into the drain."""
        try:
            await self._sandbox_pool.destroy_released(sandbox_id, session_id)
        except Exception:
            logger.debug(
                "Sandbox cleanup failed for %s", session_id, exc_info=True,
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
        final_message: str = "",
    ) -> None:
        """Drain pending iteration summaries, then emit TURN_SUMMARY.

        Soft 10s cap on the drain so a hung iteration-summary task
        can't stall session completion. Same 10s cap on the turn
        summary call. Any failure is logged and swallowed — the SDK
        falls back to the per-iteration view when TURN_SUMMARY is
        missing.
        """
        # No early return on a missing summarizer: the manifest needs no
        # model, so the download card outlives the recap.
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
        candidate_artifacts, entries_by_path = (
            await self._collect_candidate_artifacts(
                session_id=session_id, turn_id=turn_id,
            )
        )

        # Which candidates are real deliverables is now decided against
        # the workspace rather than by asking a model to pick. A file
        # that is present-but-empty or present-but-older-than-this-turn
        # is not a delivery, however convincing it looks in a prompt.
        manifest = reconcile(
            candidate_artifacts,
            entries_by_path=entries_by_path,
            turn_start=self._turn_started_at,
        )
        manifest = check_terminal_claim(manifest, final_message)
        if manifest.rejected:
            logger.info(
                "Turn %s: dropped %d candidate(s) the workspace does not "
                "support: %s",
                turn_id, len(manifest.rejected),
                ", ".join(f"{r.ref}({r.reason})" for r in manifest.rejected),
            )

        delivered = manifest.delivered
        recap = ""

        if self._turn_summarizer is not None:
            # One question the workspace cannot answer: which of several
            # real files the user actually asked for. Only asked when
            # more than one survives -- the common single-file turn never
            # makes the call.
            try:
                delivered = await asyncio.wait_for(
                    self._turn_summarizer.pick_deliverables(
                        turn_id=turn_id,
                        user_message=user_message,
                        artifacts=manifest.delivered,
                    ),
                    timeout=35.0,
                )
            except Exception:
                # Fail open: an extra entry beats a missing one.
                logger.debug(
                    "deliverable pick failed for %s", turn_id, exc_info=True,
                )

            try:
                # Outer backstop sits above the summarizer's own timeout.
                result = await asyncio.wait_for(
                    self._turn_summarizer.summarize_turn(
                        turn_id=turn_id,
                        user_message=user_message,
                        iteration_summaries=iteration_summaries,
                        artifacts=delivered,
                    ),
                    timeout=35.0,
                )
            except asyncio.TimeoutError:
                logger.warning("turn summary call timed out for %s", turn_id)
                result = None
            except Exception:
                logger.warning(
                    "turn summary call failed for %s", turn_id, exc_info=True,
                )
                result = None
            if result is not None:
                recap = result.recap
                delivered = result.artifacts

        # With recaps off there is nothing to say but still something to
        # show, so a turn that produced nothing emits nothing rather than
        # an empty card.
        if not recap and not delivered and not manifest.unsupported_claim:
            return

        try:
            await self._store.emit_event(
                session_id,
                EventType.TURN_SUMMARY,
                {
                    "turn_id": turn_id,
                    "recap": recap,
                    # Advisory, and only ever set when the turn delivered
                    # nothing at all: the closing message claimed a file
                    # that was never written. Surfaced rather than acted
                    # on -- wrongly telling someone their work failed is
                    # worse than saying nothing.
                    **(
                        {"unsupported_claim": manifest.unsupported_claim}
                        if manifest.unsupported_claim
                        else {}
                    ),
                    **(
                        {"rejected": [
                            {"ref": r.ref, "reason": r.reason}
                            for r in manifest.rejected
                        ]}
                        if manifest.rejected
                        else {}
                    ),
                    "artifacts": [
                        {"kind": a.kind, "label": a.label, "ref": a.ref}
                        for a in delivered
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
    ) -> tuple[list[Any], dict[str, dict[str, Any]]]:
        """Pull downloadable artifact candidates emitted during this turn.

        Returns the candidates and the workspace listing they were
        checked against -- reconciliation needs size and mtime for
        candidates that came from tool calls, not only for the ones the
        scan found.

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
                # ARTIFACT_UPDATED matters as much as ARTIFACT_CREATED: a
                # turn that revises an artifact emits only the former, and
                # without it that turn's card falls back to the name-keyed
                # candidate below, whose ref never resolves to a panel.
                types=[EventType.TOOL_CALL, EventType.ARTIFACT_CREATED,
                       EventType.ARTIFACT_UPDATED,
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
            elif etype_str in (
                EventType.ARTIFACT_CREATED.value,
                EventType.ARTIFACT_UPDATED.value,
            ):
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

        # The tool-call branch keys an artifact candidate by name, because
        # the id only exists once the API has answered. When the event did
        # land, that placeholder is a second row for the same artifact
        # whose ref resolves to nothing -- drop it in favour of the id.
        resolved = {
            a.label for a in out
            if a.kind == "artifact" and a.ref != a.label
        }
        out = [
            a for a in out
            if not (
                a.kind == "artifact"
                and a.ref == a.label
                and a.label in resolved
            )
        ]

        # Workspace mtime scan — surfaces files created indirectly
        # (terminal scripts, execute_code) that don't show up in the
        # tool-call stream. Deduped against the paths already added
        # via write_file/patch so the same file isn't listed twice.
        try:
            workspace_candidates, entries_by_path = (
                await self._scan_workspace_for_new_files(
                    session_id=session_id,
                    already_seen_paths={
                        a.ref for a in out if a.kind == "file"
                    },
                )
            )
        except Exception:
            logger.debug(
                "Workspace mtime scan failed for %s",
                session_id, exc_info=True,
            )
            workspace_candidates, entries_by_path = [], {}
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
        return annotated, entries_by_path

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
        from surogates.storage.tenant import boundary_workspace_prefix

        storage = self._storage
        if storage is None or self._turn_started_at is None:
            return [], {}

        try:
            session = await self._store.get_session(session_id)
        except Exception:
            return [], {}
        bucket = (session.config or {}).get("storage_bucket")
        if not bucket:
            return [], {}
        root_id = (
            (session.config or {}).get("sandbox_root_session_id")
            or str(session.id)
        )
        prefix = boundary_workspace_prefix(session.config, session, str(root_id))

        try:
            entries = await storage.list_entries(bucket, prefix=prefix)
        except Exception:
            logger.debug(
                "Workspace list_entries failed for bucket %r prefix %r",
                bucket, prefix, exc_info=True,
            )
            return [], {}

        out: list[TurnArtifact] = []
        # Keyed by workspace-relative path and returned alongside the
        # candidates: reconciliation needs size/mtime for candidates that
        # came from tool calls too, and this listing is the only place
        # they are observable without a per-file HEAD.
        entries_by_path: dict[str, dict[str, Any]] = {}
        turn_start = self._turn_started_at
        for entry in entries:
            key = entry["key"]
            rel = key[len(prefix):] if key.startswith(prefix) else key
            if not rel or rel in already_seen_paths:
                continue
            # Directory markers are not downloadable. Object stores list
            # them as zero-byte keys ending in "/", so they only ever got
            # dropped for being empty -- a backend that reports a size
            # for them would have put __pycache__/ on the download card.
            if rel.endswith("/"):
                continue
            if _is_internal_workspace_path(rel):
                continue
            modified = _coerce_modified_to_datetime(entry.get("modified"))
            entries_by_path[rel] = {
                "size": entry.get("size"), "modified": modified,
            }
            if modified is None or modified < turn_start:
                continue
            out.append(
                TurnArtifact(kind="file", label=rel, ref=rel),
            )
        return out, entries_by_path

    async def _resolve_loop_result_parent(self, session: Session) -> Session | None:
        """Return the direct-UI parent that should receive this loop run result.

        Messaging-platform parents are excluded on purpose: their result
        reaches the user through the channel's own outbound adapter, so
        delivering it here as well would double-post.
        """
        if not _is_scheduled_run(session) or session.parent_id is None:
            return None

        from surogates.session.store import SessionNotFoundError

        try:
            parent = await self._store.get_session(session.parent_id)
        except SessionNotFoundError:
            return None

        if parent.channel not in DIRECT_UI_CHANNELS:
            return None
        return parent

    async def _settle_commerce_reservation(
        self,
        session: Session,
        cost_tracker: SessionCostTracker | None,
    ) -> None:
        """Settle the monetized-turn holds pinned at message accept.

        Takes the whole ``commerce_reservations`` list from the LIVE
        session config atomically (not the session object loaded at
        wake start — follow-up messages may have appended holds since),
        then debits the wake's total LLM usage (input + output, the
        same summing the hosted buy page's forwarder reports) against
        the oldest hold. The remaining holds release with zero usage:
        their messages were folded into this wake, so the total already
        charges their consumption. Best-effort throughout — a hold
        whose settlement fails is reclaimed by the ops reservation
        reaper, and a debit that arrives after the reaper released the
        hold still charges the usage without double-releasing.
        Without a cost tracker each hold's reserved amount is consumed
        as the floor: content may already have been delivered, and a
        hold must never turn into a free turn.
        """
        if not _should_take_reservations(session, "commerce_reservations"):
            return
        client = getattr(self, "_platform_client", None)
        if client is None:
            logger.warning(
                "Session %s carries commerce reservations but the "
                "worker has no platform client; leaving the holds to "
                "the ops reaper",
                session.id,
            )
            return
        try:
            taken = await self._store.pop_session_config_key(
                session.id, "commerce_reservations",
            )
        except Exception:
            logger.warning(
                "Failed to take commerce reservations for session %s; "
                "the ops reaper will release them",
                session.id,
                exc_info=True,
            )
            return
        reservations = [r for r in (taken or []) if isinstance(r, dict)]
        if not reservations:
            return
        actual_total = (
            cost_tracker.total_input_tokens + cost_tracker.total_output_tokens
            if cost_tracker is not None
            else None
        )
        for index, reservation in enumerate(reservations):
            reserved = int(reservation.get("reserved_tokens") or 0)
            if actual_total is None:
                actual = reserved
            else:
                actual = actual_total if index == 0 else 0
            try:
                await client.commerce_debit(
                    session.agent_id,
                    entitlement_id=str(
                        reservation.get("entitlement_id") or "",
                    ),
                    reserved_tokens=reserved,
                    actual_tokens=actual,
                    reservation_id=reservation.get("reservation_id") or None,
                )
            except Exception:
                logger.warning(
                    "Commerce settlement failed for session %s "
                    "(reservation %s); the ops reservation reaper will "
                    "release the hold",
                    session.id,
                    reservation.get("reservation_id"),
                    exc_info=True,
                )

    async def _settle_allowance_reservation(
        self,
        session: Session,
        cost_tracker: SessionCostTracker | None,
    ) -> None:
        """Settle the per-user allowance holds pinned at message accept.

        Mirrors :meth:`_settle_commerce_reservation` but for the operator-
        granted per-user cap: pops the whole ``allowance_reservations``
        list and debits the wake's total LLM usage against the oldest hold
        (the rest release with zero usage — their messages folded into
        this wake, which already charges their tokens). All holds on a web
        session belong to the same end-user, so the wake total is the
        right per-user charge.

        Gated on the wake-time config carrying holds, so uncapped agents
        (the default) skip the round trip. Website sessions always pop the
        live config regardless of the stale wake object: an embed hold
        pinned by ``send_website_message`` after wake start would otherwise
        leak (there is no allowance reaper), the same escape the commerce
        settle makes for website sessions.
        """
        if not _should_take_reservations(session, "allowance_reservations"):
            return
        client = getattr(self, "_platform_client", None)
        if client is None:
            logger.warning(
                "Session %s carries allowance reservations but the worker "
                "has no platform client; the next cycle refill clears them",
                session.id,
            )
            return
        try:
            taken = await self._store.pop_session_config_key(
                session.id, "allowance_reservations",
            )
        except Exception:
            logger.warning(
                "Failed to take allowance reservations for session %s; "
                "the next cycle refill will clear them",
                session.id,
                exc_info=True,
            )
            return
        reservations = [r for r in (taken or []) if isinstance(r, dict)]
        if not reservations:
            return
        actual_total = (
            cost_tracker.total_input_tokens + cost_tracker.total_output_tokens
            if cost_tracker is not None
            else None
        )
        for index, reservation in enumerate(reservations):
            reserved = int(reservation.get("reserved_tokens") or 0)
            if actual_total is None:
                actual = reserved
            else:
                actual = actual_total if index == 0 else 0
            try:
                await client.allowance_debit(
                    session.agent_id,
                    allowance_id=str(reservation.get("allowance_id") or ""),
                    reserved_tokens=reserved,
                    actual_tokens=actual,
                    reservation_id=reservation.get("reservation_id") or None,
                )
            except Exception:
                logger.warning(
                    "Allowance settlement failed for session %s "
                    "(reservation %s); the next cycle refill clears the hold",
                    session.id,
                    reservation.get("reservation_id"),
                    exc_info=True,
                )

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
        # Detach the sandbox now, delete the pod after.
        #
        # Deleting a pod is a round trip to the cluster, and it used to sit
        # between the agent's last word and SESSION_COMPLETE -- so the user
        # watched a busy indicator through it. Measured over a month of
        # production sessions: with neither a sandbox nor a turn summary the
        # tail is 0.24s at p50, and sessions that used a sandbox reach 32s at
        # p90. Nothing downstream reads the teardown, and a leaked pod is
        # already reclaimed on worker shutdown.
        #
        # Detaching stays synchronous because it is in-memory and it carries
        # the ordering that matters: once the mapping is gone, no later turn
        # can resolve this session to a pod that is about to disappear.
        if self._sandbox_pool is not None:
            try:
                sandbox_id = await self._sandbox_pool.release_for_session(
                    str(session.id),
                )
            except Exception:
                logger.debug(
                    "Sandbox detach failed for %s", session.id, exc_info=True,
                )
            else:
                self._spawn_background(
                    self._destroy_sandbox_quietly(sandbox_id, str(session.id)),
                    name=f"sandbox-teardown-{session.id}",
                )

        # The browser is intentionally NOT torn down here. A turn end is
        # not a session end: an agent driving a multi-step browser flow
        # (e.g. logging into a site across several user interactions)
        # needs cookies and page state to survive between turns. The
        # browser persists in the session-keyed pool and is reclaimed on
        # reprovision, explicit browser_close, or worker shutdown.

        # Notify memory manager of session end.
        if self._memory_manager is not None:
            try:
                self._memory_manager.on_session_end(messages=[])
            except Exception:
                logger.debug("Memory manager on_session_end failed", exc_info=True)

        # Emit TURN_SUMMARY (if applicable) BEFORE SESSION_COMPLETE so
        # late-arriving SSE subscribers see them in event-id order.
        #
        # Orchestrated sessions skip it: a mission / auto-research
        # coordinator ends its turn repeatedly across the orchestration loop
        # (dispatch, wait, harvest, decide), and a "Task complete" recap
        # after each one reads as the chat stopping when the run is still
        # going. ``active_mission_id`` marks a live mission; an Arbor
        # research coordinator also carries ``active_research_run_id`` (and
        # keeps running report turns even after the mission id is cleared at
        # a terminal verdict), so suppress on either key.
        config = session.config or {}
        is_orchestrated_session = bool(
            config.get("active_mission_id")
            or config.get("active_research_run_id")
        )
        # Not gated on the summarizer: deciding what was delivered is
        # bookkeeping now, so the download card survives with recaps
        # turned off. Only the recap itself needs a model.
        if (
            turn_id is not None
            and reason in {"stop", "done", "complete", "completed"}
            and not is_orchestrated_session
        ):
            try:
                await self._drain_and_emit_turn_summary(
                    session_id=session.id,
                    turn_id=turn_id,
                    user_message=user_message
                    if user_message is not None
                    else _latest_user_message_text(messages),
                    # The closing message is the only place a delivery
                    # claim with nothing behind it can be seen.
                    final_message=_last_assistant_message_excerpt(messages),
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

        # Two independent best-effort settlements (neither raises); run
        # them concurrently to halve the session-complete round trip when
        # both a commerce and an allowance hold are present.
        await asyncio.gather(
            self._settle_commerce_reservation(session, cost_tracker),
            self._settle_allowance_reservation(session, cost_tracker),
        )

        session_complete_event_id = await self._store.emit_event(
            session.id,
            EventType.SESSION_COMPLETE,
            complete_data,
        )
        outcome = (
            "success"
            if reason in {"stop", "done", "complete", "completed"}
            else reason
        )

        loop_result_parent = None
        try:
            loop_result_parent = await self._resolve_loop_result_parent(session)
        except Exception:
            logger.debug(
                "Failed to resolve loop.result parent for %s",
                session.id,
                exc_info=True,
            )

        if loop_result_parent is not None:
            try:
                child_events = await self._store.get_events(session.id)
                content = extract_final_response(child_events, fallback="").strip()
                if content:
                    await self._store.emit_event(
                        loop_result_parent.id,
                        EventType.LOOP_RESULT,
                        {
                            "run_session_id": str(session.id),
                            "scheduled_session_id": str(
                                (session.config or {}).get("scheduled_session_id") or ""
                            ),
                            "content": content,
                            "outcome": outcome,
                            "duration_seconds": _seconds_since(session.created_at),
                            "run_completed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
            except Exception:
                logger.warning(
                    "Failed to emit loop.result on parent %s for run %s",
                    loop_result_parent.id,
                    session.id,
                    exc_info=True,
                )

        inbox_event_id: int | None = None
        # A scheduled run keeps announcing itself whatever its channel: it
        # is unwatched work by construction, and the branch above already
        # took the web/api parents that receive the result inline instead.
        if loop_result_parent is None and (
            _is_scheduled_run(session) or raises_completion_inbox_item(session)
        ):
            inbox_event_id = await self._store.emit_event(
                session.id,
                EventType.INBOX_TASK_COMPLETE,
                {
                    "outcome": outcome,
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
            through_event_id
            if through_event_id is not None
            else (
                inbox_event_id
                if inbox_event_id is not None
                else session_complete_event_id
            )
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
