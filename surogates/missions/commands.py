"""Slash command parsing + handlers for /mission.

The harness loop calls :func:`parse_mission_command` with the args
substring (everything after ``/mission``). Returns a
:class:`MissionCommand` dataclass that the handlers consume.

Parsing is the only concern of this top section; the DB-writing
handlers (``handle_mission_create``, ``handle_mission_status``,
``handle_mission_pause``, ``handle_mission_resume``,
``handle_mission_cancel``) live below.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from surogates.config import enqueue_session
from surogates.db.models import Session as ORMSession
from surogates.missions.store import (
    ActiveMissionConflictError,
    MissionStore,
)
from surogates.session.events import EventType

logger = logging.getLogger(__name__)


MissionAction = Literal["create", "status", "pause", "resume", "cancel"]


class MissionCommandParseError(ValueError):
    """Raised when /mission args cannot be parsed."""


@dataclass(slots=True)
class MissionCommand:
    """Parsed shape of a /mission invocation."""

    action: MissionAction
    description: str | None = None
    rubric: str | None = None
    reason: str | None = None
    cascade_to_workers: bool = False


@dataclass(slots=True)
class AutoResearchCommand(MissionCommand):
    """Parsed shape of an /auto-research invocation.

    A :class:`MissionCommand` plus the research-specific leading
    ``key=value`` tokens (``repo=`` / ``max_iterations=`` / ``baseline=``
    / ``baseline_test=`` / ``resume=``).
    """

    max_iterations: int | None = None
    resume_run: str | None = None
    repo: str | None = None
    baseline: float | None = None
    baseline_test: float | None = None


_CONTROL_VERBS = ("status", "pause", "resume", "cancel")
_RUBRIC_RE = re.compile(r"\bRubric\s*:", re.IGNORECASE)
_AUTO_RESEARCH_KV_RE = re.compile(
    r"^(max_iterations|resume|repo|baseline|baseline_test)=(\S+)\s*"
)


def parse_mission_command(raw: str) -> MissionCommand:
    """Parse the args portion of a /mission slash command.

    Empty string → status (matches /goal's behaviour).

    A control verb (``status`` / ``pause`` / ``resume`` / ``cancel``)
    optionally followed by a free-form reason → that action with the
    reason captured. ``cancel --cascade [reason]`` sets
    ``cascade_to_workers=True``.

    Anything else is treated as a ``create`` invocation; it MUST contain
    a ``Rubric:`` block (case-insensitive), otherwise the parse fails.
    """
    text = (raw or "").strip()

    if not text:
        return MissionCommand(action="status")

    first_token, _, rest = text.partition(" ")
    verb = first_token.lower()
    if verb in _CONTROL_VERBS:
        rest = rest.strip()
        if verb == "cancel":
            cascade = False
            if rest.startswith("--cascade"):
                cascade = True
                rest = rest[len("--cascade"):].strip()
            return MissionCommand(
                action="cancel",
                reason=rest or None,
                cascade_to_workers=cascade,
            )
        if verb == "pause":
            return MissionCommand(action="pause", reason=rest or None)
        if verb == "resume":
            return MissionCommand(action="resume")
        return MissionCommand(action="status")

    match = _RUBRIC_RE.search(text)
    if match is None:
        raise MissionCommandParseError(
            "missing Rubric: block. Format: "
            "'/mission <description>\\n\\nRubric:\\n<criterion>'"
        )
    description = text[:match.start()].strip()
    rubric = text[match.end():].lstrip(": \n").strip()
    if not description:
        raise MissionCommandParseError("missing description before Rubric: block")
    if not rubric:
        raise MissionCommandParseError("Rubric: block is empty")
    return MissionCommand(
        action="create", description=description, rubric=rubric,
    )


def parse_auto_research_command(raw: str) -> AutoResearchCommand:
    """Parse the args of an /auto-research slash command.

    An alias of /mission: identical control verbs and ``Rubric:``
    contract, preceded by optional leading ``key=value`` tokens
    (``repo=`` / ``max_iterations=`` / ``baseline=`` / ``baseline_test=``
    / ``resume=``). Control verbs and the rubric requirement are
    delegated to :func:`parse_mission_command`.
    """
    text = (raw or "").strip()
    kv: dict[str, str] = {}
    while True:
        match = _AUTO_RESEARCH_KV_RE.match(text)
        if not match:
            break
        kv[match.group(1)] = match.group(2)
        text = text[match.end():]

    def _as_int(key: str) -> int | None:
        if key not in kv:
            return None
        try:
            return int(kv[key])
        except ValueError:
            raise MissionCommandParseError(
                f"{key} must be an integer, got {kv[key]!r}"
            )

    def _as_float(key: str) -> float | None:
        if key not in kv:
            return None
        try:
            return float(kv[key])
        except ValueError:
            raise MissionCommandParseError(
                f"{key} must be a number, got {kv[key]!r}"
            )

    try:
        base = parse_mission_command(text)
    except MissionCommandParseError as exc:
        # parse_mission_command's message names /mission and omits the
        # research-specific repo= token. Reframe it for /auto-research so the
        # operator sees the actual required shape.
        raise MissionCommandParseError(
            "a research run needs a repo and a Rubric. Format:\n"
            "/auto-research repo=/workspace/<repo> [max_iterations=N] "
            "[baseline=<dev>] [baseline_test=<test>] <objective>\n\n"
            "Rubric:\n<criteria>\n"
            "(or a control verb: status | pause | resume | cancel [--cascade])"
        ) from exc
    return AutoResearchCommand(
        action=base.action, description=base.description, rubric=base.rubric,
        reason=base.reason, cascade_to_workers=base.cascade_to_workers,
        max_iterations=_as_int("max_iterations"),
        resume_run=kv.get("resume"),
        repo=kv.get("repo"),
        baseline=_as_float("baseline"),
        baseline_test=_as_float("baseline_test"),
    )


# ---------------------------------------------------------------------------
# Slash command handlers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MissionHandlerResult:
    """Standard return shape for slash handlers.

    ``kickoff_content`` carries the synthetic ``user.message`` body
    that a successful ``handle_mission_create`` wants the caller to
    emit AFTER it has advanced the harness cursor through its own
    slash-reply.  Deferring the kickoff emit to the caller prevents
    the bug where the slash handler's ``advance_harness_cursor``
    races past the kickoff event id, leaving the harness with
    "no actionable pending events" on the next wake.
    """

    ok: bool
    mission_id: UUID | None = None
    message: str = ""
    error: str = ""
    kickoff_content: str | None = None


_KICKOFF_TEMPLATE = """\
[Mission kickoff]

Description: {description}

Rubric:
{rubric}

You are this mission's coordinator. Decompose into specialist sub-agent
tasks via ``spawn_task``; gate dependencies with ``parents=[...]``;
end criterion-driven rounds with a verifier task whose
``result_metadata`` carries the measurable signal the rubric checks.

Do NOT claim completion in prose alone — the evaluator only honours
completion claims backed by verifier-task evidence OR an explicit
``[[mission-complete]]`` marker on its own line in your response.
"""


_RESEARCH_KICKOFF_TEMPLATE = """\
[Research mission kickoff]

Objective: {description}

Rubric:
{rubric}

You are this run's coordinator (Arbor protocol). You cannot edit code or
run commands — executors do that in isolated worktrees; you steer the
Idea Tree. Your loop each turn:

1. idea_tree(action=view, format=constraints) — re-ground in the tree.
2. OBSERVE the latest harvest + failure evidence.
3. IDEATE: load the arbor-ideate skill, then idea_tree(action=add) 1-3
   four-line hypotheses under the most informative node.
4. dispatch_experiments(node_keys=[...]) — then END YOUR TURN. Harvest
   folds the results automatically before your next wake.
5. DECIDE on returned experiments: merge_experiment(start/status) for a
   done node that beats trunk on B_dev; idea_tree(action=prune) for dead
   branches (record the lesson as the reason).

First action now: idea_tree(action=set_meta) with the contract values
from the message above (eval_cmd, eval_cmd_test, metric_direction, …),
then ideate. B_dev is for iteration; the held-out test split is reached
ONLY through merge_experiment.
"""


def _outcome_is_active(outcome: dict[str, Any] | None) -> bool:
    """True iff session.config['outcome'] represents a non-terminal /goal."""
    if not isinstance(outcome, dict):
        return False
    status = outcome.get("status")
    return status in ("active", "paused")


async def handle_mission_create(
    *,
    description: str,
    rubric: str,
    session_id: UUID,
    org_id: UUID,
    agent_id: str,
    session_store: Any,
    session_factory: Any,
    mission_store: MissionStore,
    user_id: UUID | None = None,
    service_account_id: UUID | None = None,
    max_iterations: int = 20,
) -> MissionHandlerResult:
    """Create a new mission on the calling session.

    Exactly one of ``user_id`` / ``service_account_id`` must be set —
    the caller (the harness loop or the REST route) checks the session
    principal before invoking. Anonymous-channel sessions, which have
    neither, cannot own missions.

    Rejects when the session already has:
    * a non-terminal /goal outcome (``session.config['outcome']`` in
      active/paused), OR
    * an active or paused mission (via :class:`MissionStore`).

    On success: inserts the Mission row, updates session.config with
    ``active_mission_id``, ``coordinator=True``, ``strict_coordinator=True``,
    and the ``subagent-task-orchestrator`` preloaded skill; emits
    ``mission.defined``; returns the kickoff text via
    :attr:`MissionHandlerResult.kickoff_content`.

    The kickoff ``user.message`` and the agent enqueue are intentionally
    NOT emitted here.  The caller (the harness ``/mission`` slash
    handler) must emit the kickoff AFTER its own
    ``advance_harness_cursor`` runs — otherwise the cursor advance can
    race past the kickoff's event id and leave the next wake with
    "no actionable pending events".  Same control-flow pattern as
    ``/goal``'s outcome kickoff in ``harness/loop.py``.
    """
    if (user_id is None) == (service_account_id is None):
        return MissionHandlerResult(
            ok=False,
            error=(
                "handle_mission_create requires exactly one principal "
                "(user_id or service_account_id)"
            ),
        )
    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        if sess is None:
            return MissionHandlerResult(
                ok=False, error=f"session {session_id} not found",
            )
        if _outcome_is_active(sess.config.get("outcome")):
            return MissionHandlerResult(
                ok=False,
                error=(
                    "This session has an active /goal. Clear or pause it "
                    "before starting a /mission (only one evaluator loop "
                    "per session is allowed)."
                ),
            )

    try:
        mission_id = await mission_store.create(
            org_id=org_id,
            user_id=user_id,
            service_account_id=service_account_id,
            session_id=session_id,
            agent_id=agent_id,
            description=description,
            rubric=rubric,
            max_iterations=max_iterations,
        )
    except ActiveMissionConflictError as exc:
        return MissionHandlerResult(ok=False, error=str(exc))

    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        cfg = dict(sess.config or {})
        cfg["active_mission_id"] = str(mission_id)
        cfg["coordinator"] = True
        # Strip implementation tools so the LLM has to delegate via
        # spawn_task/delegate_task instead of "fixing it quickly"
        # itself.  ``_tool_filter_for_session`` reads this flag and
        # subtracts ``COORDINATOR_IMPLEMENTATION_TOOLS`` from the
        # effective tool set — the structural enforcement that the
        # ``subagent-task-orchestrator`` skill assumes is in place.
        cfg["strict_coordinator"] = True
        preloaded = list(cfg.get("preloaded_skills") or [])
        if "subagent-task-orchestrator" not in preloaded:
            preloaded.append("subagent-task-orchestrator")
        cfg["preloaded_skills"] = preloaded
        sess.config = cfg
        await db.commit()

    await session_store.emit_event(
        session_id, EventType.MISSION_DEFINED,
        {
            "mission_id": str(mission_id),
            "description": description,
            "rubric": rubric,
            "max_iterations": max_iterations,
        },
    )

    # Kickoff + enqueue are returned to the caller, NOT emitted here —
    # see the docstring for the cursor-race rationale.
    kickoff = _KICKOFF_TEMPLATE.format(description=description, rubric=rubric)
    return MissionHandlerResult(
        ok=True, mission_id=mission_id,
        message=f"Mission {mission_id} started.",
        kickoff_content=kickoff,
    )


async def handle_research_mission_create(
    *,
    cmd: AutoResearchCommand,
    session_id: UUID,
    org_id: UUID,
    agent_id: str,
    session_store: Any,
    session_factory: Any,
    mission_store: MissionStore,
    user_id: UUID | None = None,
    service_account_id: UUID | None = None,
) -> MissionHandlerResult:
    """Create a research-kind (Arbor) mission.

    Wraps :func:`handle_mission_create` (Mission row + standard config
    stamping), then adds the research sidecar: a ``research_runs`` row +
    ROOT idea node, server-side baseline writes (``test_baseline_score``
    is a machine key the coordinator's ``set_meta`` cannot write), the
    ``arbor-coordinator`` preload in place of ``subagent-task-orchestrator``,
    a ``research.defined`` event, and the research kickoff. ``/mission``'s
    create path is untouched.
    """
    if cmd.resume_run:
        return MissionHandlerResult(
            ok=False, error="resume=<run> is not supported yet",
        )
    if not cmd.repo or not cmd.repo.startswith("/workspace/"):
        return MissionHandlerResult(
            ok=False,
            error="repo=</workspace/...> is required for /auto-research create",
        )

    base = await handle_mission_create(
        description=cmd.description, rubric=cmd.rubric,
        session_id=session_id, org_id=org_id, agent_id=agent_id,
        session_store=session_store, session_factory=session_factory,
        mission_store=mission_store,
        user_id=user_id, service_account_id=service_account_id,
        max_iterations=cmd.max_iterations or 20,
    )
    if not base.ok:
        return base

    from surogates.arbor.store import ResearchStore

    store = ResearchStore(session_factory)
    short = str(base.mission_id)[:8]
    run_id = await store.create_run(
        org_id=org_id, mission_id=base.mission_id, session_id=session_id,
        agent_id=agent_id, repo_path=cmd.repo,
        trunk_branch=f"research/run-{short}/trunk",
        branch_prefix=f"research/run-{short}",
        objective=cmd.description,
    )

    # Baselines measured at intake are written server-side: test_baseline_score
    # is a machine key (idea_tree(set_meta) rejects it), and the merge gate
    # needs it as the held-out reference.
    baseline_meta: dict[str, Any] = {}
    if cmd.baseline is not None:
        baseline_meta["baseline_score"] = cmd.baseline
    if cmd.baseline_test is not None:
        baseline_meta["test_baseline_score"] = cmd.baseline_test
    if baseline_meta:
        await store.set_meta(run_id, baseline_meta, allow_machine_keys=True)

    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        cfg = dict(sess.config or {})
        cfg["active_research_run_id"] = str(run_id)
        # The research coordinator runs the Arbor protocol, not the generic
        # task-orchestrator playbook handle_mission_create preloaded.
        preloaded = [
            s for s in (cfg.get("preloaded_skills") or [])
            if s != "subagent-task-orchestrator"
        ]
        if "arbor-coordinator" not in preloaded:
            preloaded.append("arbor-coordinator")
        cfg["preloaded_skills"] = preloaded
        sess.config = cfg
        await db.commit()

    await session_store.emit_event(
        session_id, EventType.RESEARCH_DEFINED,
        {"mission_id": str(base.mission_id), "run_id": str(run_id)},
    )

    base.kickoff_content = _RESEARCH_KICKOFF_TEMPLATE.format(
        description=cmd.description, rubric=cmd.rubric,
    )
    base.message = f"Research mission {base.mission_id} started (run {run_id})."
    return base


async def handle_mission_status(
    *,
    session_id: UUID,
    mission_store: MissionStore,
) -> MissionHandlerResult:
    """Return a human-readable status string for the session's active mission."""
    active = await mission_store.get_active_for_session(session_id)
    if active is None:
        return MissionHandlerResult(
            ok=True, message="No active mission on this session.",
        )
    return MissionHandlerResult(
        ok=True, mission_id=active.id,
        message=(
            f"Mission {active.id}: status={active.status}, "
            f"iteration={active.iteration}/{active.max_iterations}.\n"
            f"Description: {active.description}\n"
            f"Latest evaluator verdict: "
            f"{active.last_evaluation_result or '(none yet)'}"
        ),
    )


async def handle_mission_pause(
    *,
    session_id: UUID,
    reason: str | None,
    session_store: Any,
    mission_store: MissionStore,
) -> MissionHandlerResult:
    active = await mission_store.get_active_for_session(session_id)
    if active is None:
        return MissionHandlerResult(ok=False, error="No active mission to pause.")
    if active.status != "active":
        return MissionHandlerResult(
            ok=False, mission_id=active.id,
            error=f"Mission is not active (status={active.status}); cannot pause.",
        )
    await mission_store.set_status(
        active.id, "paused", paused_reason=reason,
    )
    await session_store.emit_event(
        session_id, EventType.MISSION_PAUSED,
        {"mission_id": str(active.id), "reason": reason},
    )
    return MissionHandlerResult(
        ok=True, mission_id=active.id, message="Mission paused.",
    )


async def handle_mission_resume(
    *,
    session_id: UUID,
    org_id: str,
    agent_id: str,
    session_store: Any,
    mission_store: MissionStore,
    redis: Any,
) -> MissionHandlerResult:
    active = await mission_store.get_active_for_session(session_id)
    if active is None or active.status != "paused":
        return MissionHandlerResult(
            ok=False, error="No paused mission on this session.",
        )
    await mission_store.set_status(active.id, "active")
    await session_store.emit_event(
        session_id, EventType.MISSION_RESUMED,
        {"mission_id": str(active.id)},
    )
    # Wake the coordinator so pending continuations are processed.
    await enqueue_session(
        redis,
        org_id=str(org_id),
        agent_id=agent_id,
        session_id=session_id,
    )
    return MissionHandlerResult(
        ok=True, mission_id=active.id, message="Mission resumed.",
    )


async def handle_mission_cancel(
    *,
    session_id: UUID,
    reason: str | None,
    cascade_to_workers: bool,
    session_store: Any,
    session_factory: Any,
    mission_store: MissionStore,
    redis: Any,
) -> MissionHandlerResult:
    active = await mission_store.get_active_for_session(session_id)
    if active is None:
        return MissionHandlerResult(ok=False, error="No active mission to cancel.")
    if active.status not in ("active", "paused"):
        return MissionHandlerResult(
            ok=False, mission_id=active.id,
            error=f"Mission already terminal (status={active.status}).",
        )
    await mission_store.set_status(
        active.id, "cancelled", cancelled_reason=reason,
    )
    await session_store.clear_session_config_key(session_id, "active_mission_id")
    if cascade_to_workers:
        await _cascade_cancel_workers(
            mission_id=active.id,
            session_factory=session_factory,
            redis=redis,
        )
    await session_store.emit_event(
        session_id, EventType.MISSION_CANCELLED,
        {
            "mission_id": str(active.id),
            "reason": reason,
            "cascade_to_workers": cascade_to_workers,
        },
    )
    return MissionHandlerResult(
        ok=True, mission_id=active.id, message="Mission cancelled.",
    )


async def _cascade_cancel_workers(
    *, mission_id: UUID, session_factory: Any, redis: Any,
) -> None:
    """Cancel every non-terminal task belonging to ``mission_id``.

    For each ``running`` task, publishes an interrupt on its current
    Session's ``INTERRUPT_CHANNEL_PREFIX:<session_id>`` channel (the same
    mechanism ``cancel_task`` / ``stop_worker`` use). All non-terminal
    rows then transition to ``cancelled`` in a single UPDATE so we do
    not leave a window in which the dispatcher could re-claim a ready
    task while we are still iterating.
    """
    from sqlalchemy import func as _func, select as _sel, update as _upd

    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.db.models import Task

    async with session_factory() as db:
        running_session_ids = (await db.execute(
            _sel(Task.current_session_id).where(
                Task.mission_id == mission_id,
                Task.status == "running",
                Task.current_session_id.isnot(None),
            )
        )).scalars().all()
        await db.execute(
            _upd(Task)
            .where(
                Task.mission_id == mission_id,
                Task.status.in_(("todo", "ready", "running", "blocked")),
            )
            .values(status="cancelled", completed_at=_func.now())
        )
        await db.commit()

    for sid in running_session_ids:
        try:
            await redis.publish(
                f"{INTERRUPT_CHANNEL_PREFIX}:{sid}",
                "mission_cancel_cascade",
            )
        except Exception:
            # Don't let one bad publish strand the rest of the cascade.
            # The worker session times out naturally if the interrupt
            # doesn't land; the task is already marked cancelled in DB.
            logger.warning(
                "Failed to publish interrupt for session %s during mission %s cascade",
                sid, mission_id, exc_info=True,
            )
