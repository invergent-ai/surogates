"""Built-in ``delegate_task`` tool -- sub-agent delegation.

Spawns one or more child sessions that run in their own context window,
waits for them to complete, and returns the children's final responses
as the tool result.

Supports:

- **Batch fan-out**: pass ``goals`` (array) to launch N children in
  parallel; results are returned as a JSON list.
- **Role-based recursion**: ``role="orchestrator"`` lets a child delegate
  further; ``role="leaf"`` (default) strips ``delegate_task`` from the
  child's toolset.
- **Depth limit**: parent sessions track ``delegation_depth`` in their
  config. Calls deeper than ``_MAX_DELEGATION_DEPTH`` are rejected.
- **Session tracing**: emits ``DELEGATION_START`` / ``DELEGATION_COMPLETE``
  / ``DELEGATION_FAILED`` events on the parent with a tool-call trace
  built from the child's event log. A short trace summary is appended
  to the tool result text so the LLM can judge child reliability.
- **Stale detection**: while polling, emits ``DELEGATION_STALE`` once
  per child when the child has been idle past a threshold (different
  thresholds for "idle" vs "in tool"); the hard timeout still applies.

The handler requires ``session_store``, ``redis``, ``tenant``,
``session_id``, and ``budget`` to be passed as keyword arguments by the
harness loop (which injects them automatically).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from uuid import UUID

from surogates.config import enqueue_session
from surogates.session.events import EventType
from surogates.tools.registry import ToolRegistry, ToolSchema

logger = logging.getLogger(__name__)

# 60-minute ceiling so multi-step workflows (deep-research planner +
# writer hand-off, multi-source web extraction, long sandboxed runs)
# can complete without the parent treating an in-progress child as a
# failure.  Anything longer than this is treated as the child being
# wedged and the parent reclaims its iteration budget.
_DELEGATION_TIMEOUT_SECONDS: int = 3600
_POLL_INTERVAL_SECONDS: float = 1.0
_CHILD_MAX_ITERATIONS: int = 30
_MAX_DELEGATION_DEPTH: int = 2

# Cap on how long the parent waits to re-acquire its
# TurnConcurrencyGate slot when ``_run_single_delegation`` returns.
# In the common case the slot is free immediately (the parent's own
# release a moment ago opened one).  Genuine cap saturation by other
# tenants is rare; if we can't get a slot within this window we
# proceed without one and let the dispatcher's outer release fall on
# the floor-at-zero guard.  Slight under-count is acceptable;
# blocking the parent forever would expire the lease and produce an
# orphan re-enqueue cycle, which is a much worse failure mode.
_REACQUIRE_TIMEOUT_SECONDS: float = 30.0
_REACQUIRE_BACKOFF_SECONDS: float = 0.5


async def _reacquire_gate_with_backoff(
    turn_gate: Any, org_id: str, agent_id: str,
) -> bool:
    """Re-acquire the parent's gate slot, retrying briefly if at cap.

    Returns ``True`` on success.  ``False`` after
    ``_REACQUIRE_TIMEOUT_SECONDS`` of failed attempts -- caller logs
    and proceeds without (the dispatcher's outer release floors at
    zero, so we don't drive the counter negative).
    """
    if turn_gate is None:
        return False
    deadline = time.monotonic() + _REACQUIRE_TIMEOUT_SECONDS
    while True:
        try:
            if await turn_gate.try_acquire(org_id, agent_id):
                return True
        except Exception:
            logger.debug(
                "Transient try_acquire failure during re-acquire "
                "(org=%s agent=%s)", org_id, agent_id, exc_info=True,
            )
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_REACQUIRE_BACKOFF_SECONDS)

# Agent types that must not be delegated to recursively or in a
# batched fan-out.  These workflows are expensive (they themselves
# spawn sub-agents and run multi-round web extraction); allowing a
# parent that is *already* one of them to delegate to another, or
# letting a single ``delegate_task`` call spawn N of them in parallel,
# turns one user prompt into a runaway fleet of long-running sessions
# that each burn the full timeout.
_NON_RECURSIVE_AGENT_TYPES: frozenset[str] = frozenset({
    "deep-research",
})

# Children spend most time waiting for the LLM (idle) or running a tool.
# Idle stalls are more suspicious than tool stalls — tools like web fetch
# or sandbox exec can legitimately take minutes. Different thresholds let
# us flag genuinely stuck children without false alarms on slow tools.
_IDLE_STALE_THRESHOLD_SECONDS: float = 60.0
_IN_TOOL_STALE_THRESHOLD_SECONDS: float = 180.0

# Last N tool calls included in the LLM-visible result summary. The full
# trace lives in the DELEGATION_COMPLETE event payload.
_TRACE_SUMMARY_LIMIT: int = 10

_VALID_ROLES = {"leaf", "orchestrator"}

# Tools that should never run inside a delegated child, regardless of
# agent_type preset or parent toolset. ``ask_user_question`` would
# block waiting for human input the child has no surface for; the
# coordinator-family tools shouldn't let a child fork its own worker
# pool.
_DELEGATION_ALWAYS_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "ask_user_question",
    "spawn_worker",
    "send_worker_message",
    "stop_worker",
})

# Built-in file-mutating tools whose ``path`` argument we collect into
# the delegation's ``files_written`` list. Other tools may modify files
# indirectly (terminal, code_execution) — we don't try to track those.
_FILE_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "patch"})

# Built-in file-reading tools — collected into ``files_read`` so the
# parent can correlate reads with later modifications.
_FILE_READ_TOOLS: frozenset[str] = frozenset({"read_file"})


_DELEGATE_SCHEMA = ToolSchema(
    name="delegate_task",
    description=(
        "Delegate one or more tasks to sub-agents that each run in their "
        "own session. Pass `goal` for a single task or `goals` (array) "
        "to fan out in parallel. Use this for complex sub-tasks that "
        "benefit from a fresh context window."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Clear description of what the sub-agent should "
                    "accomplish. Use either `goal` or `goals`, not both."
                ),
            },
            "goals": {
                "type": "array",
                "description": (
                    "List of tasks to delegate in parallel. Each item is "
                    "an object with its own `goal` plus optional "
                    "`context`, `model`, `agent_type`, and `role`. Use "
                    "either `goal` or `goals`, not both."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "context": {"type": "string"},
                        "model": {"type": "string"},
                        "agent_type": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": ["leaf", "orchestrator"],
                        },
                    },
                    "required": ["goal"],
                    "additionalProperties": False,
                },
            },
            "context": {
                "type": "string",
                "description": "Relevant context the sub-agent needs to know.",
            },
            "model": {
                "type": "string",
                "description": "Optional model override for the sub-agent.",
            },
            "agent_type": {
                "type": "string",
                "description": (
                    "Optional name of a pre-configured sub-agent type. "
                    "Applies that type's system prompt, tool filter, "
                    "model, and iteration cap to the child session. "
                    "Explicit 'model' wins over the agent type's preset."
                ),
            },
            "role": {
                "type": "string",
                "enum": ["leaf", "orchestrator"],
                "description": (
                    "Recursion role. 'leaf' (default) strips "
                    "delegate_task from the child so it cannot spawn "
                    "further children. 'orchestrator' keeps it, allowing "
                    "one more level of delegation up to the depth limit."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
)


def register(registry: ToolRegistry) -> None:
    """Register the ``delegate_task`` tool."""
    registry.register(
        name="delegate_task",
        schema=_DELEGATE_SCHEMA,
        handler=_delegate_handler,
        toolset="core",
    )


def _normalize_tasks(arguments: dict[str, Any]) -> list[dict[str, Any]] | str:
    """Resolve scalar/batch arguments into a list of task dicts.

    Returns the task list on success, or an error string on validation
    failure. The list always has at least one item.
    """
    goal = arguments.get("goal")
    goals = arguments.get("goals")

    if goal and goals:
        return "provide either `goal` or `goals`, not both"
    if not goal and not goals:
        return "either `goal` or `goals` is required"

    shared_context = arguments.get("context") or ""
    shared_model = arguments.get("model")
    shared_agent_type = arguments.get("agent_type")
    shared_role = arguments.get("role") or "leaf"
    if shared_role not in _VALID_ROLES:
        return f"invalid role: {shared_role!r}"

    if goal:
        return [{
            "goal": str(goal),
            "context": shared_context,
            "model": shared_model,
            "agent_type": shared_agent_type,
            "role": shared_role,
        }]

    if not isinstance(goals, list) or not goals:
        return "`goals` must be a non-empty array"

    tasks: list[dict[str, Any]] = []
    for idx, item in enumerate(goals):
        if not isinstance(item, dict):
            return f"goals[{idx}] must be an object"
        item_goal = item.get("goal")
        if not item_goal:
            return f"goals[{idx}].goal is required"
        item_role = item.get("role") or shared_role
        if item_role not in _VALID_ROLES:
            return f"goals[{idx}].role invalid: {item_role!r}"
        tasks.append({
            "goal": str(item_goal),
            "context": item.get("context") or shared_context,
            "model": item.get("model") or shared_model,
            "agent_type": item.get("agent_type") or shared_agent_type,
            "role": item_role,
        })
    return tasks


async def _delegate_handler(
    arguments: dict[str, Any],
    **kwargs: Any,
) -> str:
    """Create one or more child sessions, run them concurrently, and
    return their results.

    Required kwargs (injected by the harness):
        session_store, redis, tenant, session_id, budget, session_factory.
    """
    session_store = kwargs.get("session_store")
    redis = kwargs.get("redis")
    tenant = kwargs.get("tenant")
    parent_session_id_str = kwargs.get("session_id")
    budget = kwargs.get("budget")
    session_factory = kwargs.get("session_factory")
    memory_manager = kwargs.get("memory_manager")
    bundle = kwargs.get("bundle")
    turn_gate = kwargs.get("turn_gate")

    if session_store is None:
        return json.dumps({"error": "session_store not available for delegation"})
    if tenant is None:
        return json.dumps({"error": "tenant context not available for delegation"})
    if parent_session_id_str is None:
        return json.dumps({"error": "parent session_id not available for delegation"})

    normalized = _normalize_tasks(arguments)
    if isinstance(normalized, str):
        return json.dumps({"error": normalized})
    tasks = normalized
    batch_mode = arguments.get("goals") is not None

    parent_session_id = UUID(str(parent_session_id_str))
    parent_session = await session_store.get_session(parent_session_id)
    parent_config = parent_session.config or {}
    parent_depth = int(parent_config.get("delegation_depth") or 0)

    if parent_depth >= _MAX_DELEGATION_DEPTH:
        return json.dumps({
            "error": (
                f"delegation depth limit reached "
                f"({parent_depth}/{_MAX_DELEGATION_DEPTH}); cannot nest "
                "further."
            ),
        })
    child_depth = parent_depth + 1

    # Guard against runaway fan-out / recursion of expensive workflows
    # like deep-research.  Two distinct cases:
    #
    #   (c) parent IS already running as one of these agent types and
    #       tries to delegate to the *same* type -- a planner spawning
    #       another planner.  Even within the depth cap this multiplies
    #       work and burns the timeout on every node.
    #
    #   (d) a single ``delegate_task`` call fans the same expensive
    #       agent type out across N goals in parallel.  One user prompt
    #       producing four ~15-minute children is exactly the failure
    #       mode that motivated this guard.
    parent_agent_type = parent_config.get("agent_type") or ""
    non_recursive_in_batch = [
        t["agent_type"] for t in tasks
        if t["agent_type"] in _NON_RECURSIVE_AGENT_TYPES
    ]
    if parent_agent_type in _NON_RECURSIVE_AGENT_TYPES:
        for t in tasks:
            if t["agent_type"] == parent_agent_type:
                return json.dumps({
                    "error": (
                        f"agent_type {parent_agent_type!r} cannot "
                        f"delegate to itself; finish the current "
                        f"workflow or hand off to a different agent "
                        f"type."
                    ),
                })
    if len(tasks) > 1 and non_recursive_in_batch:
        return json.dumps({
            "error": (
                "batch ``goals`` may not include agent_type "
                f"{sorted(set(non_recursive_in_batch))} -- these "
                "workflows must be delegated one at a time so they do "
                "not fan out into a runaway fleet."
            ),
        })

    # Split remaining budget evenly across children when batched.
    remaining = budget.remaining if budget else _CHILD_MAX_ITERATIONS
    per_child_budget = max(1, remaining // max(1, len(tasks)))

    # Release the parent's TurnConcurrencyGate slot for the duration
    # of the delegation: the parent is about to spend up to 15 minutes
    # idle inside _poll_child_completion waiting for the child, NOT
    # consuming worker CPU.  Counting it as 1 slot during that window
    # is a category error -- the gate is meant to track active work,
    # not sleeping waiters.  With this release, a deep delegation
    # chain stops self-saturating its own per-tenant cap.
    #
    # Re-acquire is best-effort: if the cap is genuinely saturated on
    # exit, we proceed without and let the dispatcher's outer release
    # fall on the floor-at-zero guard.  Slight under-count is
    # acceptable (cap is a guideline, not a correctness constraint);
    # blocking the parent forever waiting for a slot would be a much
    # worse failure mode (lease expiry, orphan re-enqueue cycle).
    parent_org_id = str(parent_session.org_id)
    parent_agent_id = parent_session.agent_id
    released_slot = False
    if turn_gate is not None:
        try:
            await turn_gate.release(parent_org_id, parent_agent_id)
            released_slot = True
        except Exception:
            logger.warning(
                "delegate_task: failed to release parent gate slot "
                "(org=%s agent=%s); proceeding without",
                parent_org_id, parent_agent_id, exc_info=True,
            )

    try:
        results = await asyncio.gather(*[
            _run_single_delegation(
                task=task,
                parent_session=parent_session,
                child_depth=child_depth,
                child_iteration_cap=min(_CHILD_MAX_ITERATIONS, per_child_budget),
                session_store=session_store,
                redis=redis,
                tenant=tenant,
                session_factory=session_factory,
                memory_manager=memory_manager,
                bundle=bundle,
            )
            for task in tasks
        ], return_exceptions=False)
    finally:
        if released_slot:
            try:
                await _reacquire_gate_with_backoff(
                    turn_gate, parent_org_id, parent_agent_id,
                )
            except Exception:
                logger.warning(
                    "delegate_task: failed to re-acquire parent gate "
                    "slot (org=%s agent=%s); the dispatcher's release "
                    "will rely on the floor-at-zero guard",
                    parent_org_id, parent_agent_id, exc_info=True,
                )

    if batch_mode:
        return json.dumps([
            {"goal": task["goal"], "result": result_text}
            for task, result_text in zip(tasks, results)
        ])
    return results[0]


async def _run_single_delegation(
    *,
    task: dict[str, Any],
    parent_session: Any,
    child_depth: int,
    child_iteration_cap: int,
    session_store: Any,
    redis: Any,
    tenant: Any,
    session_factory: Any,
    memory_manager: Any,
    bundle: Any | None = None,
) -> str:
    """Spawn one child, poll it, emit lifecycle events, and return its result."""
    goal: str = task["goal"]
    context: str = task["context"]
    model_override: str | None = task["model"]
    agent_type: str | None = task["agent_type"]
    role: str = task["role"]

    parent_session_id = parent_session.id
    agent_id = parent_session.agent_id

    agent_def: Any | None = None
    if agent_type:
        from surogates.harness.agent_resolver import resolve_agent_by_name
        agent_def = await resolve_agent_by_name(
            agent_type, tenant,
            session_factory=session_factory,
            bundle=bundle,
        )
        if agent_def is None:
            return json.dumps({
                "error": f"Unknown or disabled agent_type: {agent_type!r}.",
            })

    if child_iteration_cap <= 0:
        return json.dumps({"error": "iteration budget exhausted; cannot delegate"})

    iterations = child_iteration_cap
    if agent_def is not None and agent_def.max_iterations is not None:
        iterations = min(iterations, agent_def.max_iterations)

    if model_override is None and agent_def is not None and agent_def.model:
        model_override = agent_def.model

    user_content = goal
    if context:
        user_content = f"{goal}\n\n## Context\n{context}"

    child_config: dict[str, Any] = {
        "max_iterations": iterations,
        "streaming": False,
        "delegation_depth": child_depth,
        "delegation_role": role,
    }
    if agent_type:
        child_config["agent_type"] = agent_type

    allowed_tools: list[str] | None = None
    excluded_tools: list[str] = []
    if agent_def is not None:
        if agent_def.tools:
            allowed_tools = list(agent_def.tools)
        if agent_def.disallowed_tools:
            excluded_tools = list(agent_def.disallowed_tools)
        if agent_def.policy_profile:
            child_config["policy_profile"] = agent_def.policy_profile

    # Inherit parent's exclusions: anything the parent can't run, the
    # child can't either.
    parent_config = parent_session.config or {}
    parent_allowed = parent_config.get("allowed_tools")
    for t in parent_config.get("excluded_tools") or []:
        if t not in excluded_tools:
            excluded_tools.append(t)

    # Toolset intersection: child's allowlist must be ⊆ parent's
    # allowlist. Without this, an agent_type preset could grant the
    # child tools the parent itself doesn't have.
    if parent_allowed:
        parent_set = set(parent_allowed)
        if allowed_tools is None:
            allowed_tools = list(parent_allowed)
        else:
            allowed_tools = [t for t in allowed_tools if t in parent_set]

    # Hardcoded blocklist: tools that should never run in a delegated
    # child regardless of preset or parent config.
    for t in _DELEGATION_ALWAYS_BLOCKED_TOOLS:
        if allowed_tools is not None:
            if t in allowed_tools:
                allowed_tools.remove(t)
        elif t not in excluded_tools:
            excluded_tools.append(t)

    # Leaf role: strip delegate_task -- UNLESS the agent_def itself
    # lists delegate_task as required.  Some agent types are orchestrators
    # by definition (e.g. ``deep-research``, which uses delegate_task
    # to hand off to ``research-writer``); stripping the tool there
    # turns a structural workflow into a runtime failure where the
    # planner literally cannot reach the writer and falls back to
    # writing prose in-place.  Treat the agent_def's tool list as
    # authoritative: if it advertises delegate_task, the agent is an
    # orchestrator no matter what the caller passed for ``role``.
    agent_def_requires_delegate = (
        agent_def is not None
        and agent_def.tools is not None
        and "delegate_task" in agent_def.tools
    )
    if role == "leaf" and not agent_def_requires_delegate:
        if allowed_tools is not None:
            allowed_tools = [t for t in allowed_tools if t != "delegate_task"]
        elif "delegate_task" not in excluded_tools:
            excluded_tools.append("delegate_task")

    if allowed_tools is not None:
        child_config["allowed_tools"] = allowed_tools
    if excluded_tools and "allowed_tools" not in child_config:
        child_config["excluded_tools"] = excluded_tools

    started_at = time.monotonic()
    try:
        from surogates.session.provisioning import create_child_session

        child_session = await create_child_session(
            store=session_store,
            parent=parent_session,
            channel="delegation",
            model=model_override,
            config=child_config,
        )
        child_id = child_session.id

        await session_store.emit_event(
            child_id,
            EventType.USER_MESSAGE,
            {"content": user_content},
        )
        await session_store.emit_event(
            parent_session_id,
            EventType.DELEGATION_START,
            {
                "child_session_id": str(child_id),
                "goal": goal,
                "role": role,
                "depth": child_depth,
                "agent_type": agent_type,
                "model": model_override,
            },
        )
        if redis is not None:
            await enqueue_session(
                redis,
                org_id=str(tenant.org_id),
                agent_id=agent_id,
                session_id=child_id,
            )

        outcome = await _poll_child_completion(
            session_store=session_store,
            parent_session_id=parent_session_id,
            child_id=child_id,
        )

        duration_s = round(time.monotonic() - started_at, 3)

        if outcome["status"] == "complete":
            trace = _build_trace_from_events(outcome["events"])
            file_changes = _extract_file_changes(outcome["events"])
            await session_store.emit_event(
                parent_session_id,
                EventType.DELEGATION_COMPLETE,
                {
                    "child_session_id": str(child_id),
                    "goal": goal,
                    "duration_seconds": duration_s,
                    "tool_call_count": len(trace),
                    "trace": trace,
                    "files_written": file_changes["written"],
                    "files_read": file_changes["read"],
                },
            )
            result_text = outcome["text"]
            if memory_manager is not None:
                try:
                    memory_manager.on_delegation(
                        task=goal,
                        result=result_text[:2000],
                        child_session_id=str(child_id),
                    )
                except Exception:
                    logger.debug("Memory manager on_delegation failed", exc_info=True)
            return _format_result_with_trace_summary(
                result_text, trace, file_changes,
            )

        # failed or timeout
        await session_store.emit_event(
            parent_session_id,
            EventType.DELEGATION_FAILED,
            {
                "child_session_id": str(child_id),
                "goal": goal,
                "reason": outcome["reason"],
                "duration_seconds": duration_s,
            },
        )
        return json.dumps({"error": outcome["reason"]})

    except Exception as exc:
        logger.exception("delegate_task failed for parent %s", parent_session_id)
        return json.dumps({"error": f"Delegation failed: {exc}"})


async def _poll_child_completion(
    *,
    session_store: Any,
    parent_session_id: Any,
    child_id: Any,
    timeout: float | None = None,
    poll_interval: float | None = None,
    idle_stale_threshold: float | None = None,
    in_tool_stale_threshold: float | None = None,
) -> dict[str, Any]:
    """Poll until SESSION_COMPLETE / SESSION_FAIL, emitting a one-shot
    ``DELEGATION_STALE`` event on the parent if the child idles past
    threshold.

    Constants are looked up at call time (not as default-arg values) so
    tests can monkey-patch the module-level defaults.

    Returns ``{"status": "complete", "text": str, "events": [...]}`` on
    success, or ``{"status": "failed", "reason": str}`` on failure /
    timeout.
    """
    if timeout is None:
        timeout = _DELEGATION_TIMEOUT_SECONDS
    if poll_interval is None:
        poll_interval = _POLL_INTERVAL_SECONDS
    if idle_stale_threshold is None:
        idle_stale_threshold = _IDLE_STALE_THRESHOLD_SECONDS
    if in_tool_stale_threshold is None:
        in_tool_stale_threshold = _IN_TOOL_STALE_THRESHOLD_SECONDS
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    last_event_count = 0
    last_progress_time = loop.time()
    stale_emitted = False

    while loop.time() < deadline:
        events = await session_store.get_events(child_id)

        for event in events:
            if event.type == EventType.SESSION_COMPLETE.value:
                from surogates.harness.message_utils import extract_final_response
                return {
                    "status": "complete",
                    "text": extract_final_response(
                        events,
                        fallback=json.dumps({
                            "error": "No response found in child session",
                        }),
                    ),
                    "events": events,
                }
            if event.type == EventType.SESSION_FAIL.value:
                reason = (event.data or {}).get("reason", "unknown")
                return {
                    "status": "failed",
                    "reason": f"Child session failed: {reason}",
                }

        if len(events) > last_event_count:
            last_event_count = len(events)
            last_progress_time = loop.time()
            stale_emitted = False
        elif not stale_emitted:
            idle = loop.time() - last_progress_time
            in_tool = _last_event_is_unmatched_tool_call(events)
            threshold = (
                in_tool_stale_threshold if in_tool else idle_stale_threshold
            )
            if idle >= threshold:
                await session_store.emit_event(
                    parent_session_id,
                    EventType.DELEGATION_STALE,
                    {
                        "child_session_id": str(child_id),
                        "idle_seconds": round(idle, 1),
                        "in_tool": in_tool,
                        "threshold_seconds": threshold,
                    },
                )
                stale_emitted = True

        await asyncio.sleep(poll_interval)

    return {"status": "failed", "reason": "Delegation timed out"}


def _last_event_is_unmatched_tool_call(events: list[Any]) -> bool:
    """True when the latest TOOL_CALL has no following TOOL_RESULT —
    i.e. the child is actively inside a tool execution."""
    pending_tool_call_ids: set[str] = set()
    for event in events:
        if event.type == EventType.TOOL_CALL.value:
            tool_call_id = (event.data or {}).get("tool_call_id")
            if tool_call_id:
                pending_tool_call_ids.add(str(tool_call_id))
        elif event.type == EventType.TOOL_RESULT.value:
            tool_call_id = (event.data or {}).get("tool_call_id")
            if tool_call_id:
                pending_tool_call_ids.discard(str(tool_call_id))
    return bool(pending_tool_call_ids)


def _build_trace_from_events(events: list[Any]) -> list[dict[str, Any]]:
    """Build a tool-call trace from a child's event log.

    Each trace entry: ``{name, ok, tool_call_id?}``. Pairs TOOL_CALL with
    its TOOL_RESULT by ``tool_call_id`` when present; unpaired calls are
    recorded as ``ok=False`` (the child terminated mid-tool)."""
    by_call_id: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for event in events:
        data = event.data or {}
        if event.type == EventType.TOOL_CALL.value:
            entry = {
                "name": data.get("tool_name") or data.get("name") or "<unknown>",
                "ok": False,
                "tool_call_id": data.get("tool_call_id"),
            }
            ordered.append(entry)
            if entry["tool_call_id"]:
                by_call_id[str(entry["tool_call_id"])] = entry
        elif event.type == EventType.TOOL_RESULT.value:
            call_id = data.get("tool_call_id")
            if call_id and str(call_id) in by_call_id:
                entry = by_call_id[str(call_id)]
                entry["ok"] = not bool(data.get("error"))
    return ordered


def _extract_file_changes(events: list[Any]) -> dict[str, list[str]]:
    """Collect file paths the child read or wrote, in first-seen order.

    Reads ``arguments.path`` from ``write_file``/``patch``/``read_file``
    TOOL_CALL events. ``patch`` calls in ``mode='patch'`` (V4A multi-file
    body) are not parsed — only the simpler ``replace`` mode is captured.
    Returns ``{"written": [...], "read": [...]}`` with each path
    deduplicated."""
    written: list[str] = []
    read: list[str] = []
    seen_written: set[str] = set()
    seen_read: set[str] = set()
    for event in events:
        if event.type != EventType.TOOL_CALL.value:
            continue
        data = event.data or {}
        name = data.get("name") or data.get("tool_name")
        args = data.get("arguments")
        if not isinstance(args, dict):
            continue
        path = args.get("path")
        if not isinstance(path, str) or not path:
            continue
        if name in _FILE_WRITE_TOOLS and path not in seen_written:
            written.append(path)
            seen_written.add(path)
        elif name in _FILE_READ_TOOLS and path not in seen_read:
            read.append(path)
            seen_read.add(path)
    return {"written": written, "read": read}


def _format_result_with_trace_summary(
    result_text: str,
    trace: list[dict[str, Any]],
    file_changes: dict[str, list[str]] | None = None,
) -> str:
    """Append a one-line trace summary to the result so the LLM can see
    what the child did. Full trace + file lists live in the
    DELEGATION_COMPLETE event — this is just a hint, not a replacement."""
    suffix_parts: list[str] = []
    if trace:
        tail = trace[-_TRACE_SUMMARY_LIMIT:]
        summary_items = [
            f"{entry['name']}{'' if entry['ok'] else '!'}" for entry in tail
        ]
        elided = (
            ""
            if len(trace) <= _TRACE_SUMMARY_LIMIT
            else f"…+{len(trace) - _TRACE_SUMMARY_LIMIT} earlier, "
        )
        suffix_parts.append(
            f"delegation trace: {elided}{len(tail)} tool calls — "
            f"{', '.join(summary_items)}"
        )
    if file_changes:
        if file_changes.get("written"):
            suffix_parts.append(
                f"files modified: {', '.join(file_changes['written'])}"
            )
        if file_changes.get("read"):
            suffix_parts.append(
                f"files read: {', '.join(file_changes['read'])}"
            )
    if not suffix_parts:
        return result_text
    return f"{result_text}\n\n[{' | '.join(suffix_parts)}]"
