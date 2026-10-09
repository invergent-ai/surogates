"""Outcome and mission slash-command helpers for AgentHarness."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from surogates.harness.loop_mission_evaluator import (
    _build_mission_judge,
    _maybe_run_mission_evaluator,
)
from surogates.harness.outcomes import (
    DEFAULT_MAX_ITERATIONS,
    OutcomeState,
    apply_evaluation,
    build_evaluator_messages,
    parse_goal_command,
    parse_outcome_evaluation,
    start_outcome,
)
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


#: The mission commands that are refused once done, with the event each writes and its answer.
_MISSION_DONE = {
    "pause": (EventType.MISSION_PAUSED, "Mission paused."),
    "resume": (EventType.MISSION_RESUMED, "Mission resumed."),
    "cancel": (EventType.MISSION_CANCELLED, "Mission cancelled."),
}


def _goal_defined(state: OutcomeState) -> str:
    """The answer to the ``/goal <text>`` that set *state*."""
    return f"Outcome defined ({state.max_iterations} iterations): {state.description}"


class OutcomeCommandMixin:
    def _outcome_settings(self) -> Any:
        try:
            from surogates.config import load_settings

            return load_settings().outcomes
        except Exception:
            logger.debug("Failed to load outcome settings", exc_info=True)
            return SimpleNamespace(
                max_iterations=DEFAULT_MAX_ITERATIONS,
                max_parse_failures=3,
            )

    async def _session_has_active_mission(self, session_id: UUID) -> bool:
        """True iff the session has an active or paused mission row.

        Used by ``_handle_goal_command`` to enforce mutual exclusion: only
        one evaluator loop per session is allowed, so a /mission already
        in flight blocks /goal creation (and vice versa).
        """
        if self._session_factory is None:
            return False
        try:
            from surogates.missions.store import MissionStore

            store = MissionStore(self._session_factory)
            return (await store.get_active_for_session(session_id)) is not None
        except Exception:
            logger.debug(
                "Mission active-check failed for session %s; treating as no mission",
                session_id, exc_info=True,
            )
            return False

    async def _mission_has_pending_work(self, session: Session, *, or_raise: bool = False) -> bool:
        """True iff the session's mission is in a non-terminal status.

        The session is owned by the mission's lifecycle: while the
        mission is ``active`` or ``paused`` it can still produce work
        (more tasks to spawn, an evaluator retry after a parse failure,
        a ``/mission resume`` after a manual pause). Completing the
        session here would set ``status=completed``, every subsequent
        wake would bail at the status guard in ``process_wake_cycle``,
        and the mission could never progress.

        Only when the mission reaches a terminal status (``satisfied``,
        ``blocked``, ``failed``, ``cancelled``, ``max_iterations_reached``)
        does ``apply_verdict`` clear ``active_mission_id`` from the
        session config; ``get_active_for_session`` then returns ``None``,
        this method returns ``False``, and the session completes
        normally on the next no-tool-call response.

        Returns ``False`` (allow completion) on any failure path so a
        bug in the mission layer can't strand sessions forever.  With
        *or_raise* the failure is raised instead: a command's end is
        written again by the wake that is retried, so there not knowing
        must not be taken for "no mission".
        """
        if self._session_factory is None:
            return False
        # ``active_mission_id`` is cleared from the config at a terminal
        # verdict, so a session without it has no mission to query for.
        if not (session.config or {}).get("active_mission_id"):
            return False
        try:
            from surogates.missions.store import MissionStore

            store = MissionStore(self._session_factory)
            active = await store.get_active_for_session(session.id)
            return active is not None
        except Exception:
            if or_raise:
                raise
            logger.debug(
                "Mission pending-work check failed for session %s; "
                "falling back to completing session",
                session.id, exc_info=True,
            )
            return False

    async def _mission_is_paused(self, session: Session) -> bool:
        """True iff the session's mission is paused."""
        if self._session_factory is None or not (session.config or {}).get("active_mission_id"):
            return False
        from surogates.missions.store import MissionStore

        mission = await MissionStore(self._session_factory).get_active_for_session(session.id)
        return mission is not None and mission.status == "paused"

    async def _mission_may_be_paused(self, session: Session) -> bool:
        """``_mission_is_paused``, and True where the mission cannot be
        read: not knowing is not "no"."""
        try:
            return await self._mission_is_paused(session)
        except Exception:
            logger.debug("Session %s: its mission could not be read", session.id, exc_info=True)
            return True

    async def _queue_behind_a_pause(self, session: Session, result: Any) -> None:
        """Queue a wake once a command has paused the session's mission:
        the sweeper spares a paused mission's chat, so nothing else would
        answer the command if this worker died before it has."""
        if result is None or not result.ok or self._redis is None:
            return
        from surogates.config import enqueue_session

        await enqueue_session(
            self._redis, org_id=str(session.org_id), agent_id=session.agent_id, session_id=session.id,
        )

    async def _handle_mission_command(
        self,
        session: Session,
        content: str,
        lease: SessionLease,
        *,
        typed_on: datetime | None = None,
    ) -> None:
        """Dispatch ``/mission ...`` to the matching handler.

        Mirrors :meth:`_handle_goal_command`: parses args, calls into
        :mod:`surogates.missions.commands`, then emits an LLM_RESPONSE
        carrying the operator-visible message.  *typed_on* is when the
        user sent the command: a mission this session started since then
        for the same words is the one a worker made for this very command
        before it died, and is not refused as a second one.
        """
        from surogates.missions.commands import (
            MissionCommandParseError,
            MissionHandlerResult,
            handle_mission_accept,
            handle_mission_cancel,
            handle_mission_create,
            handle_mission_pause,
            handle_mission_reject,
            handle_mission_resume,
            handle_mission_budget,
            handle_mission_status,
            parse_mission_command,
        )
        from surogates.missions.store import MissionStore

        # ``result`` is the inner handler's return when a branch invokes
        # one; the post-cursor kickoff emit reads ``result.kickoff_content``.
        # Branches that short-circuit on a precondition (missing Redis,
        # missing principal, unparseable command, invalid action) leave
        # this None and skip the kickoff emit.
        result: MissionHandlerResult | None = None

        args = content[len("/mission"):].strip()
        try:
            command = parse_mission_command(args)
        except MissionCommandParseError as exc:
            message = f"/mission parse error: {exc}"
        else:
            if self._session_factory is None:
                message = (
                    "/mission requires a configured session factory; "
                    "this looks like a harness initialization bug."
                )
            else:
                mission_store = MissionStore(self._session_factory)
                redis_client = self._redis
                if command.action == "create":
                    principal_user_id = self._acting_principal.user_id
                    principal_sa_id = self._acting_principal.service_account_id
                    if redis_client is None:
                        message = (
                            "/mission create cannot run without a Redis "
                            "connection (the coordinator must be enqueued "
                            "after kickoff)."
                        )
                    elif principal_user_id is None and principal_sa_id is None:
                        # Anonymous-channel sessions have neither a user nor
                        # a service-account principal — the session itself is
                        # the principal.  Missions need a durable owner that
                        # outlives the session, so reject these explicitly.
                        message = (
                            "/mission requires a user or service-account "
                            "session — anonymous channel sessions cannot "
                            "own missions."
                        )
                    else:
                        result = await handle_mission_create(
                            description=command.description or "",
                            rubric=command.rubric or "",
                            session_id=session.id,
                            user_id=principal_user_id,
                            service_account_id=principal_sa_id,
                            org_id=self._tenant.org_id,
                            agent_id=session.agent_id,
                            session_store=self._store,
                            session_factory=self._session_factory,
                            mission_store=mission_store,
                            budget_tokens=command.budget_tokens,
                            typed_on=typed_on,
                        )
                        message = result.message or result.error
                        if result.ok and result.mission_id is not None:
                            # Propagate the config write back to the
                            # in-memory session so the rest of this wake
                            # sees ``coordinator=True`` + the orchestrator
                            # skill preload. Without this, the kickoff
                            # message gets processed by the same wake
                            # against a stale config and tools gated on
                            # ``coordinator`` (``spawn_task`` &c) get
                            # filtered out as worker-excluded.
                            cfg = dict(session.config or {})
                            cfg["active_mission_id"] = str(result.mission_id)
                            cfg["coordinator"] = True
                            # Strip implementation tools so the LLM has to
                            # delegate via spawn_task/delegate_task instead of
                            # "fixing it quickly" itself.  See
                            # COORDINATOR_IMPLEMENTATION_TOOLS for the set.
                            cfg["strict_coordinator"] = True
                            preloaded = list(cfg.get("preloaded_skills") or [])
                            if "subagent-task-orchestrator" not in preloaded:
                                preloaded.append("subagent-task-orchestrator")
                            cfg["preloaded_skills"] = preloaded
                            session.config = cfg
                elif command.action == "budget":
                    message = await handle_mission_budget(
                        session_id=session.id,
                        budget_tokens=command.budget_tokens,
                        mission_store=mission_store,
                    )
                elif command.action == "status":
                    result = await handle_mission_status(
                        session_id=session.id, mission_store=mission_store,
                    )
                    message = result.message
                elif command.action == "pause":
                    result = await handle_mission_pause(
                        session_id=session.id,
                        reason=command.reason,
                        session_store=self._store,
                        mission_store=mission_store,
                    )
                    await self._queue_behind_a_pause(session, result)
                    message = result.message or result.error
                elif command.action == "resume":
                    if redis_client is None:
                        message = (
                            "/mission resume cannot wake the coordinator "
                            "without a Redis connection."
                        )
                    else:
                        result = await handle_mission_resume(
                            session_id=session.id,
                            org_id=str(session.org_id),
                            agent_id=session.agent_id,
                            session_store=self._store,
                            mission_store=mission_store,
                            redis=redis_client,
                        )
                        message = result.message or result.error
                elif command.action == "cancel":
                    if redis_client is None:
                        message = (
                            "/mission cancel cannot cascade interrupts "
                            "without a Redis connection."
                        )
                    else:
                        result = await handle_mission_cancel(
                            session_id=session.id,
                            reason=command.reason,
                            cascade_to_workers=command.cascade_to_workers,
                            session_store=self._store,
                            session_factory=self._session_factory,
                            mission_store=mission_store,
                            redis=redis_client,
                        )
                        message = result.message or result.error
                        if result.ok:
                            # Mirror the DB ``clear_session_config_key``
                            # call in the in-memory session so subsequent
                            # iterations of this wake (and the next /goal
                            # mutual-exclusion check) see no active
                            # mission.
                            cfg = dict(session.config or {})
                            cfg.pop("active_mission_id", None)
                            session.config = cfg
                elif command.action == "accept":
                    if redis_client is None:
                        # The amendment commits before the deferred
                        # continuation is emitted, and that emit is what
                        # enqueues the coordinator. Without Redis the
                        # mission would go active carrying a continuation
                        # nothing wakes to process, and accept could not be
                        # retried — amend_rubric has already cleared the
                        # paused_reason the handler authorizes on.
                        message = (
                            "/mission accept cannot wake the coordinator "
                            "without a Redis connection."
                        )
                    else:
                        result = await handle_mission_accept(
                            session_id=session.id,
                            session_store=self._store,
                            mission_store=mission_store,
                        )
                        message = result.message or result.error
                elif command.action == "reject":
                    result = await handle_mission_reject(
                        session_id=session.id,
                        session_store=self._store,
                        mission_store=mission_store,
                        reason=command.reason,
                    )
                    message = result.message or result.error
                    if result.ok:
                        # Mirror the DB clear_session_config_key call in the
                        # in-memory session, the way the cancel branch does.
                        cfg = dict(session.config or {})
                        cfg.pop("active_mission_id", None)
                        session.config = cfg
                else:
                    message = (
                        "Usage: /mission <description>\\n\\nRubric:\\n<criterion>"
                        " | /mission status | /mission pause [reason]"
                        " | /mission resume | /mission cancel [--cascade] [reason]"
                        " | /mission accept | /mission reject [reason]"
                    )
                message = await self._done_by_this_message(session, command.action, result) or message

        # /mission create writes its synthetic kickoff message before its
        # answer, as /goal does: an answered command is not run again, so an
        # answer with no kickoff after it would leave the mission started
        # and never worked on.
        kicks_off = result is not None and result.ok and result.kickoff_content is not None
        if kicks_off:
            await self._kick_off(session, result)

        # The cursor is the turn's end's to move (``_end_command_turn``): it
        # must not pass what the user sent while this was being answered.
        await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": message}, **self._names_its_message()},
            lease_token=lease.lease_token,
        )

        if kicks_off:
            if redis_client is not None:
                try:
                    from surogates.config import enqueue_session

                    await enqueue_session(
                        redis_client,
                        org_id=str(session.org_id),
                        agent_id=session.agent_id,
                        session_id=session.id,
                    )
                except Exception:
                    logger.debug(
                        "Failed to enqueue mission kickoff", exc_info=True,
                    )

    async def _done_by_this_message(self, session: Session, action: str, result: Any) -> str | None:
        """The answer to a mission command that is refused now because a
        run of this very command did it before its worker died: what that
        run would have said.  None when the command is not such a one.

        The mission is found as the command left it, and the event the
        first run wrote since the command was typed says whose doing it
        is."""
        answering = getattr(self, "_answering", None)
        if answering is None or result is None or result.ok or action not in _MISSION_DONE:
            return None
        done, answer = _MISSION_DONE[action]
        return answer if await self._store.has_event(session.id, done, after=answering) else None

    async def _kick_off(self, session: Session, result: Any) -> None:
        """Write the message that starts the work a mission command made,
        unless a run of the command wrote it before its worker died."""
        if await self._said_since_this_message(
            session,
            lambda data: (data.get("synthetic"), data.get("content")) == (result.kickoff_synthetic, result.kickoff_content),
        ):
            return
        await self._store.emit_event(
            session.id, EventType.USER_MESSAGE,
            {
                "content": result.kickoff_content,
                "synthetic": result.kickoff_synthetic,
            },
        )

    async def _handle_auto_research_command(
        self,
        session: Session,
        content: str,
        lease: SessionLease,
        *,
        typed_on: datetime | None = None,
    ) -> None:
        """Dispatch ``/auto-research ...`` — an alias of /mission that
        creates a research-kind mission.

        Create routes to :func:`handle_research_mission_create`; control
        verbs (status/pause/resume/cancel) reuse the standard mission
        handlers (a research mission IS a mission). The kickoff-after-cursor
        and LLM_RESPONSE emit follow the same contract as /mission.
        """
        from surogates.db.models import Session as ORMSession
        from surogates.missions.commands import (
            MissionCommandParseError,
            MissionHandlerResult,
            handle_mission_budget,
            handle_mission_cancel,
            handle_mission_pause,
            handle_mission_resume,
            handle_mission_status,
            handle_research_mission_create,
            parse_auto_research_command,
        )
        from surogates.missions.store import MissionStore

        result: MissionHandlerResult | None = None
        args = content[len("/auto-research"):].strip()
        try:
            command = parse_auto_research_command(args)
        except MissionCommandParseError as exc:
            message = f"/auto-research parse error: {exc}"
        else:
            if self._session_factory is None:
                message = (
                    "/auto-research requires a configured session factory; "
                    "this looks like a harness initialization bug."
                )
            else:
                mission_store = MissionStore(self._session_factory)
                redis_client = self._redis
                if command.action == "create":
                    principal_user_id = self._acting_principal.user_id
                    principal_sa_id = self._acting_principal.service_account_id
                    if redis_client is None:
                        message = (
                            "/auto-research cannot run without a Redis "
                            "connection (the coordinator must be enqueued "
                            "after kickoff)."
                        )
                    elif principal_user_id is None and principal_sa_id is None:
                        message = (
                            "/auto-research requires a user or service-account "
                            "session — anonymous channel sessions cannot own "
                            "research missions."
                        )
                    else:
                        result = await handle_research_mission_create(
                            cmd=command,
                            session_id=session.id,
                            user_id=principal_user_id,
                            service_account_id=principal_sa_id,
                            org_id=self._tenant.org_id,
                            agent_id=session.agent_id,
                            session_store=self._store,
                            session_factory=self._session_factory,
                            mission_store=mission_store,
                            typed_on=typed_on,
                        )
                        message = result.message or result.error
                        if result.ok and result.mission_id is not None:
                            # The research handler wrote several config keys
                            # (active_mission_id, coordinator, strict_coordinator,
                            # active_research_run_id, arbor-coordinator preload).
                            # Re-read the row so this wake sees the full set and
                            # the kickoff is processed against fresh config.
                            async with self._session_factory() as db:
                                row = await db.get(ORMSession, session.id)
                                if row is not None:
                                    session.config = dict(row.config or {})
                elif command.action == "budget":
                    message = await handle_mission_budget(
                        session_id=session.id,
                        budget_tokens=command.budget_tokens,
                        mission_store=mission_store,
                    )
                elif command.action == "status":
                    result = await handle_mission_status(
                        session_id=session.id, mission_store=mission_store,
                    )
                    message = result.message
                elif command.action == "pause":
                    result = await handle_mission_pause(
                        session_id=session.id, reason=command.reason,
                        session_store=self._store, mission_store=mission_store,
                    )
                    await self._queue_behind_a_pause(session, result)
                    message = result.message or result.error
                elif command.action == "resume":
                    if redis_client is None:
                        message = (
                            "/auto-research resume cannot wake the coordinator "
                            "without a Redis connection."
                        )
                    else:
                        result = await handle_mission_resume(
                            session_id=session.id,
                            org_id=str(session.org_id),
                            agent_id=session.agent_id,
                            session_store=self._store,
                            mission_store=mission_store,
                            redis=redis_client,
                        )
                        message = result.message or result.error
                elif command.action == "cancel":
                    if redis_client is None:
                        message = (
                            "/auto-research cancel cannot cascade interrupts "
                            "without a Redis connection."
                        )
                    else:
                        result = await handle_mission_cancel(
                            session_id=session.id, reason=command.reason,
                            cascade_to_workers=command.cascade_to_workers,
                            session_store=self._store,
                            session_factory=self._session_factory,
                            mission_store=mission_store, redis=redis_client,
                        )
                        message = result.message or result.error
                        if result.ok:
                            cfg = dict(session.config or {})
                            cfg.pop("active_mission_id", None)
                            session.config = cfg
                else:
                    message = (
                        "Usage: /auto-research repo=</workspace/...> "
                        "[max_iterations=N] [baseline=<dev>] "
                        "[baseline_test=<test>] <objective>\\n\\nRubric:\\n"
                        "<criterion> | /auto-research status | pause | "
                        "resume | cancel [--cascade]"
                    )
                message = await self._done_by_this_message(session, command.action, result) or message

        # The synthetic kickoff before the answer, as /mission and /goal.
        kicks_off = result is not None and result.ok and result.kickoff_content is not None
        if kicks_off:
            await self._kick_off(session, result)

        # The cursor is the turn's end's to move (``_end_command_turn``): it
        # must not pass what the user sent while this was being answered.
        await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": message}, **self._names_its_message()},
            lease_token=lease.lease_token,
        )

        if kicks_off:
            if redis_client is not None:
                try:
                    from surogates.config import enqueue_session

                    await enqueue_session(
                        redis_client,
                        org_id=str(session.org_id),
                        agent_id=session.agent_id,
                        session_id=session.id,
                    )
                except Exception:
                    logger.debug(
                        "Failed to enqueue research mission kickoff",
                        exc_info=True,
                    )

    async def _handle_goal_command(
        self,
        session: Session,
        content: str,
        lease: SessionLease,
    ) -> None:
        args = content[len("/goal") :].strip()
        command = parse_goal_command(args)
        current = OutcomeState.from_config((session.config or {}).get("outcome"))

        outcome_kickoff_needed = False

        if command.action == "status":
            message = self._format_outcome_status(current)
        elif command.action == "set":
            if await self._goal_is_this_messages(session, current, command):
                # Its worker died once it had set the goal: the goal is
                # started, and answered for, as that run would have.
                message = _goal_defined(current)
                outcome_kickoff_needed = True
            # Reject setting a new outcome while one is active — a continuation
            # kickoff for the prior outcome may be pending in the event log,
            # and overwriting session.config["outcome"] would orphan it.
            elif current is not None and current.status == "active":
                message = (
                    f"Outcome already active ({current.iteration}/"
                    f"{current.max_iterations}): {current.description}. "
                    "Use /goal pause or /goal clear before setting a new outcome."
                )
            elif await self._session_has_active_mission(session.id):
                # Mutual exclusion: only one evaluator loop per session.
                # /mission already runs an evaluator — adding a /goal would
                # produce two competing judges on the same chat.
                message = (
                    "This session has an active /mission. Cancel or pause it "
                    "before setting a /goal (only one evaluator loop per "
                    "session is allowed)."
                )
            else:
                message = await self._define_goal_outcome(session, command)
                outcome_kickoff_needed = True
        elif command.action == "pause":
            message = await self._pause_goal_outcome(session, current)
        elif command.action == "resume":
            message = await self._resume_goal_outcome(session, current)
        elif command.action == "clear":
            message = await self._clear_goal_outcome(session, current)
        else:
            message = "Usage: /goal <outcome>, /goal status, /goal pause, /goal resume, /goal clear."

        # The goal's first message before the answer: an answered command is
        # not run again, so an answer with no first message after it would
        # leave the goal set and never worked on.  Replay shows the message
        # after the answer all the same.
        outcome = OutcomeState.from_config((session.config or {}).get("outcome")) if outcome_kickoff_needed else None
        if outcome is not None and not await self._said_since_this_message(
            session, lambda data: data.get("synthetic") == "outcome_kickoff" and data.get("outcome_id") == outcome.id,
        ):
            kickoff_id = await self._store.emit_synthetic_user_message(
                session.id,
                content=outcome.description,
                synthetic="outcome_kickoff",
                metadata={"outcome_id": outcome.id},
            )
            logger.debug(
                "Session %s: emitted outcome kickoff user message %s",
                session.id,
                kickoff_id,
            )

        # The cursor is the turn's end's to move (``_end_command_turn``): it
        # must not pass what the user sent while this was being answered.
        await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": message}, **self._names_its_message()},
            lease_token=lease.lease_token,
        )

        if outcome is not None:
            if self._redis is not None:
                try:
                    from surogates.config import enqueue_session

                    await enqueue_session(
                        self._redis,
                        org_id=str(session.org_id),
                        agent_id=session.agent_id,
                        session_id=session.id,
                    )
                except Exception:
                    logger.debug("Failed to enqueue outcome kickoff", exc_info=True)

    async def _said_since_this_message(self, session: Session, is_it: Any) -> bool:
        """Whether a message the harness wrote since the command it answers
        now was typed is the one *is_it* tells by its data: the one a run
        of this command wrote before its worker died."""
        answering = getattr(self, "_answering", None)
        if answering is None:
            return False
        return any(
            is_it(event.data or {})
            for event in await self._store.get_events(session.id, after=answering, types=[EventType.USER_MESSAGE])
        )

    async def _goal_is_this_messages(self, session: Session, current: OutcomeState | None, command: Any) -> bool:
        """Whether the session's goal in flight is the one a run of this
        very command set before its worker died: it is recorded as defined
        since the command was typed, with the command's words."""
        answering = getattr(self, "_answering", None)
        if answering is None or current is None or current.status != "active" or current.description != (command.text or "").strip():
            return False
        return any(
            (event.data or {}).get("outcome_id") == current.id
            for event in await self._store.get_events(session.id, after=answering, types=[EventType.OUTCOME_DEFINED])
        )

    async def _define_goal_outcome(self, session: Session, command: Any) -> str:
        settings = self._outcome_settings()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            state = start_outcome(
                command.text,
                rubric=command.rubric,
                max_iterations=getattr(
                    settings,
                    "max_iterations",
                    DEFAULT_MAX_ITERATIONS,
                ),
                now_iso=now_iso,
            )
        except ValueError:
            return "Usage: /goal <outcome>. Example: /goal Fix all failing tests."

        # Recorded before it is set: a goal that is set and not recorded is
        # one a second run of the command could not tell for its own.
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_DEFINED,
            {
                "outcome_id": state.id,
                "description": state.description,
                "rubric": state.rubric,
                "max_iterations": state.max_iterations,
            },
        )
        await self._store.update_session_config_key(
            session.id,
            "outcome",
            state.to_config(),
        )
        session.config = {**(session.config or {}), "outcome": state.to_config()}
        return _goal_defined(state)

    async def _pause_goal_outcome(
        self,
        session: Session,
        current: OutcomeState | None,
    ) -> str:
        if current is None or current.status not in {"active", "paused"}:
            return "No active outcome. Set one with /goal <text>."
        current.status = "paused"
        current.paused_reason = "user-paused"
        current.updated_at = datetime.now(timezone.utc).isoformat()
        await self._store.update_session_config_key(
            session.id,
            "outcome",
            current.to_config(),
        )
        session.config = {**(session.config or {}), "outcome": current.to_config()}
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_PAUSED,
            {"outcome_id": current.id, "reason": current.paused_reason},
        )
        return f"Outcome paused: {current.description}"

    async def _resume_goal_outcome(
        self,
        session: Session,
        current: OutcomeState | None,
    ) -> str:
        if current is None or current.status not in {"paused", "max_iterations_reached"}:
            return "No paused outcome to resume."
        current.status = "active"
        current.paused_reason = None
        current.updated_at = datetime.now(timezone.utc).isoformat()
        await self._store.update_session_config_key(
            session.id,
            "outcome",
            current.to_config(),
        )
        session.config = {**(session.config or {}), "outcome": current.to_config()}
        return f"Outcome resumed: {current.description}"

    async def _clear_goal_outcome(
        self,
        session: Session,
        current: OutcomeState | None,
    ) -> str:
        await self._store.clear_session_config_key(session.id, "outcome")
        session.config = {**(session.config or {})}
        session.config.pop("outcome", None)
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_CLEARED,
            {"outcome_id": current.id if current else None},
        )
        return "Outcome cleared." if current else "No active outcome."

    def _format_outcome_status(self, state: OutcomeState | None) -> str:
        if state is None:
            return "No active outcome. Set one with /goal <text>."
        lines = [
            (
                f"Outcome ({state.status}, {state.iteration}/"
                f"{state.max_iterations} iterations): {state.description}"
            ),
        ]
        if state.last_explanation:
            lines.append(f"Last evaluation: {state.last_explanation}")
        if state.paused_reason:
            lines.append(f"Paused reason: {state.paused_reason}")
        return "\n".join(lines)

    async def _maybe_run_mission_evaluator_for_session(
        self,
        *,
        session: Session,
        latest_response: str,
        model: str,
    ) -> None:
        """Bind self's session_factory / store / LLM and dispatch to the
        module-level :func:`_maybe_run_mission_evaluator`.

        Kept as an instance method so it can pull the configured eval
        model and LLM client; the actual logic (trigger detection,
        prompt building, verdict handling) lives on the module-level
        helper so tests can drive it with a stubbed judge without
        constructing a full harness.
        """
        if self._session_factory is None:
            return
        from surogates.missions.store import MissionStore

        settings = self._outcome_settings()
        eval_model = getattr(settings, "evaluator_model", "") or model
        judge = _build_mission_judge(
            llm_client=self._llm, eval_model=eval_model,
        )
        await _maybe_run_mission_evaluator(
            session_id=session.id,
            coordinator_last_response=latest_response,
            session_store=self._store,
            session_factory=self._session_factory,
            mission_store=MissionStore(self._session_factory),
            judge=judge,
        )

    async def _evaluate_outcome(
        self,
        *,
        state: OutcomeState,
        latest_response: str,
        model: str,
    ) -> Any:
        settings = self._outcome_settings()
        eval_model = getattr(settings, "evaluator_model", "") or model
        messages = build_evaluator_messages(
            state,
            latest_response,
            response_max_chars=getattr(
                settings, "evaluator_response_max_chars", 16384,
            ),
        )
        try:
            response = await self._llm.chat.completions.create(
                model=eval_model,
                messages=messages,
                temperature=0,
                max_tokens=500,
            )
            raw = self._extract_chat_message_content(response)
        except Exception as exc:
            logger.warning(
                "Outcome evaluator failed for %s: %s",
                state.id,
                exc,
            )
            raw = json.dumps({
                "result": "needs_revision",
                "explanation": f"evaluator error: {type(exc).__name__}",
                "feedback": "Continue working toward the outcome.",
            })
        return parse_outcome_evaluation(raw)

    async def _maybe_continue_outcome(
        self,
        session: Session,
        lease: SessionLease,
        *,
        latest_response: str,
        response_event_id: int,
        model: str,
    ) -> bool:
        state = OutcomeState.from_config((session.config or {}).get("outcome"))
        if state is None or state.status != "active":
            return False

        start_event_id = await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_START,
            {
                "outcome_id": state.id,
                "iteration": state.iteration,
                "response_event_id": response_event_id,
            },
        )
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_ONGOING,
            {"outcome_id": state.id, "iteration": state.iteration},
        )
        evaluation = await self._evaluate_outcome(
            state=state,
            latest_response=latest_response,
            model=model,
        )
        settings = self._outcome_settings()
        decision = apply_evaluation(
            state,
            evaluation,
            now_iso=datetime.now(timezone.utc).isoformat(),
            max_parse_failures=getattr(settings, "max_parse_failures", 3),
        )
        await self._store.update_session_config_key(
            session.id,
            "outcome",
            state.to_config(),
        )
        session.config = {**(session.config or {}), "outcome": state.to_config()}

        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_END,
            {
                "outcome_id": state.id,
                "outcome_evaluation_start_id": start_event_id,
                "iteration": state.iteration,
                "result": decision.result,
                "explanation": evaluation.explanation,
                "feedback": evaluation.feedback,
                "parse_failed": evaluation.parse_failed,
            },
        )

        status_event_id: int | None = None
        if decision.message:
            status_event_id = await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": decision.message}},
            )

        if not decision.should_continue or not decision.continuation_prompt:
            return False

        marker_event_id = await self._store.emit_event(
            session.id,
            EventType.OUTCOME_CONTINUATION,
            {
                "outcome_id": state.id,
                "iteration": state.iteration,
                "status_event_id": status_event_id,
            },
        )
        continuation_event_id = await self._store.emit_synthetic_user_message(
            session.id,
            content=decision.continuation_prompt,
            synthetic="outcome_continuation",
            metadata={"outcome_id": state.id},
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=marker_event_id,
            lease_token=lease.lease_token,
        )
        logger.debug(
            "Session %s: outcome continuation user message %s queued",
            session.id,
            continuation_event_id,
        )
        if self._redis is not None:
            try:
                from surogates.config import enqueue_session

                await enqueue_session(
                    self._redis,
                    org_id=str(session.org_id),
                    agent_id=session.agent_id,
                    session_id=session.id,
                )
            except Exception:
                logger.debug("Failed to enqueue outcome continuation", exc_info=True)
        return True
