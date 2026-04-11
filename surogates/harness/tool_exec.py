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

PARALLEL_SAFE_TOOLS: frozenset[str] = frozenset({
    "file_read",
    "read_file",
    "session_search",
    "skills_list",
    "web_search",
})

PATH_SCOPED_TOOLS: frozenset[str] = frozenset({
    "file_read",
    "file_write",
    "read_file",
    "write_file",
})

MAX_TOOL_WORKERS: int = 8

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


def should_parallelize(tool_calls: list[dict[str, Any]]) -> bool:
    """Decide whether a batch of tool calls can be executed concurrently.

    Rules:
    - Single tool call -> sequential (no benefit from parallelism).
    - Any tool in ``NEVER_PARALLEL_TOOLS`` -> sequential.
    - All tools in ``PARALLEL_SAFE_TOOLS`` -> parallel.
    - Tools in ``PATH_SCOPED_TOOLS`` -> parallel only if paths don't overlap.
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

    # All tools are known-safe?
    if all(n in PARALLEL_SAFE_TOOLS for n in names):
        return True

    # All tools are path-scoped?  Check for overlapping paths.
    if all(n in PATH_SCOPED_TOOLS for n in names):
        return paths_do_not_overlap(tool_calls)

    # Mixed bag of safe + path-scoped with no overlap is also OK.
    safe_or_path = PARALLEL_SAFE_TOOLS | PATH_SCOPED_TOOLS
    if all(n in safe_or_path for n in names):
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
) -> list[dict]:
    """Execute tool calls, choosing parallel vs sequential."""
    if should_parallelize(tool_calls):
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
) -> list[dict]:
    """Execute tool calls concurrently using asyncio.gather.

    Results are returned in the original tool-call order.
    If interrupted, remaining calls are skipped.
    """
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
            )

    tasks = [_guarded(tc) for tc in tool_calls]
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
) -> dict:
    """Execute a single tool call: emit events, dispatch, return result message."""
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
    # containment (symlink resolution, is_relative_to).
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

    # Execute the tool, capturing errors as results (never crash the loop).
    # SANDBOX tools are dispatched to the sandbox pod where the real Python
    # tool handlers run (the sandbox image includes the surogates package).
    # HARNESS tools run in-process in the worker.
    start = time.monotonic()
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
            await sandbox_pool.ensure(str(session.id), sandbox_spec)
            # Dispatch to the sandbox pod — runs the real Python tool handler
            # inside the sandbox via tool-executor.
            if isinstance(tool_args, dict):
                args_str = json.dumps(tool_args)
            else:
                args_str = tool_args if tool_args else "{}"
            result_content = await sandbox_pool.execute(
                str(session.id), tool_name, args_str,
            )
        else:
            result_content = await tools.dispatch(
                tool_name,
                tool_args,
                session_id=str(session.id),
                tenant=tenant,
                session_store=store,
                redis=redis,
                budget=budget,
                memory_manager=memory_manager,
                sandbox_pool=sandbox_pool,
                workspace_path=workspace_path,
                api_client=api_client,
            )
    except KeyError:
        result_content = json.dumps({
            "error": f"Unknown tool: {tool_name}",
        })
    except Exception as exc:
        logger.exception(
            "Tool %s failed for session %s", tool_name, session.id,
        )
        result_content = json.dumps({
            "error": f"Tool execution failed: {exc}",
        })
    elapsed_ms = int((time.monotonic() - start) * 1000)

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
