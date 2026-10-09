"""Artifact promotion, progress, summary, and completion helpers for AgentHarness."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from surogates.artifacts.store import FolderArtifacts
from surogates.channels.constants import DIRECT_UI_CHANNELS, REALTIME_CHANNELS
from surogates.devices.binding import device_of
from surogates.harness.loop_artifacts import (
    _FENCE_RE,
    _PROMOTABLE_FENCES,
    _coerce_modified_to_datetime,
    _coerce_tool_args,
    _derive_artifact_name,
    _terminal_executes_file,
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
    _seconds_since,
    _should_notify_parent_on_completion,
)
from surogates.harness.message_utils import extract_final_response
from surogates.session.events import EventType
from surogates.session.files import HARNESS_WITHIN_S, gave_up_level, session_files
from surogates.devices.workspace import WALK_MARGIN_NS
from surogates.tools.utils.tool_result_storage import WORKSPACE_STORAGE_DIR, keep_out_of_git
from surogates.session.inbox_payload import raises_completion_inbox_item
from surogates.harness.landing import (
    _fence,
    keep_copy,
    land_turn,
    left_alone,
    prune_later,
    redo_files,
    turn_ended,
    waiting_on_you,
)
from surogates.sandbox.pool import sandbox_session_key
from surogates.workstreams.history import waits_to_land
from surogates.workstreams import is_project_master, is_project_thread
from surogates.workstreams.spend import admitted_at_wake

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
    # A project's turn is held at its wake, after the wake read the session.
    return bool((session.config or {}).get(config_key)) or admitted_at_wake(session)


def summary_ruled_out(session: Any) -> bool:
    """Whether the session's config or channel rules every turn's recap out, whatever the turn does (see wants_turn_summary)."""
    config = getattr(session, "config", None) or {}
    return bool(
        config.get("active_mission_id")
        or config.get("active_research_run_id")
        or is_project_master(config)
        or getattr(session, "channel", None) in REALTIME_CHANNELS
    )


def wants_turn_summary(session: Any, *, turn_id: str | None, reason: str) -> bool:
    """Whether a finished turn gets its recap and deliverables scan.

    Orchestrated sessions skip it: a mission / auto-research coordinator ends its turn repeatedly
    across the orchestration loop, and a "Task complete" recap after each one reads as the chat
    stopping while the run goes on (``active_mission_id``, or ``active_research_run_id`` which an
    Arbor coordinator keeps after a terminal verdict). A project's master skips it the same way: it
    ends a turn at every exchange while its threads work on. A phone call skips it too: the caller heard
    the answer, nothing renders a recap card on a call, and the drain (up to 10 s of summary calls)
    holds the session while the caller's next words wait for it.
    """
    if summary_ruled_out(session):
        return False
    return turn_id is not None and reason in {"stop", "done", "complete", "completed"}


async def resolve_loop_result_parent(store: Any, session: Any) -> Any | None:
    """Return the direct-UI parent that should receive this loop run result.

    Messaging-platform parents are excluded on purpose: their result
    reaches the user through the channel's own outbound adapter, so
    delivering it here as well would double-post.
    """
    if not _is_scheduled_run(session) or session.parent_id is None:
        return None

    from surogates.session.store import SessionNotFoundError

    try:
        parent = await store.get_session(session.parent_id)
    except SessionNotFoundError:
        return None

    if parent.channel not in DIRECT_UI_CHANNELS:
        return None
    return parent


async def announce_failure(store: Any, session: Any, *, error: str, summary: str = "") -> None:
    """Say that *session* failed where its user reads its result: a
    scheduled run's ``loop.result`` on its direct-UI parent, else a failed
    ``task_complete`` item for a scheduled run or an inbox-announced root.

    The turn's own failure and the dispatcher's give-up both end here, so a
    run reports the same way however it fails.
    """
    parent = None
    try:
        parent = await resolve_loop_result_parent(store, session)
    except Exception:
        logger.debug(
            "Failed to resolve loop.result parent for %s", session.id, exc_info=True,
        )
    if parent is not None:
        try:
            await store.emit_event(
                parent.id,
                EventType.LOOP_RESULT,
                {
                    "run_session_id": str(session.id),
                    "scheduled_session_id": str(
                        (session.config or {}).get("scheduled_session_id") or ""
                    ),
                    "content": error,
                    "outcome": "failed",
                    "duration_seconds": _seconds_since(session.created_at),
                    "run_completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            logger.warning(
                "Failed to emit loop.result on parent %s for run %s",
                parent.id, session.id, exc_info=True,
            )
    elif _is_scheduled_run(session) or raises_completion_inbox_item(session):
        await store.emit_event(
            session.id,
            EventType.INBOX_TASK_COMPLETE,
            {
                "outcome": "failed",
                "summary": summary,
                "duration_seconds": _seconds_since(session.created_at),
                "session_title": session.title or "Task failed",
                "error": error,
            },
        )


# The file whose stamp is where a local folder's turn begins: written by the
# folder's own filesystem, so its clock is the one that stamps the turn's
# files, a network share's or a FAT stick's included.
_TURN_MARK = f"{WORKSPACE_STORAGE_DIR}/.turn"


class ArtifactCompletionMixin:
    #: The turn's tool sagas, set when its loop starts; its end completes them.
    _turn_saga: Any = None
    #: Where a local folder's turn began by its folder's clock, a walk's
    #: cursor; None in the cloud, or when the computer could not say.
    _turn_cursor: str | None = None
    #: Whether the turn asked its folder where it began: at its first tool
    #: call.  One that made none changed nothing there, and walks nothing.
    _turn_marked: bool = False
    #: The last event before the turn's loop started: the turn's own come after it.
    _turn_after_event_id: int = 0

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
        - an API client is wired (``self._api_client``), or the session
          is on a local folder, where it is made in the folder, within
          ``HARNESS_WITHIN_S``,
        - the content contains at least one promotable fence (svg/html),
        - the fence body parses as non-empty.

        At most ONE artifact is created per response, matching the
        guidance's one-artifact-per-response rule.  Failures are logged
        but swallowed — a failed auto-promotion must not derail the
        turn.
        """
        on_device = device_of(session.config) is not None
        if (self._api_client is None and not on_device) or not assistant_content:
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
                if on_device:
                    async with asyncio.timeout(HARNESS_WITHIN_S), session_files(
                        session, storage=self._storage, session_factory=self._session_factory, redis=self._redis,
                    ) as files:
                        await FolderArtifacts(files, self._store, session.id, sandbox_session_key(session)).create_artifact(
                            name=name, kind=kind, spec={spec_key: body},
                        )
                else:
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
    ) -> list[dict[str, Any]] | None:
        """Drain pending iteration summaries, then emit TURN_SUMMARY.

        Soft 10s cap on the drain so a hung iteration-summary task
        can't stall session completion. Same 10s cap on the turn
        summary call. Any failure is logged and swallowed — the SDK
        falls back to the per-iteration view when TURN_SUMMARY is
        missing.

        Returns the deliverables the summary named, or None when it was
        not written, so a worker's report lists no files it cannot know.
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
            entries_by_path=entries_by_path or {},
            turn_start=self._turn_started_at,
        )
        if entries_by_path is not None:
            # A folder that could not be listed is unseen, not empty: a claim
            # of a file its commands made is no false claim.
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
            return []

        artifacts = [{"kind": a.kind, "label": a.label, "ref": a.ref} for a in delivered]
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
                    "artifacts": artifacts,
                },
            )
        except Exception:
            logger.warning(
                "Failed to emit TURN_SUMMARY for %s", turn_id, exc_info=True,
            )
            return None
        return artifacts

    async def _collect_candidate_artifacts(
        self,
        *,
        session_id: UUID,
        turn_id: str,
    ) -> tuple[list[Any], dict[str, dict[str, Any]] | None]:
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

        # Reconciliation rejects executed helper scripts as scaffolding.
        # Only execution counts: inspecting a PDF with pdfinfo or passing
        # an output path to a generator must not discard the deliverable.
        annotated: list[TurnArtifact] = []
        for art in out:
            if art.kind != "file":
                annotated.append(art)
                continue
            executed = any(
                _terminal_executes_file(cmd, art.ref)
                for cmd in terminal_commands
            )
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

        if self._turn_started_at is None:
            return [], {}
        try:
            session = await self._store.get_session(session_id)
        except Exception:
            return [], {}
        if device_of(session.config) is not None:
            # A local folder's files are its computer's to list; its cloud
            # prefix is metadata, never permission to read one.
            return await self._scan_folder_for_new_files(session, already_seen_paths)
        storage = self._storage
        if storage is None:
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

    async def _walk_folder(self, session: Any, *, since: str) -> Any | None:
        """The files of *session*'s local folder changed since the cursor *since*, as the file panel's tree sees them.

        None when its computer cannot say within ``HARNESS_WITHIN_S``: the
        turn's files are best effort.
        """
        from surogates.api.routes.workspace import _FOLDER_TOP_HIDDEN, _SKIP_DIRS

        try:
            async with asyncio.timeout(HARNESS_WITHIN_S), session_files(
                session, storage=self._storage, session_factory=self._session_factory, redis=self._redis,
            ) as files:
                return await files.walk(
                    await files.resolve(""), skip=_SKIP_DIRS, skip_top=_FOLDER_TOP_HIDDEN, skip_hidden=True, since=since,
                )
        except Exception as exc:
            logger.log(
                gave_up_level(exc), "Session %s: its folder's files could not be listed", session.id, exc_info=True,
            )
            return None

    async def _folder_cursor(self, session: Any) -> str | None:
        """Where a local folder's turn begins, by the folder's own clock: the stamp of a mark written there, less the walk's margin.

        A few small operations, never a walk.  None when its computer cannot
        say within ``HARNESS_WITHIN_S``.
        """
        try:
            async with asyncio.timeout(HARNESS_WITHIN_S), session_files(
                session, storage=self._storage, session_factory=self._session_factory, redis=self._redis,
            ) as files:
                await keep_out_of_git(files)
                mark = await files.resolve(_TURN_MARK)
                await files.write(mark, b"")
                stamped = await files.stat(mark)
        except Exception as exc:
            logger.log(
                gave_up_level(exc), "Session %s: its folder's clock could not be read", session.id, exc_info=True,
            )
            return None
        if stamped is None:
            return None
        return str(max(0, int(stamped.mtime * 1_000_000_000) - WALK_MARGIN_NS))

    async def _mark_turn_start(self, session: Any) -> None:
        """Before a local folder's turn's first tool call: where the turn begins there (see _folder_cursor).

        Awaited before the call runs, never in the background: a mark that
        landed after a tool's first write would miss its file.  Not taken for
        a turn whose recap is ruled out, which lists no files.
        """
        if self._turn_marked or device_of(session.config) is None or summary_ruled_out(session):
            return
        self._turn_marked = True
        self._turn_cursor = await self._folder_cursor(session)

    async def _scan_folder_for_new_files(
        self, session: Any, already_seen_paths: set[str],
    ) -> tuple[list[Any], dict[str, dict[str, Any]] | None]:
        """A local folder's turn's files: what changed there since the turn began, by the folder's own clock.

        Each is listed by its path from the folder's top, and by the folder's
        own path, as the agent may have named it.  No ``modified``: the
        folder's clock chose them, and the "stale" rule would compare the
        server's.  A turn that made no tool call lists none, and walks
        nothing.  A turn that began while its computer was away lists none,
        and neither does one whose folder its computer did not list: their
        entries are None, as nothing was seen.
        """
        from surogates.harness.turn_summarizer import (
            TurnArtifact,
            _is_internal_workspace_path,
        )

        if not self._turn_marked:
            # No tool call: the turn changed nothing in the folder.
            return [], {}
        if self._turn_cursor is None:
            logger.info(
                "Session %s: its computer did not say where this turn began, so the turn lists none of its folder's files",
                session.id,
            )
            return [], None
        walked = await self._walk_folder(session, since=self._turn_cursor)
        if walked is None:
            return [], None
        root = session.config["workspace_path"].rstrip("/")
        out: list[TurnArtifact] = []
        entries_by_path: dict[str, dict[str, Any]] = {}
        for rel, size in walked.files:
            if _is_internal_workspace_path(rel, on_folder=True):
                continue
            entries_by_path[rel] = entries_by_path[f"{root}/{rel}"] = {"size": size, "modified": None}
            if rel not in already_seen_paths and f"{root}/{rel}" not in already_seen_paths:
                out.append(TurnArtifact(kind="file", label=rel, ref=rel))
        return out, entries_by_path

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
        user_message: str = "",
    ) -> None:
        """Emit SESSION_COMPLETE and advance the cursor.

        When ``turn_id`` is supplied AND the completion reason represents
        a successful turn end (``stop``/``done``/``complete``/``completed``),
        drains any in-flight iteration-summary tasks and emits a
        ``TURN_SUMMARY`` event before ``SESSION_COMPLETE`` so the SDK
        sees the recap in the same event stream as the closing message.

        A project's thread lands its turn first: before its pod goes, and
        before the turn summary and the report, so both see the landed files.
        """
        landing: dict[str, Any] | None = None
        # The turn is over: what it handed on lands with it, and is no later Stop's to drop.
        turn_ended(session)
        if is_project_thread(session.config) and self._sandbox_pool is not None:
            tool_saga = self._turn_saga.current_saga if self._turn_saga is not None else None
            try:
                await self._open_copy_to_land(session)
                landing = await land_turn(
                    store=self._store, session_factory=self._session_factory,
                    sandbox_pool=self._sandbox_pool, session=session,
                    saga_settings=self._saga_settings,
                    tool_saga_id=tool_saga.saga_id if tool_saga is not None else None,
                    after_event_id=self._turn_after_event_id, redis=self._redis,
                )
            except Exception:
                logger.exception("Landing failed for %s", session.id)
                # The master hears it: the report names the turn's files as not saved.
                landing = {"state": "failed", "files": [], "excluded": [], "repositories": []}

        not_kept: list[str] = []
        # What a hand-back that failed leaves: its completion's mark, and the files kept apart.
        unkept: dict[str, Any] = {}
        if (
            session.config.get("history_thread") and self._sandbox_pool is not None
            and self._sandbox_pool.holds_copy(sandbox_session_key(session))
        ):
            # A thread's helper hands its copy back onto the thread's hand-off: it lands with the thread.
            try:
                kept = await keep_copy(
                    session_factory=self._session_factory, sandbox_pool=self._sandbox_pool,
                    session=session, saga_settings=self._saga_settings, action="hand_back", redis=self._redis,
                )
                not_kept = kept["not_kept"] if kept else []
            except Exception:
                logger.exception("Could not hand the copy of %s back to its thread", session.id)
                unkept = await self._kept_apart(session)

        # The turn's tool saga ends with it: a later stop compensates only its own turn.
        if self._turn_saga is not None:
            await self._finalize_sagas(self._turn_saga, session)

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
        # A landing that recorded a commit leaves its pod the day's pruning
        # of the project's history: run at the very end, once the report is out.
        prunes: str | None = None
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
                if sandbox_id is not None and landing is not None and landing["state"] == "completed" and landing.get("commit"):
                    prunes = sandbox_id
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
        # Not gated on the summarizer: deciding what was delivered is
        # bookkeeping now, so the download card survives with recaps
        # turned off. Only the recap itself needs a model.
        # The turn's files, for a report: None when no summary was written.
        files: list[dict[str, Any]] | None = None
        if wants_turn_summary(session, turn_id=turn_id, reason=reason):
            try:
                files = await self._drain_and_emit_turn_summary(
                    session_id=session.id,
                    turn_id=turn_id,
                    user_message=user_message,
                    # The closing message is the only place a delivery
                    # claim with nothing behind it can be seen.
                    final_message=_last_assistant_message_excerpt(messages),
                )
            except Exception:
                logger.exception(
                    "Turn summary drain failed for %s", session.id,
                )

        if landing is not None:
            # A thread's files are its landing's; artifacts still come from its summary.
            landed = landing["files"]
            if not landed and landing["state"] != "completed":
                # A landing that never knew its files: the turn's own list names them, none landed.
                landed = [{**f, "landing": "not_merged"} for f in files or [] if f.get("kind") == "file"]
            files = landed + [a for a in files or [] if a.get("kind") != "file"]
        elif is_project_thread(session.config) and (alone := await redo_files(self._store, session.id)):
            # A redo turn that never used its pod left the files it was woken
            # for as they are: not merged, and nothing waits on you.
            files = [f for f in files or [] if f.get("ref") not in alone] + [left_alone(p) for p in sorted(alone)]

        if not_kept:
            # Another helper, or the thread, changed them first: theirs stays.
            files = [f for f in files or [] if f.get("ref") not in not_kept] + [
                {"kind": "file", "label": path, "ref": path, "landing": "not_merged", "reason": "kept"} for path in not_kept
            ]

        complete_data: dict[str, Any] = {
            "reason": reason,
            "worker_id": self._worker_id,
            **({"not_kept": not_kept} if not_kept else {}),
            # A helper whose hand-back failed: its work is on no hand-off, and its thread is told.
            **unkept,
        }
        if cost_tracker is not None:
            complete_data["cost_summary"] = cost_tracker.summary()
        if landing is not None:
            # Whether this turn end saved the thread's work in the history: a
            # later turn on a copy made afresh lost nothing before it.  A
            # landing that failed before its commit step, or held a file,
            # leaves work the next copy lacks.  A turn that never used its
            # pod is no such turn end.
            complete_data["saved"] = bool(landing.get("saved"))

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
            loop_result_parent = await resolve_loop_result_parent(self._store, session)
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
                    files=files,
                    landing=landing,
                    unkept=unkept,
                )
            except Exception:
                logger.warning(
                    "Failed to notify parent %s of worker %s completion",
                    session.parent_id, session.id,
                    exc_info=True,
                )

        await self._finalize_dynamic_loop_if_needed(session)

        if landing is not None and landing.get("redo"):
            from surogates.harness.worker_notify import notify_parent_of_task_event

            # After the turn's end, so that its next turn reads it, and queued,
            # as a report queues its master: the thread redoes those files.
            await notify_parent_of_task_event(
                session_store=self._store, parent_session_id=session.id, event_type=EventType.HISTORY_REDO,
                payload={"saga": landing["saga"], "files": landing["redo"]}, redis=self._redis,
            )

        if landing is not None and landing["state"] == "completed":
            # A file it waited on you over has landed since: that wait is over.
            landed = {f["ref"] for f in landing["files"] if f.get("landing") == "landed"}
            if landed:
                await self._store.land_file_waits(session.id, landed)

        if landing is not None and (landing.get("stuck") or landing["state"] == "escalated"):
            # It waits on you: a file left out again after its redo, or a
            # landing it could not put back whole.
            escalated = landing["state"] == "escalated"
            paths = [f["ref"] for f in landing["files"] if f.get("kind") == "file"] if escalated else landing["stuck"]
            await self._store.emit_event(
                session.id, EventType.INBOX_ACTION_REQUIRED, waiting_on_you(paths, escalated=escalated),
            )

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

        if prunes is not None:
            from surogates.storage.tenant import boundary_workspace_prefix

            # Last, with the turn ended and reported, and outside the wake: its lease goes without
            # waiting, so the thread's next message is not held for a pruning.  Its pod goes after it.
            prune_later(
                session_factory=self._session_factory, sandbox_pool=self._sandbox_pool, sandbox_id=prunes,
                session_id=str(session.id), workstream=session.config["workstream_id"],
                packs=landing.get("packs", 0), saga_settings=self._saga_settings,
                # The bucket itself says which packs are old: no pod's clock, and not the worker's.
                storage=self._storage, bucket=session.config.get("storage_bucket"),
                prefix=boundary_workspace_prefix(session.config, session, session.id), redis=self._redis,
            )

    async def _kept_apart(self, session: Any) -> dict[str, Any]:
        """A helper's copy whose hand-back failed, kept apart before its pod goes; what its completion says of it.

        Nothing when the helper changed no file.  Else ``kept`` false, and
        ``left``, the files kept apart, as a failed helper's are; no
        ``left`` when the copy could not be kept apart either.
        """
        try:
            apart = await keep_copy(
                session_factory=self._session_factory, sandbox_pool=self._sandbox_pool,
                session=session, saga_settings=self._saga_settings, action="keep_apart", settle=False,
            )
        except Exception:
            logger.exception("Could not keep the copy of %s apart", session.id)
            return {"kept": False}
        left = apart.get("left", []) if apart else []
        return {"kept": False, "left": left} if left else {}

    async def _open_copy_to_land(self, session: Session) -> None:
        """Give a thread's turn that never used its pod one, when its end lands all the same.

        Never for a thread on the user's computer: it works in the folder there,
        and has no copy in the cloud to land.
        """
        owner = sandbox_session_key(session)
        if device_of(session.config) is not None:
            return
        if self._sandbox_pool.holds_copy(owner) or self._storage is None or session.config.get("history_off"):
            return
        if await waits_to_land(self._session_factory, self._storage, session, fence=_fence(self._saga_settings)):
            from surogates.harness.tool_exec import _build_session_sandbox_spec

            spec = await _build_session_sandbox_spec(session, self._tenant, owner, credential_vault=self._credential_vault)
            await self._sandbox_pool.ensure(owner, spec)

    async def _fail_session(
        self,
        session: Session,
        messages: list[dict],
        lease: SessionLease,
        *,
        reason: str,
        cost_tracker: SessionCostTracker | None = None,
        **data: Any,
    ) -> None:
        """Emit SESSION_FAIL and run the same teardown as a completion.

        A failure that returns from the loop without this leaves the
        session looking alive to everything downstream: the parent that
        delegated it never wakes, a scheduled run never reports, the
        dynamic loop is never rescheduled, and the failed prompt stays past
        the cursor where the stranded-message check resurrects it.
        """
        if self._turn_saga is not None:
            await self._finalize_sagas(self._turn_saga, session)

        # A failed turn does not land, since its files may be half made, but
        # its copy is kept on its branch, and lands with its next turn.  A
        # failed helper's is kept apart, merged onto nothing, and its thread
        # told.  Then its pod goes: the next turn takes the work up from the history.
        saved: bool | None = None
        left: list[str] = []
        # The helpers' files the turn's take-ups left as its copy had them: named here, as a landing names them.
        not_taken: list[str] = []
        owner = sandbox_session_key(session)
        # The turn is over, kept or not: what it handed on is no later Stop's to drop.
        turn_ended(session)
        if self._sandbox_pool is not None and self._sandbox_pool.holds_copy(owner):
            helper = bool(session.config.get("history_thread"))
            try:
                kept = await keep_copy(
                    session_factory=self._session_factory, sandbox_pool=self._sandbox_pool,
                    session=session, saga_settings=self._saga_settings, action="keep_apart" if helper else "keep",
                    redis=self._redis,
                )
                saved = kept is not None
                left = kept.get("left", []) if kept else []
                not_taken = kept.get("not_taken", []) if kept else []
            except Exception:
                logger.exception("Could not keep the copy of %s", session.id)
                saved = False
            try:
                sandbox_id = await self._sandbox_pool.release_for_session(owner)
            except Exception:
                # The turn's end is still written: a pod left behind goes at its deadline.
                logger.warning("Could not let the pod of %s go", session.id, exc_info=True)
            else:
                self._spawn_background(
                    self._destroy_sandbox_quietly(sandbox_id, str(session.id)), name=f"sandbox-teardown-{session.id}",
                )

        fail_data: dict[str, Any] = {
            "reason": reason, "worker_id": self._worker_id, **data,
            # Whether the keep saved the turn's work, as a completed turn's landing does.
            **({"saved": saved} if saved is not None else {}),
            # A failed helper's changes, kept apart in the history: never in its thread's copy.
            **({"left": left} if left else {}),
            **({"not_taken": not_taken} if not_taken else {}),
        }
        if cost_tracker is not None:
            fail_data["cost_summary"] = cost_tracker.summary()
        # A project's failed turn spent what it spent; its holds settle as a
        # completed turn's do, rather than waiting for a reaper or a refill.
        # Only a project's session holds its next turn again at its wake; any
        # other session's retry, or a message typed meanwhile, settles them.
        if admitted_at_wake(session):
            await asyncio.gather(
                self._settle_commerce_reservation(session, cost_tracker),
                self._settle_allowance_reservation(session, cost_tracker),
            )
        fail_event_id = await self._store.emit_event(
            session.id, EventType.SESSION_FAIL, fail_data,
        )
        error = f"{reason}: {data}" if data else reason
        if left:
            error += f". Its changes to {', '.join(left)} were kept apart, not brought into the thread's copy"
        elif saved and is_project_thread(session.config):
            # Said as a landing that did not finish says it: nothing of the turn is lost.
            error += ". Its work is kept, and lands with the thread's next turn"
        await announce_failure(
            self._store, session, error=error, summary=_last_assistant_message_excerpt(messages),
        )

        try:
            await self._store.update_session_status(session.id, "failed")
        except Exception:
            logger.warning(
                "Failed to update session status to failed for %s",
                session.id, exc_info=True,
            )

        if _should_notify_parent_on_completion(session):
            from surogates.harness.worker_notify import notify_parent_on_failure
            try:
                await notify_parent_on_failure(
                    session_store=self._store,
                    worker_session_id=session.id,
                    parent_session_id=session.parent_id,
                    org_id=str(session.org_id),
                    agent_id=session.agent_id,
                    error=error,
                    redis=self._redis,
                    task_id=getattr(session, "task_id", None),
                    session_factory=self._session_factory,
                    not_taken=not_taken,
                )
            except Exception:
                logger.warning(
                    "Failed to notify parent %s of worker %s failure",
                    session.parent_id, session.id, exc_info=True,
                )

        await self._finalize_dynamic_loop_if_needed(session)

        try:
            await self._store.advance_harness_cursor(
                session.id, fail_event_id, lease.lease_token,
            )
        except Exception:
            logger.warning(
                "Failed to advance cursor after session failure for %s", session.id,
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
