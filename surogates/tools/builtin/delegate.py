"""The run path of the ``agent`` tool: a sub-agent's child session, from its start to its report.

``agent`` (``tools/builtin/agents.py``) calls ``_run_single_delegation``,
which creates the child session on the ``delegation`` channel, gives it its
tools (``child_tool_policy``), and in the foreground waits for its report.

- **Depth limit**: parent sessions track ``delegation_depth`` in their
  config; ``agent`` refuses a start at ``_MAX_DELEGATION_DEPTH``.
- **Session tracing**: emits ``DELEGATION_START`` / ``DELEGATION_COMPLETE``
  / ``DELEGATION_FAILED`` events on the parent with a tool-call trace
  built from the child's event log. A short trace summary is appended
  to the result text so the LLM can judge child reliability.
- **Progress**: while polling, emits ``DELEGATION_PROGRESS`` on the parent
  for the child's newest tool call, so the parent's chat is not silent
  while it waits.
- **Stale detection**: while polling, emits ``DELEGATION_STALE`` once
  per child when the child has been idle past a threshold (different
  thresholds for "idle" vs "in tool"); the hard timeout still applies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from surogates.channels.constants import session_queue_priority
from surogates.config import enqueue_session
from surogates.session.events import EventType

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

# Agent types that must not be delegated to recursively or in a
# batched fan-out.  These workflows are expensive (they themselves
# spawn sub-agents and run multi-round web extraction); allowing a
# parent that is *already* one of them to delegate to another, or
# letting one response start N of them in parallel,
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


# Tools that should never run inside a delegated child, regardless of
# agent_type preset or parent toolset. ``ask_user_question`` would
# block waiting for human input the child has no surface for: a child
# asks its parent with ``send_message`` instead.
_DELEGATION_ALWAYS_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "ask_user_question",
})

# Built-in file-mutating tools whose ``path`` argument we collect into
# the delegation's ``files_written`` list. Other tools may modify files
# indirectly (terminal, code_execution) — we don't try to track those.
_FILE_WRITE_TOOLS: frozenset[str] = frozenset({"write_file", "patch"})

# Built-in file-reading tools — collected into ``files_read`` so the
# parent can correlate reads with later modifications.
_FILE_READ_TOOLS: frozenset[str] = frozenset({"read_file"})


def child_tool_policy(
    *,
    parent_config: dict[str, Any],
    agent_def: Any | None,
    role: str,
    leaf_tools: tuple[str, ...] = ("agent",),
) -> tuple[list[str] | None, list[str]]:
    """The tools a delegated child may use: its ``allowed_tools`` (None for no allowlist) and ``excluded_tools``.

    A leaf loses *leaf_tools*, the tools that would start a child of its own,
    unless its agent type lists ``agent`` (it is an orchestrator by
    definition).
    """
    allowed_tools: list[str] | None = None
    excluded_tools: list[str] = []
    if agent_def is not None:
        if agent_def.tools:
            allowed_tools = list(agent_def.tools)
        if agent_def.disallowed_tools:
            excluded_tools = list(agent_def.disallowed_tools)
    # Inherit parent's exclusions: anything the parent can't run, the
    # child can't either.
    parent_allowed = parent_config.get("allowed_tools")
    for t in parent_config.get("excluded_tools") or []:
        if t not in excluded_tools:
            excluded_tools.append(t)

    # Toolset intersection: child's allowlist must be ⊆ parent's
    # allowlist -- UNLESS the child is using an admin-defined
    # agent_def, in which case the agent_def's tools list is
    # authoritative for what the sub-agent needs.  Intersection used
    # to silently drop tools the agent_def explicitly required (e.g.
    # the research-writer's create_artifact was being stripped because
    # the planner -- a peer sub-agent -- doesn't have it in its own
    # allowlist).  Symptom in the wild: the writer literally cannot
    # create the report artifact, so it falls back to
    # ask_user_question and stalls the workflow.
    #
    # Platform-level denials still propagate via excluded_tools
    # (parent's exclusions are inherited above) and via
    # _DELEGATION_ALWAYS_BLOCKED_TOOLS below, so this carve-out
    # cannot be used to escape governance.
    if parent_allowed and allowed_tools is None:
        # Ad-hoc delegation with no agent_def -- inherit parent's
        # allowlist unchanged.  Without an admin contract there's
        # no other source of truth for what the child should have.
        allowed_tools = list(parent_allowed)
    # When ``allowed_tools`` came from the agent_def, leave it alone.

    # Hardcoded blocklist: tools that should never run in a delegated
    # child regardless of preset or parent config.
    for t in _DELEGATION_ALWAYS_BLOCKED_TOOLS:
        if allowed_tools is not None:
            if t in allowed_tools:
                allowed_tools.remove(t)
        elif t not in excluded_tools:
            excluded_tools.append(t)

    # Leaf role: strip agent -- UNLESS the agent_def itself
    # lists agent as required.  Some agent types are orchestrators
    # by definition (e.g. ``deep-research``, which uses agent
    # to hand off to ``research-writer``); stripping the tool there
    # turns a structural workflow into a runtime failure where the
    # planner literally cannot reach the writer and falls back to
    # writing prose in-place.  Treat the agent_def's tool list as
    # authoritative: if it advertises agent, the agent is an
    # orchestrator no matter what the caller passed for ``role``.
    agent_def_requires_delegate = (
        agent_def is not None
        and agent_def.tools is not None
        and "agent" in agent_def.tools
    )
    if role == "leaf" and not agent_def_requires_delegate:
        for leaf_tool in leaf_tools:
            if allowed_tools is not None:
                allowed_tools = [t for t in allowed_tools if t != leaf_tool]
            elif leaf_tool not in excluded_tools:
                excluded_tools.append(leaf_tool)
    return allowed_tools, excluded_tools


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
    live_parent_config: dict[str, Any] | None = None,
    leaf_tools: tuple[str, ...] = ("agent",),
    extra_config: dict[str, Any] | None = None,
    seed: list[dict] | None = None,
    start_extra: dict[str, Any] | None = None,
    on_started: Any | None = None,
) -> str:
    """Spawn one child, poll it, emit lifecycle events, and return its result.

    *leaf_tools* are the tools a leaf child loses, *extra_config* goes into the child's config,
    *seed* (a fork's conversation) is the child's context before its brief,
    *start_extra* goes into ``delegation.start``.  *on_started*, given the
    child's session once it is queued, returns the call's result to end the
    call there (a sub-agent in the background), or None to wait for it.
    """
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

    from surogates.board.groups import ensure_group_and_inherit

    # ``live_parent_config`` is the calling harness's session_config
    # kwarg: mirroring the group id into it lets the parent's board
    # hook deliver the join snapshot in THIS wake — right after
    # the agent call returns — so the model summarizes child results
    # with the verified board in view.  Concurrent delegations race
    # benignly (identical value, idempotent set).
    await ensure_group_and_inherit(
        parent_session=parent_session,
        session_store=session_store,
        child_config=child_config,
        live_parent_config=live_parent_config,
    )

    allowed_tools, excluded_tools = child_tool_policy(
        parent_config=parent_session.config or {}, agent_def=agent_def, role=role, leaf_tools=leaf_tools,
    )
    if allowed_tools is not None:
        child_config["allowed_tools"] = allowed_tools
    if excluded_tools and "allowed_tools" not in child_config:
        child_config["excluded_tools"] = excluded_tools
    if extra_config:
        child_config.update(extra_config)

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

        if seed is not None:
            # A fork starts from its parent's conversation, as a compaction
            # would leave it: replay reads it before the brief.
            await session_store.emit_event(
                child_id,
                EventType.CONTEXT_COMPACT,
                {
                    "compacted_messages": seed,
                    "strategy": "fork",
                    "original_message_count": len(seed),
                    "compressed_message_count": len(seed),
                },
            )
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
                **(start_extra or {}),
            },
        )
        if redis is not None:
            await enqueue_session(
                redis,
                org_id=str(tenant.org_id),
                agent_id=agent_id,
                session_id=child_id,
                priority=session_queue_priority(child_session),
            )

        if on_started is not None:
            early = await on_started(child_session)
            if early is not None:
                return early

        try:
            outcome = await _poll_child_completion(
                session_store=session_store,
                parent_session_id=parent_session_id,
                child_id=child_id,
            )
        except asyncio.CancelledError:
            # The turn that waited for the agent was stopped: no call takes its report now, so it stops too, as
            # stop_agent stops one.
            from surogates.tools.builtin.agents import stop_tree

            try:
                await asyncio.shield(stop_tree(
                    session_store=session_store, redis=redis, session_factory=session_factory, top_id=child_id,
                    by=parent_session_id, reason="the turn that waited for it was stopped",
                ))
            except Exception:
                logger.warning("Could not stop agent %s after its caller's turn was stopped", child_id, exc_info=True)
            raise

        duration_s = round(time.monotonic() - started_at, 3)

        if outcome["status"] == "complete":
            trace = _build_trace_from_events(outcome["events"])
            file_changes = _extract_file_changes(outcome["events"])

            # Propagate any artifacts the child created up to the
            # parent's chat thread.  The artifact spec lives under the
            # child's session prefix in S3, so we re-emit
            # ``artifact.created`` on the parent with
            # ``originating_session_id`` set to whichever session
            # actually owns the file.  The SDK uses that id to fetch
            # the spec via ``GET /api/sessions/{originating_session_id}
            # /artifacts/{artifact_id}`` instead of the chat's own
            # session id.  Propagation cascades naturally up the
            # delegation chain because the parent's own
            # run path will run the same scan on
            # whichever events it gets back from THIS session,
            # preserving the original ``originating_session_id``.
            await _propagate_child_artifacts(
                session_store=session_store,
                parent_session_id=parent_session_id,
                child_session_id=child_id,
                child_events=outcome["events"],
            )

            from surogates.harness.loop_context_replay import _listed, not_handed_back

            result_text = outcome["text"]
            done = next((e.data or {} for e in outcome["events"] if e.type == EventType.SESSION_COMPLETE.value), {})
            if done.get("not_kept"):
                # The helper's version of these is in the history, not in the thread's copy. Each name on the result's
                # one line, as a report names a thread's files: the pod's names are data.
                named = _listed([{"label": path} for path in done["not_kept"]])
                result_text += f"\n\nNot kept, because the thread or another helper changed them first: {named}"
            if unkept := not_handed_back(done, copy="this copy"):
                # Its hand-back failed: none of its work is in the thread's copy.
                result_text += f"\n\n{unkept}"
            from surogates.harness.subagents import BUDGET_REASON, budget_report

            if done.get("reason") == BUDGET_REASON:
                # An agent of the ``agent`` tool with a token budget used it up: its report is the reply to its last
                # tools-off call, never a line from the middle of a step.
                report = budget_report(outcome["events"])
                # Its report in place of the words it had mid-step; the notes made above stay after it.
                notes = result_text[len(outcome["text"]):].strip()
                result_text = "\n\n".join(
                    part for part in (report, notes, "[It stopped before it finished: it used its token budget.]") if part
                )
            result = _format_result_with_trace_summary(result_text, trace, file_changes)
            # Last, once the result is made: this event says the call has the helper's result, so the helper's
            # own report is no news to the parent (``loop_pending.answered_helpers``).
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
            if memory_manager is not None:
                try:
                    memory_manager.on_delegation(
                        task=goal,
                        result=result_text[:2000],
                        child_session_id=str(child_id),
                    )
                except Exception:
                    logger.debug("Memory manager on_delegation failed", exc_info=True)
            return result

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
        logger.exception("Sub-agent run failed for parent %s", parent_session_id)
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
    tool_calls = 0

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
            if event.type == EventType.SESSION_PAUSE.value and (event.data or {}).get("stopped_by"):
                # Stopped by an agent above it (``stop_agent``): the call has its answer, and waits no more.
                return {
                    "status": "failed",
                    "reason": f"The agent was stopped: {(event.data or {}).get('message') or 'stopped'}",
                }
            if event.type == EventType.SESSION_FAIL.value:
                from surogates.harness.loop_context_replay import _listed

                reason = (event.data or {}).get("reason", "unknown")
                # A failed helper's files may be half made: they stay in the history, apart. Each name on the
                # result's one line, as a report names a thread's files: the pod's names are data.
                left = (event.data or {}).get("left")
                named = _listed([{"label": path} for path in left]) if left else ""
                return {
                    "status": "failed",
                    "reason": f"Child session failed: {reason}" + (
                        f". Its changes to {named} were kept apart, not brought into this copy" if left else ""
                    ),
                }

        if len(events) > last_event_count:
            calls = [e for e in events[last_event_count:] if e.type == EventType.TOOL_CALL.value]
            if calls:
                tool_calls += len(calls)
                await session_store.emit_event(
                    parent_session_id,
                    EventType.DELEGATION_PROGRESS,
                    {
                        "child_session_id": str(child_id),
                        "tool": (calls[-1].data or {}).get("name"),
                        "tool_calls": tool_calls,
                    },
                )
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

    from surogates.harness.loop_pending import GAVE_UP_ON_HELPER

    # The helper may still run: its report, when it comes, is news to its parent (``answered_helpers``).
    return {"status": "failed", "reason": GAVE_UP_ON_HELPER}


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


async def _propagate_child_artifacts(
    *,
    session_store: Any,
    parent_session_id: Any,
    child_session_id: Any,
    child_events: list[Any],
) -> None:
    """Re-emit each child ``artifact.created`` event on the parent.

    The artifact spec lives in S3 under the *originating* session's
    workspace prefix (whichever session called ``create_artifact``).
    The SDK fetches it via that session's id, so the propagated event
    must carry an ``originating_session_id`` field pointing at the
    real owner.  If the child already propagated artifacts from
    further descendants (e.g. the deep-research planner forwarding
    the writer's report), preserve their original
    ``originating_session_id`` rather than reset it to the
    intermediate session.

    Errors are logged and swallowed -- a propagation failure must
    not abort the delegation result that triggered it.
    """
    for event in child_events:
        etype = getattr(event, "type", None)
        if etype != EventType.ARTIFACT_CREATED.value:
            continue
        data = dict(getattr(event, "data", None) or {})
        # Preserve the deepest known owner: a propagated event
        # already carries this field; a freshly-created one does
        # not, in which case we stamp it with the direct child.
        if not data.get("originating_session_id"):
            data["originating_session_id"] = str(child_session_id)
        try:
            await session_store.emit_event(
                parent_session_id,
                EventType.ARTIFACT_CREATED,
                data,
            )
        except Exception:
            logger.warning(
                "Failed to propagate artifact.created from "
                "child=%s to parent=%s; the artifact still exists "
                "on the child session but will not surface on the "
                "parent's chat thread.",
                child_session_id, parent_session_id, exc_info=True,
            )


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
        from surogates.harness.loop_context_replay import _listed

        # Each name on the summary's one line, as a report names a thread's files: the paths are the model's own
        # tool arguments.
        if file_changes.get("written"):
            suffix_parts.append(
                f"files modified: {_listed([{'label': path} for path in file_changes['written']])}"
            )
        if file_changes.get("read"):
            suffix_parts.append(
                f"files read: {_listed([{'label': path} for path in file_changes['read']])}"
            )
    if not suffix_parts:
        return result_text
    return f"{result_text}\n\n[{' | '.join(suffix_parts)}]"
