"""Tool execution and parallelisation logic for the agent harness.

Provides the policy for deciding whether a batch of tool calls can be
executed concurrently, path-overlap detection for file-scoped tools,
and the actual execution functions (sequential, concurrent, single tool).

The execution functions are standalone async functions that accept their
dependencies as parameters so the harness can delegate without coupling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable

from surogates.harness.message_utils import make_skipped_tool_result

# ---------------------------------------------------------------------------
# Path sanitisation — replace workspace absolute paths with __WORKSPACE__
# so that real filesystem paths never leak to the frontend via SSE events.
# ---------------------------------------------------------------------------

_WORKSPACE_TOKEN = "__WORKSPACE__"


def _sanitize_paths(data: Any, workspace_path: str | None) -> Any:
    """Replace occurrences of the workspace absolute path with __WORKSPACE__.

    Works on strings, dicts, and nested structures.  Returns a new object;
    does not mutate the input.
    """
    if not workspace_path:
        return data
    # Normalise: ensure no trailing slash for consistent replacement.
    ws = workspace_path.rstrip("/")
    if isinstance(data, str):
        return data.replace(ws, _WORKSPACE_TOKEN)
    if isinstance(data, dict):
        return {k: _sanitize_paths(v, workspace_path) for k, v in data.items()}
    if isinstance(data, list):
        return [_sanitize_paths(v, workspace_path) for v in data]
    return data
from surogates.session.events import EventType
from surogates.tools.coerce import coerce_tool_args

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from surogates.governance.saga.orchestrator import SagaOrchestrator
    from surogates.harness.budget import IterationBudget
    from surogates.harness.subdirectory_hints import SubdirectoryHintTracker
    from surogates.sandbox.pool import SandboxPool
    from surogates.session.models import Session, SessionLease
    from surogates.session.store import SessionStore
    from surogates.tenant.context import TenantContext
    from surogates.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool parallelisation policy constants
# ---------------------------------------------------------------------------

NEVER_PARALLEL_TOOLS: frozenset[str] = frozenset({"clarify", "delegate_task"})

# Read-only tools that are safe to execute concurrently with each other
# and with ongoing LLM streaming.  These tools have no side effects —
# executing them speculatively during streaming is always harmless.
# Also used by :func:`should_parallelize` as the set of tools that can
# always run in parallel (superset of the former ``CONCURRENCY_SAFE_TOOLS``).
CONCURRENCY_SAFE_TOOLS: frozenset[str] = frozenset({
    "file_read",
    "read_file",
    "search_files",
    "list_files",
    "session_search",
    "skills_list",
    "skill_view",
    "web_search",
    "web_extract",
    "web_crawl",
    "todo",
})

# Tools whose errors should abort all sibling concurrent executions.
# A terminal error often signals an environment problem that makes
# sibling tool results unreliable.
SIBLING_ABORT_TOOLS: frozenset[str] = frozenset({
    "terminal",
})


def is_concurrency_safe(tool_name: str) -> bool:
    """Return ``True`` if *tool_name* can safely execute during streaming."""
    return tool_name in CONCURRENCY_SAFE_TOOLS

PATH_SCOPED_TOOLS: frozenset[str] = frozenset({
    "file_read",
    "file_write",
    "read_file",
    "write_file",
})

# Sandbox tools that are safe to run in parallel.  These execute as
# independent operations in the sandbox pod — no shared mutable state
# beyond the filesystem (which the LLM is responsible for coordinating).
SANDBOX_PARALLEL_TOOLS: frozenset[str] = frozenset({
    "terminal",
})

# All tools that can run concurrently — read-only tools plus sandbox tools
# that are independent operations.  Used by both `should_parallelize` and
# the streaming executor.
PARALLEL_TOOLS: frozenset[str] = CONCURRENCY_SAFE_TOOLS | SANDBOX_PARALLEL_TOOLS


def is_parallelizable(tool_name: str) -> bool:
    """Return ``True`` if *tool_name* can run concurrently with other parallelizable tools."""
    return tool_name in PARALLEL_TOOLS

MAX_TOOL_WORKERS: int = 8

# Read-only tools that never participate in saga tracking — they have no
# side effects to compensate.
SAGA_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "clarify",
    "list_files",
    "read_file",
    "file_read",
    "search_files",
    "session_search",
    "skill_view",
    "skills_list",
    "todo",
    "web_crawl",
    "web_extract",
    "web_search",
})

# Patterns that indicate a terminal command may modify/delete files.
_DESTRUCTIVE_PATTERNS = re.compile(
    r"""(?:^|\s|&&|\|\||;|`)(?:
        rm\s|rmdir\s|
        mv\s|
        sed\s+-i|
        truncate\s|
        dd\s|
        shred\s|
        git\s+(?:reset|clean|checkout)\s
    )""",
    re.VERBOSE,
)
# Output redirects that overwrite files (> but not >>)
_REDIRECT_OVERWRITE = re.compile(r'[^>]>[^>]|^>[^>]')


def is_destructive_command(cmd: str) -> bool:
    """Heuristic: does this terminal command look like it modifies/deletes files?"""
    if not cmd:
        return False
    if _DESTRUCTIVE_PATTERNS.search(cmd):
        return True
    if _REDIRECT_OVERWRITE.search(cmd):
        return True
    return False

# ---------------------------------------------------------------------------
# Parallelisation decision logic
# ---------------------------------------------------------------------------


def _all_concurrency_safe(tool_calls: list[dict[str, Any]]) -> bool:
    """Return ``True`` if every tool call in the batch is concurrency-safe."""
    return all(
        is_concurrency_safe(tc.get("function", {}).get("name", ""))
        for tc in tool_calls
    )


def should_parallelize(tool_calls: list[dict[str, Any]]) -> bool:
    """Decide whether a batch of tool calls can be executed concurrently.

    Rules:
    - Single tool call -> sequential (no benefit from parallelism).
    - Any tool in ``NEVER_PARALLEL_TOOLS`` -> sequential.
    - All tools in ``CONCURRENCY_SAFE_TOOLS`` -> parallel.
    - Tools in ``PATH_SCOPED_TOOLS`` -> parallel only if paths don't overlap.
    - All tools are sandbox-executable (terminal, file ops) -> parallel.
    - Otherwise -> sequential.
    """
    if len(tool_calls) <= 1:
        return False

    names: list[str] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        names.append(fn.get("name", ""))

    # Any tool that must never run in parallel?
    if any(n in NEVER_PARALLEL_TOOLS for n in names):
        return False

    # All tools are parallelizable (read-only + sandbox)?
    if all(n in PARALLEL_TOOLS for n in names):
        return True

    # Path-scoped tools can run in parallel if paths don't overlap.
    all_parallel_or_path = PARALLEL_TOOLS | PATH_SCOPED_TOOLS
    if all(n in all_parallel_or_path for n in names):
        return paths_do_not_overlap(
            [tc for tc in tool_calls
             if tc.get("function", {}).get("name", "") in PATH_SCOPED_TOOLS],
        )

    # Default: sequential.
    return False


def paths_do_not_overlap(tool_calls: list[dict[str, Any]]) -> bool:
    """Return ``True`` if none of the path-scoped tool calls target overlapping paths.

    Two paths overlap if one is a prefix of the other (i.e. same file or
    parent-child directory relationship).
    """
    paths: list[str] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except (json.JSONDecodeError, TypeError):
            args = {}
        # Common argument names for file paths.
        path = args.get("path") or args.get("file_path") or args.get("filename") or ""
        if path:
            paths.append(str(path))

    # Check pairwise overlap.
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            a, b = paths[i], paths[j]
            if a == b or a.startswith(b) or b.startswith(a):
                return False

    return True


# ---------------------------------------------------------------------------
# Tool execution functions
# ---------------------------------------------------------------------------


async def execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    session: Session,
    lease: SessionLease,
    store: SessionStore,
    tools: ToolRegistry,
    tenant: TenantContext,
    interrupt_check: Callable[[], bool],
    redis: Redis | None = None,
    budget: IterationBudget | None = None,
    memory_manager: Any | None = None,
    hint_tracker: SubdirectoryHintTracker | None = None,
    sandbox_pool: SandboxPool | None = None,
    api_client: Any | None = None,
    session_factory: Any | None = None,
    saga: SagaOrchestrator | None = None,
    log_policy_allowed: bool = False,
) -> list[dict]:
    """Execute tool calls, choosing parallel vs sequential.

    When *saga* is active, parallel execution is restricted to
    concurrency-safe (read-only) tools only — side-effecting tools
    must run sequentially so saga compensation has deterministic step
    ordering.  Read-only tools have no side effects to compensate,
    so they can still benefit from parallelism.
    """
    if should_parallelize(tool_calls) and (
        saga is None or _all_concurrency_safe(tool_calls)
    ):
        return await execute_tool_calls_concurrent(
            tool_calls,
            session=session,
            lease=lease,
            store=store,
            tools=tools,
            tenant=tenant,
            interrupt_check=interrupt_check,
            redis=redis,
            budget=budget,
            memory_manager=memory_manager,
            hint_tracker=hint_tracker,
            sandbox_pool=sandbox_pool,
            api_client=api_client,
            session_factory=session_factory,
            log_policy_allowed=log_policy_allowed,
        )
    return await execute_tool_calls_sequential(
        tool_calls,
        session=session,
        lease=lease,
        store=store,
        tools=tools,
        tenant=tenant,
        interrupt_check=interrupt_check,
        redis=redis,
        budget=budget,
        memory_manager=memory_manager,
        hint_tracker=hint_tracker,
        sandbox_pool=sandbox_pool,
        api_client=api_client,
        session_factory=session_factory,
        saga=saga,
        log_policy_allowed=log_policy_allowed,
    )


async def execute_tool_calls_sequential(
    tool_calls: list[dict[str, Any]],
    *,
    session: Session,
    lease: SessionLease,
    store: SessionStore,
    tools: ToolRegistry,
    tenant: TenantContext,
    interrupt_check: Callable[[], bool],
    redis: Redis | None = None,
    budget: IterationBudget | None = None,
    memory_manager: Any | None = None,
    hint_tracker: SubdirectoryHintTracker | None = None,
    sandbox_pool: SandboxPool | None = None,
    api_client: Any | None = None,
    session_factory: Any | None = None,
    saga: SagaOrchestrator | None = None,
    log_policy_allowed: bool = False,
) -> list[dict]:
    """Execute tool calls one at a time, emitting events for each."""
    results: list[dict] = []

    for tc in tool_calls:
        # --- Interrupt check before each tool call ---
        if interrupt_check():
            results.append(make_skipped_tool_result(tc))
            continue

        result_msg = await execute_single_tool(
            tc,
            session=session,
            lease=lease,
            store=store,
            tools=tools,
            tenant=tenant,
            redis=redis,
            budget=budget,
            memory_manager=memory_manager,
            hint_tracker=hint_tracker,
            sandbox_pool=sandbox_pool,
            api_client=api_client,
            session_factory=session_factory,
            saga=saga,
            log_policy_allowed=log_policy_allowed,
        )
        results.append(result_msg)

    return results


async def execute_tool_calls_concurrent(
    tool_calls: list[dict[str, Any]],
    *,
    session: Session,
    lease: SessionLease,
    store: SessionStore,
    tools: ToolRegistry,
    tenant: TenantContext,
    interrupt_check: Callable[[], bool],
    redis: Redis | None = None,
    budget: IterationBudget | None = None,
    memory_manager: Any | None = None,
    hint_tracker: SubdirectoryHintTracker | None = None,
    sandbox_pool: SandboxPool | None = None,
    api_client: Any | None = None,
    session_factory: Any | None = None,
    log_policy_allowed: bool = False,
) -> list[dict]:
    """Execute tool calls concurrently using asyncio.gather.

    Results are returned in the original tool-call order.
    If interrupted, remaining calls are skipped.

    Each concurrent tool runs inside a copied :mod:`contextvars` context
    so that ``new_span()`` inside ``execute_single_tool`` does not clobber
    sibling tasks' trace state.
    """
    import contextvars as _cv

    from surogates.trace import get_trace

    # Capture the parent trace *before* spawning tasks so every child
    # span shares the same parent.
    parent_trace = get_trace()

    # Cap concurrency to MAX_TOOL_WORKERS via a semaphore.
    sem = asyncio.Semaphore(MAX_TOOL_WORKERS)

    async def _guarded(tc: dict[str, Any]) -> dict:
        if interrupt_check():
            return make_skipped_tool_result(tc)
        async with sem:
            if interrupt_check():
                return make_skipped_tool_result(tc)
            return await execute_single_tool(
                tc,
                session=session,
                lease=lease,
                store=store,
                tools=tools,
                tenant=tenant,
                redis=redis,
                budget=budget,
                memory_manager=memory_manager,
                hint_tracker=hint_tracker,
                sandbox_pool=sandbox_pool,
                api_client=api_client,
                session_factory=session_factory,
                _parent_trace=parent_trace,
                log_policy_allowed=log_policy_allowed,
            )

    # Spawn each task in its own context copy so new_span() calls
    # inside execute_single_tool are isolated from siblings.
    loop = asyncio.get_running_loop()
    tasks = [
        loop.create_task(_guarded(tc), context=_cv.copy_context())
        for tc in tool_calls
    ]
    return list(await asyncio.gather(*tasks))


async def execute_single_tool(
    tc: dict[str, Any],
    *,
    session: Session,
    lease: SessionLease,
    store: SessionStore,
    tools: ToolRegistry,
    tenant: TenantContext,
    redis: Redis | None = None,
    budget: IterationBudget | None = None,
    memory_manager: Any | None = None,
    hint_tracker: SubdirectoryHintTracker | None = None,
    sandbox_pool: SandboxPool | None = None,
    api_client: Any | None = None,
    session_factory: Any | None = None,
    _parent_trace: Any | None = None,
    saga: SagaOrchestrator | None = None,
    log_policy_allowed: bool = False,
) -> dict:
    """Execute a single tool call: emit events, dispatch, return result message.

    When *log_policy_allowed* is True, every governance check that passes
    also emits a ``policy.allowed`` event.  Off by default because each
    successful ``tool.call`` is already an implicit allow; enable for
    compliance audits that require an explicit per-decision record.
    """
    from surogates.trace import TraceContext, get_trace, new_span

    # Each tool call gets its own child span for fine-grained tracing.
    # When called from concurrent execution, _parent_trace is the
    # captured parent so siblings don't clobber each other.
    new_span(_parent_trace)

    fn = tc.get("function", {})
    tool_name: str = fn.get("name", "")
    tool_args_raw: str = fn.get("arguments", "")
    tool_call_id: str = tc.get("id", "")

    # Parse arguments.
    try:
        tool_args = json.loads(tool_args_raw) if tool_args_raw else {}
    except json.JSONDecodeError:
        tool_args = {}

    # Coerce argument types to match JSON Schema declarations.
    tool_args = coerce_tool_args(tool_name, tool_args, tools)

    # Sanitise paths in tool arguments before emitting events — replace
    # the workspace absolute path with __WORKSPACE__ so real filesystem
    # paths never leak to the frontend.
    workspace_path = session.config.get("workspace_path")
    sanitized_args = _sanitize_paths(tool_args, workspace_path)

    # Emit TOOL_CALL event.
    # Include checkpoint hash if the harness stashed one (file-mutating tools).
    tool_call_data: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "name": tool_name,
        "arguments": sanitized_args,
    }
    checkpoint_hash = tc.get("_checkpoint_hash")
    if checkpoint_hash:
        tool_call_data["checkpoint_hash"] = checkpoint_hash

    call_event_id = await store.emit_event(
        session.id,
        EventType.TOOL_CALL,
        tool_call_data,
    )

    # Workspace sandbox check — enforced at the governance layer before
    # the tool is dispatched.  Uses AGT ExecutionSandbox for path
    # containment (symlink resolution, is_relative_to).  Emits a
    # ``policy.denied`` event on failure; emits ``policy.allowed`` on
    # success only when ``governance.log_allowed`` is enabled.
    from surogates.governance.events import policy_denied_event
    from surogates.governance.policy import GovernanceGate, _PATH_ARGUMENT_MAP
    path_keys = _PATH_ARGUMENT_MAP.get(tool_name)
    if workspace_path and path_keys:
        _sandbox_gate = GovernanceGate()
        decision = _sandbox_gate.check(
            tool_name, tool_args, workspace_path=workspace_path,
        )
        if not decision.allowed:
            logger.warning(
                "Workspace sandbox blocked %s for session %s: %s",
                tool_name, session.id, decision.reason,
            )
            await store.emit_event(
                session.id,
                EventType.POLICY_DENIED,
                policy_denied_event(tool_name, decision.reason or ""),
            )

            result_content = json.dumps({
                "error": f"Blocked: {decision.reason}",
            })

            result_event_id = await store.emit_event(
                session.id,
                EventType.TOOL_RESULT,
                {
                    "tool_call_id": tool_call_id,
                    "name": tool_name,
                    "content": result_content,
                    "elapsed_ms": 0,
                },
            )
            await store.advance_harness_cursor(
                session.id,
                through_event_id=result_event_id,
                lease_token=lease.lease_token,
            )
            return {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_content,
            }

        if log_policy_allowed:
            await store.emit_event(
                session.id,
                EventType.POLICY_ALLOWED,
                {
                    "tool": tool_name,
                    "check": "workspace_sandbox",
                    "timestamp": time.time(),
                },
            )

    # --- Saga step tracking ---
    saga_step = None
    _active_saga_id: str | None = None
    if saga is not None and tool_name not in SAGA_EXCLUDED_TOOLS:
        from surogates.governance.events import saga_step_event as _sse
        from surogates.governance.saga.state_machine import StepState as _StepState

        current = saga.current_saga
        if current is not None:
            _active_saga_id = current.saga_id
            saga_step = saga.add_step(
                _active_saga_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=tool_args,
                checkpoint_hash=checkpoint_hash,
            )
            saga_step.transition(_StepState.EXECUTING)
            await store.emit_event(
                session.id,
                EventType.SAGA_STEP_BEGIN,
                _sse(
                    _active_saga_id,
                    saga_step.step_id,
                    tool_name,
                    _StepState.EXECUTING.value,
                    tool_call_id=tool_call_id,
                    arguments=sanitized_args,
                    checkpoint_hash=checkpoint_hash,
                ),
            )

    # Execute the tool, capturing errors as results (never crash the loop).
    # SANDBOX tools are dispatched to the sandbox pod where the real Python
    # tool handlers run (the sandbox image includes the surogates package).
    # HARNESS tools run in-process in the worker.
    start = time.monotonic()
    tool_failed = False
    try:
        from surogates.tools.router import TOOL_LOCATIONS, ToolLocation
        location = TOOL_LOCATIONS.get(tool_name, ToolLocation.SANDBOX)

        if location == ToolLocation.SANDBOX and sandbox_pool is not None:
            # Lazily provision or reuse the session's sandbox.
            from surogates.sandbox.base import SandboxSpec, Resource
            sandbox_spec = getattr(tenant, "sandbox_spec", None) or SandboxSpec()
            ws_bucket = session.config.get("workspace_bucket", "")
            if ws_bucket and not any(r.source_ref.startswith("s3://") for r in sandbox_spec.resources):
                sandbox_spec.resources.append(
                    Resource(source_ref=f"s3://{ws_bucket}", mount_path="/workspace"),
                )
            # Pass through skill-declared env vars to the sandbox pod.
            # Only matters at provisioning time — env is baked into the pod spec.
            if not sandbox_spec.env.get("_passthrough_done"):
                from surogates.tools.utils.env_passthrough import get_sandbox_env
                for k, v in get_sandbox_env().items():
                    sandbox_spec.env.setdefault(k, v)
                sandbox_spec.env["_passthrough_done"] = "1"
            await sandbox_pool.ensure(str(session.id), sandbox_spec)
            # Dispatch to the sandbox pod — runs the real Python tool handler
            # inside the sandbox via tool-executor.
            if isinstance(tool_args, dict):
                sandbox_args = dict(tool_args)
            else:
                sandbox_args = json.loads(tool_args) if tool_args else {}
            # Inject trace context so the sandbox can correlate its work.
            # The key is prefixed with underscore and stripped by the
            # tool-executor before the tool handler sees the arguments.
            trace = get_trace()
            if trace:
                sandbox_args["_trace_context"] = {
                    "trace_id": trace.trace_id,
                    "span_id": trace.span_id,
                }
            args_str = json.dumps(sandbox_args)
            # NOTE: The tool-executor inside the sandbox image must strip
            # _trace_context from args before dispatching to the handler.
            # It can use the values for its own structured logging.
            result_content = await sandbox_pool.execute(
                str(session.id), tool_name, args_str,
            )
        else:
            result_content = await tools.dispatch(
                tool_name,
                tool_args,
                session_id=str(session.id),
                agent_id=session.agent_id,
                tenant=tenant,
                session_store=store,
                redis=redis,
                budget=budget,
                memory_manager=memory_manager,
                sandbox_pool=sandbox_pool,
                workspace_path=workspace_path,
                api_client=api_client,
                session_factory=session_factory,
                tools=tools,
                tool_call_id=tool_call_id,
                lease_token=lease.lease_token,
            )
    except KeyError:
        tool_failed = True
        result_content = json.dumps({
            "error": f"Unknown tool: {tool_name}",
        })
    except Exception as exc:
        tool_failed = True
        # SandboxUnavailableError is the infra-level failure class --
        # provision/exec failed for a reason that affects every
        # sandbox-routed tool.  Surface a recognisable result so the LLM
        # stops dispatching the rest of the sandbox tool zoo.
        from surogates.sandbox.base import (
            SandboxUnavailableError,
            sandbox_unavailable_result,
        )
        if isinstance(exc, SandboxUnavailableError):
            from surogates.tools.router import TOOL_LOCATIONS, ToolLocation
            affected = sorted(
                t for t, loc in TOOL_LOCATIONS.items()
                if loc == ToolLocation.SANDBOX
            )
            logger.error(
                "Sandbox unavailable for session %s: %s", session.id, exc.reason,
            )
            result_content = sandbox_unavailable_result(
                exc.reason, tools_affected=affected,
            )
        else:
            logger.exception(
                "Tool %s failed for session %s", tool_name, session.id,
            )
            result_content = json.dumps({
                "error": f"Tool execution failed: {exc}",
            })
    elapsed_ms = int((time.monotonic() - start) * 1000)

    # --- Saga step commit / fail ---
    if saga_step is not None and _active_saga_id is not None:
        if tool_failed:
            saga_step.error = result_content
            saga_step.transition(_StepState.FAILED)
            await store.emit_event(
                session.id,
                EventType.SAGA_STEP_FAILED,
                _sse(_active_saga_id, saga_step.step_id, tool_name,
                     _StepState.FAILED.value, error=saga_step.error),
            )
        else:
            saga_step.execute_result = result_content
            saga_step.transition(_StepState.COMMITTED)
            await store.emit_event(
                session.id,
                EventType.SAGA_STEP_COMMITTED,
                _sse(_active_saga_id, saga_step.step_id, tool_name,
                     _StepState.COMMITTED.value),
            )

    # Subdirectory hints -- inject context discovered from new directories.
    if hint_tracker is not None:
        hints = hint_tracker.check_tool_call(tool_name, tool_args)
        if hints:
            result_content += hints

    # Layer 2: persist oversized results to disk instead of truncating.
    from surogates.tools.utils.tool_result_storage import maybe_persist_tool_result

    result_content = maybe_persist_tool_result(
        content=result_content,
        tool_name=tool_name,
        tool_use_id=tool_call_id,
    )

    # Sanitise result content — replace workspace paths with __WORKSPACE__.
    result_content = _sanitize_paths(result_content, workspace_path)

    # Emit TOOL_RESULT event.
    result_event_id = await store.emit_event(
        session.id,
        EventType.TOOL_RESULT,
        {
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result_content,
            "elapsed_ms": elapsed_ms,
        },
    )

    # Advance the cursor through the result event.
    try:
        await store.advance_harness_cursor(
            session.id, result_event_id, lease.lease_token,
        )
    except Exception:
        logger.warning(
            "Failed to advance cursor for session %s", session.id,
        )

    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": result_content,
    }
