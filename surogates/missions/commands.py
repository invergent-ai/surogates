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
from datetime import datetime, timezone
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


MissionAction = Literal[
    "create", "status", "pause", "resume", "cancel", "budget",
    "accept", "reject",
]


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
    # Token allowance. ``None`` means "not specified"; with action="budget"
    # and clear_budget=True it means "take the ceiling off".
    budget_tokens: int | None = None
    clear_budget: bool = False


_CONTROL_VERBS = (
    "status", "pause", "resume", "cancel", "budget", "accept", "reject",
)
_RUBRIC_RE = re.compile(r"\bRubric\s*:", re.IGNORECASE)


# A ``Budget:`` directive line, anywhere in a create body. Anchored to a whole
# line so prose like "reconcile the marketing Budget: Q3" is not a directive.
_BUDGET_LINE_RE = re.compile(
    r"^[ \t]*Budget[ \t]*:[ \t]*(?P<value>[^\n]*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_TOKEN_COUNT_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<suffix>[kKmM]?)$")
_SUFFIX_SCALE = {"": 1, "k": 1_000, "m": 1_000_000}


def parse_token_budget(raw: str) -> int:
    """Parse a human token count: ``20000000``, ``20M``, ``500k``, ``1.5M``.

    Raises :class:`MissionCommandParseError` on anything unusable rather than
    falling back to "unbounded" — a mistyped ceiling that silently means no
    ceiling is the failure mode this exists to prevent.
    """
    text = (raw or "").strip().replace("_", "")
    m = _TOKEN_COUNT_RE.match(text)
    if m is None:
        raise MissionCommandParseError(
            f"unusable budget {raw!r}. Use a token count like "
            "'20M', '500k' or '20000000'."
        )
    value = int(float(m.group("num")) * _SUFFIX_SCALE[m.group("suffix").lower()])
    if value <= 0:
        raise MissionCommandParseError("budget must be greater than zero")
    return value


def _extract_budget(text: str) -> tuple[str, int | None]:
    """Pull a ``Budget:`` directive out of *text*; return (remainder, tokens)."""
    match = _BUDGET_LINE_RE.search(text)
    if match is None:
        return text, None
    tokens = parse_token_budget(match.group("value"))
    return text[:match.start()] + text[match.end():], tokens


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
        if verb in ("accept", "reject"):
            return MissionCommand(action=verb, reason=rest or None)
        if verb == "budget":
            if not rest:
                raise MissionCommandParseError(
                    "budget needs a value: '/mission budget 20M', or "
                    "'/mission budget none' to remove the ceiling."
                )
            if rest.lower() in ("none", "off", "clear", "unlimited"):
                return MissionCommand(
                    action="budget", budget_tokens=None, clear_budget=True,
                )
            return MissionCommand(
                action="budget", budget_tokens=parse_token_budget(rest),
            )
        return MissionCommand(action="status")

    text, budget_tokens = _extract_budget(text)

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
        budget_tokens=budget_tokens,
    )


# ---------------------------------------------------------------------------
# Slash command handlers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MissionHandlerResult:
    """Standard return shape for slash handlers.

    ``kickoff_content`` carries the synthetic ``user.message`` body
    that a successful ``handle_mission_create`` wants the caller to
    emit.  The caller writes it before its own slash-reply: once the
    reply is in the log the command is answered and is not run again,
    so a reply with no kickoff after it would leave the mission
    started and never worked on.
    """

    ok: bool
    mission_id: UUID | None = None
    message: str = ""
    error: str = ""
    kickoff_content: str | None = None
    # Label written into the deferred synthetic user.message's ``synthetic``
    # key. Defaults to the create path's value so existing callers are
    # unchanged; the amendment path overrides it so the two are
    # distinguishable in the event log and in ``count_synthetic_since``.
    kickoff_synthetic: str = "mission_kickoff"


_KICKOFF_TEMPLATE = """\
[Mission kickoff]

Description: {description}

Rubric:
{rubric}

You are this mission's coordinator. Start agents with ``agent`` to do the
work, wait for their reports, and sequence the next steps yourself: an
agent that depends on another's result starts once that result is in, and
a failed agent is continued or replaced by you. End each round with an
agent that verifies the work against the rubric.

Do NOT claim completion in prose alone -- the evaluator only honours
completion claims backed by your agents' reports OR an explicit
``[[mission-complete]]`` marker on its own line in your response.
"""


_AMENDED_CONTINUATION_TEMPLATE = """\
[Mission rubric amended by the user]

The rubric you were being graded against has been replaced. You are not
starting over -- everything already completed still counts.

Previous rubric:
{old_rubric}

New rubric:
{new_rubric}

Re-read the new rubric against the work already done, then continue. If the
completed work already satisfies it, say so with the evidence and mark
completion with [[mission-complete]] on its own line.
"""


def _outcome_is_active(outcome: dict[str, Any] | None) -> bool:
    """True iff session.config['outcome'] represents a non-terminal /goal."""
    if not isinstance(outcome, dict):
        return False
    status = outcome.get("status")
    return status in ("active", "paused")


def _aware(moment: datetime) -> datetime:
    """*moment* in UTC: the store gives some timestamps without their zone."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


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
    budget_tokens: int | None = None,
    typed_on: datetime | None = None,
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
    and no preloaded skill; emits
    ``mission.defined``; returns the kickoff text via
    :attr:`MissionHandlerResult.kickoff_content`.

    The kickoff ``user.message`` and the agent enqueue are intentionally
    NOT emitted here: the caller (the harness ``/mission`` slash handler)
    writes the kickoff, and then its answer.  Same control-flow pattern
    as ``/goal``'s outcome kickoff.

    *typed_on* is when the user sent the command, where a command is what
    calls this.  A mission this session started since then with the same
    description and rubric is the one a worker made for this very command
    before it died: it is not refused as a second one, and what that
    worker had not yet written is written now.
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
            budget_tokens=budget_tokens,
        )
    except ActiveMissionConflictError as exc:
        made = await mission_store.get_active_for_session(session_id)
        if (
            typed_on is None or made is None or made.status != "active"
            or (made.description, made.rubric) != (description, rubric)
            or _aware(made.created_at) < _aware(typed_on)
        ):
            return MissionHandlerResult(ok=False, error=str(exc))
        mission_id = made.id

    async with session_factory() as db:
        sess = await db.get(ORMSession, session_id)
        cfg = dict(sess.config or {})
        cfg["active_mission_id"] = str(mission_id)
        cfg["coordinator"] = True
        # Strip implementation tools so the LLM has to start agents
        # instead of "fixing it quickly" itself.  ``_tool_filter_for_session``
        # reads this flag and subtracts ``COORDINATOR_IMPLEMENTATION_TOOLS``
        # from the effective tool set.
        cfg["strict_coordinator"] = True
        sess.config = cfg
        await db.commit()

    if await session_store.last_event(
        session_id, EventType.MISSION_DEFINED, containing={"mission_id": str(mission_id)},
    ) is None:
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
    # see the docstring.
    kickoff = _KICKOFF_TEMPLATE.format(description=description, rubric=rubric)
    return MissionHandlerResult(
        ok=True, mission_id=mission_id,
        message=f"Mission {mission_id} started.",
        kickoff_content=kickoff,
    )


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
    # The mission's status is written last, and every step before it can be
    # taken again: a run cut off before it leaves the mission in flight, and
    # the command run again takes each step again, its helpers' stop with them.
    if cascade_to_workers:
        await _stop_mission_agents(
            mission_id=active.id, coordinator_id=session_id, session_store=session_store,
            session_factory=session_factory, mission_store=mission_store, redis=redis,
        )
    await session_store.clear_session_config_key(session_id, "active_mission_id")
    await mission_store.set_status(
        active.id, "cancelled", cancelled_reason=reason,
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


async def handle_mission_accept(
    *,
    session_id: UUID,
    session_store: Any,
    mission_store: MissionStore,
    typed_at: int | None = None,
) -> MissionHandlerResult:
    """Apply the judge's proposed rubric. This is the authority act.

    The new rubric is read from the ``mission.refinement_proposed`` event,
    never from the command and never from anything the coordinator wrote.
    The coordinator relays the proposal to the user; what it types cannot change
    what this commits.

    The continuation is returned as ``kickoff_content`` rather than emitted
    here: the slash dispatcher advances the harness cursor past its own
    reply, so an inline emit would be skipped on the next wake.

    The amendment is recorded before it is applied: a rubric amended and
    not recorded is one a second run of the command could not tell for its
    own.  *typed_at* is the id of the event the command was typed in, where
    a command is what calls this, and the record names it.  The record of
    the mission's amendment that names it is the one a run of this very
    command made before its worker died: it is applied again, which changes
    nothing applied already, and answered, its continuation with it, as that
    run would have.  Another accept's record is never this one's.  Nor is an
    amendment recorded twice: one recorded since the mission's latest
    proposal, by an accept whose amendment then failed, is the one applied.
    """
    active = await mission_store.get_active_for_session(session_id)
    mine = await session_store.last_event(
        session_id, EventType.MISSION_AMENDED, containing={"mission_id": str(active.id), "typed_at": typed_at},
    ) if active is not None and typed_at is not None else None
    amended = mine.data if mine is not None else None
    if amended is None:
        if active is None or active.paused_reason != "awaiting_refinement":
            return MissionHandlerResult(
                ok=False,
                error="No rubric refinement is awaiting your decision.",
            )
        proposal = await session_store.last_event(
            session_id, EventType.MISSION_REFINEMENT_PROPOSED, containing={"mission_id": str(active.id)},
        )
        proposed = proposal.data if proposal is not None else {}
        new_rubric = str(proposed.get("proposed_rubric") or "").strip()
        if not new_rubric:
            return MissionHandlerResult(
                ok=False, mission_id=active.id,
                error=(
                    "The recorded proposal is missing or empty; use "
                    "/mission reject or /mission cancel."
                ),
            )
        earlier = await session_store.last_event(
            session_id, EventType.MISSION_AMENDED, containing={"mission_id": str(active.id)},
        )
        if earlier is not None and earlier.id > proposal.id:
            amended = earlier.data
        else:
            amended = {
                "mission_id": str(active.id),
                "old_rubric": active.rubric,
                "new_rubric": new_rubric,
                "evidence": str(proposed.get("evidence") or ""),
                "typed_at": typed_at,
            }
            await session_store.emit_event(session_id, EventType.MISSION_AMENDED, amended)
    await mission_store.amend_rubric(active.id, new_rubric=amended["new_rubric"])
    return MissionHandlerResult(
        ok=True, mission_id=active.id,
        message="Rubric amended; mission resumed.",
        kickoff_content=_AMENDED_CONTINUATION_TEMPLATE.format(
            old_rubric=amended["old_rubric"], new_rubric=amended["new_rubric"],
        ),
        kickoff_synthetic="mission_amended",
    )


async def handle_mission_reject(
    *,
    session_id: UUID,
    session_store: Any,
    mission_store: MissionStore,
    reason: str | None = None,
) -> MissionHandlerResult:
    """Decline the proposal; the mission ends as the judge originally ruled.

    ``held_verdict`` comes off the proposal event rather than from a fresh
    judge call: the user is declining *this* proposal against *that* ruling,
    and a second call could return something else.

    ``paused_reason`` is left as ``awaiting_refinement`` on the terminal row
    -- a breadcrumb that this mission ended after a refinement was declined.
    ``get_active_for_session`` excludes terminal statuses, so it cannot be
    mistaken for a live proposal.

    ``reason`` is the user's own justification for declining. It is recorded
    on the verdict event: the whole point of this mechanism is that a pivot
    leaves a trail, and so should a refusal to pivot.
    """
    active = await mission_store.get_active_for_session(session_id)
    if active is None or active.paused_reason != "awaiting_refinement":
        return MissionHandlerResult(
            ok=False,
            error="No rubric refinement is awaiting your decision.",
        )
    proposal = await session_store.latest_mission_proposal(session_id, active.id)
    held = str((proposal or {}).get("held_verdict") or "blocked")
    if held not in ("blocked", "failed"):
        held = "blocked"

    await mission_store.set_status(active.id, held)
    await session_store.clear_session_config_key(session_id, "active_mission_id")
    await session_store.emit_event(
        session_id, EventType.MISSION_EVALUATION_END,
        {
            "mission_id": str(active.id),
            "trigger": "refinement_rejected",
            "result": held,
            "explanation": str((proposal or {}).get("explanation") or ""),
            "feedback": "",
            "reason": reason or "",
        },
    )
    return MissionHandlerResult(
        ok=True, mission_id=active.id,
        message=f"Refinement declined; mission {held}.",
    )


async def _stop_mission_agents(
    *, mission_id: UUID, coordinator_id: UUID, session_store: Any, session_factory: Any,
    mission_store: MissionStore, redis: Any,
) -> None:
    """Stop every running agent of ``mission_id``, with the agents under it, as ``stop_agent`` stops one.

    An agent already stopped is interrupted again while a worker still holds its session: a cascade cut off after
    its stop was written never told it, and the run that takes the cascade again is the one that does.  One at rest
    is not told again.
    """
    import json

    from surogates.config import INTERRUPT_CHANNEL_PREFIX
    from surogates.tools.builtin.agents import _STOP_SIGNAL, stop_tree

    for agent in await mission_store.agents(mission_id):
        try:
            if agent.status in ("active", "processing"):
                await stop_tree(
                    session_store=session_store, redis=redis, session_factory=session_factory,
                    top_id=agent.session_id, by=coordinator_id, reason="the mission was cancelled",
                )
            elif agent.status == "paused" and redis is not None and await session_store.has_live_lease(agent.session_id):
                await redis.publish(f"{INTERRUPT_CHANNEL_PREFIX}:{agent.session_id}", json.dumps({"reason": _STOP_SIGNAL}))
        except Exception:
            # One agent that cannot be stopped does not strand the rest of the cascade.
            logger.warning(
                "Failed to stop agent %s during mission %s cascade", agent.session_id, mission_id, exc_info=True,
            )


async def handle_mission_budget(
    *,
    session_id: UUID,
    budget_tokens: int | None,
    mission_store: MissionStore,
) -> str:
    """Set or clear the active mission's token allowance."""
    mission = await mission_store.get_active_for_session(session_id)
    if mission is None:
        return "No active mission on this session."
    await mission_store.set_budget(mission.id, budget_tokens=budget_tokens)
    if budget_tokens is None:
        return "Mission budget cleared — the objective is now unbounded."
    spent = await mission_store.tokens_spent(mission.id)
    return (
        f"Mission budget set to {budget_tokens:,} tokens "
        f"({spent:,} already spent)."
    )
