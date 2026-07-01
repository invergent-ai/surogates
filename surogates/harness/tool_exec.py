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
import os
import re
import time
from typing import TYPE_CHECKING, Any, Callable

from surogates.session.events import EventType
from surogates.harness.message_utils import make_skipped_tool_result
from surogates.harness.tool_guardrails import (
    ToolGuardrailDecision,
    ToolGuardrails,
    append_toolguard_guidance,
    canonical_tool_args,
    toolguard_synthetic_result,
)
from surogates.tools.coerce import coerce_tool_args
from surogates.storage.tenant import boundary_workspace_prefix

# ---------------------------------------------------------------------------
# Path sanitisation — replace workspace absolute paths with __WORKSPACE__
# so that real filesystem paths never leak to the frontend via SSE events.
# ---------------------------------------------------------------------------

_WORKSPACE_TOKEN = "__WORKSPACE__"


_WORKSPACE_MOUNT_PATH = "/workspace"


def build_workspace_source_ref(
    *,
    storage_bucket: str,
    workspace_prefix: str,
) -> str:
    """Build the ``s3://`` URI used to mount a sandbox workspace.

    *workspace_prefix* is the fully-resolved physical prefix (already
    layered with the agent's ``storage_key_prefix`` and either the
    boundary-partitioned or per-session segment), so this only prepends
    the bucket.
    """
    return f"s3://{storage_bucket}/{workspace_prefix}"


def _build_session_sandbox_spec(
    session: Any,
    tenant: Any,
    sandbox_owner: str,
) -> Any:
    """Build the SandboxSpec used to provision *session*'s sandbox.

    Delegation children and loop iterations share the root's sandbox
    (see :func:`surogates.sandbox.pool.sandbox_session_key`) so the
    sub-agent or next tick can see the files already produced.  The
    workspace mount must be derived from *sandbox_owner* (the root)
    rather than ``session.id`` — otherwise a reprovision under a child
    would mount an empty per-child prefix.

    The tenant's baseline spec is copied before mutation so that
    appending the workspace mount or env passthrough never bleeds
    across sessions sharing the same tenant context.
    """
    from surogates.sandbox.base import Resource, SandboxSpec, default_sandbox_spec
    from surogates.tools.utils.env_passthrough import get_sandbox_env

    baseline = getattr(tenant, "sandbox_spec", None)
    if baseline is None:
        sandbox_spec = default_sandbox_spec()
    else:
        # Copy fields explicitly so the caller's baseline spec stays
        # immutable across sessions sharing the same tenant context.
        # ``Resource`` is a frozen dataclass; reusing the list elements
        # is safe.
        sandbox_spec = SandboxSpec(
            image=baseline.image,
            resources=list(baseline.resources),
            cpu=baseline.cpu,
            memory=baseline.memory,
            cpu_limit=baseline.cpu_limit,
            memory_limit=baseline.memory_limit,
            timeout=baseline.timeout,
            env=dict(baseline.env),
        )

    storage_bucket = session.config.get("storage_bucket", "")
    has_workspace_mount = any(
        r.mount_path == _WORKSPACE_MOUNT_PATH for r in sandbox_spec.resources
    )
    if storage_bucket and not has_workspace_mount:
        sandbox_spec.resources.append(
            Resource(
                source_ref=build_workspace_source_ref(
                    storage_bucket=storage_bucket,
                    workspace_prefix=boundary_workspace_prefix(
                        session.config,
                        session,
                        sandbox_owner,
                    ),
                ),
                mount_path=_WORKSPACE_MOUNT_PATH,
            ),
        )
    # Pass through skill-declared env vars to the sandbox pod.  Only
    # matters at provisioning time — env is baked into the pod spec.
    if not sandbox_spec.env.get("_passthrough_done"):
        for k, v in get_sandbox_env().items():
            sandbox_spec.env.setdefault(k, v)
        sandbox_spec.env["_passthrough_done"] = "1"
    # Docker backend needs the root session key (for labels + stale-container
    # cleanup) and a host-bindable workspace path. K8sSandbox ignores both.
    # Delegation children inherit the root's workspace_path via
    # create_child_session, so reading session.config here is correct for them.
    sandbox_spec.session_id = sandbox_owner
    sandbox_spec.workspace_path = session.config.get("workspace_path")
    return sandbox_spec


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

def _truncate_args(arguments: Any, limit: int = 500) -> str:
    raw = json.dumps(arguments, default=str, sort_keys=True)
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 3)] + "..."

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from surogates.governance.saga.orchestrator import SagaOrchestrator
    from surogates.browser.control import BrowserControlStore
    from surogates.browser.pool import BrowserPool
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

NEVER_PARALLEL_TOOLS: frozenset[str] = frozenset({"ask_user_question"})

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

# Tools that can run concurrently with the LLM stream — read-only tools
# plus parallel-safe sandbox tools.  Used by the streaming executor to
# decide whether to dispatch a tool eagerly while the model is still
# generating.  Eager dispatch must be safe to cancel mid-flight, so
# side-effecting tools that allocate durable state (delegation tools
# create child sessions in the DB) are excluded even though they're
# safe to batch-dispatch concurrently after the stream completes —
# see :data:`BATCH_PARALLEL_TOOLS`.
PARALLEL_TOOLS: frozenset[str] = CONCURRENCY_SAFE_TOOLS | SANDBOX_PARALLEL_TOOLS


def is_parallelizable(tool_name: str) -> bool:
    """Return ``True`` if *tool_name* can run concurrently with other parallelizable tools."""
    return tool_name in PARALLEL_TOOLS


# Delegation tools that spawn child sessions.  Each call creates an
# independent session with its own lease, event log, and budget, so a
# batch can safely fan out concurrently after the LLM stream completes.
# Heavyskill (parallel-reason-then-synthesize) relies on this for
# ``delegate_task``; ``spawn_worker`` already documents parallel
# fan-out in its schema.  Excluded from :data:`PARALLEL_TOOLS` so the
# streaming executor doesn't dispatch them eagerly — a discarded
# stream would orphan child sessions created speculatively.
DELEGATION_TOOLS: frozenset[str] = frozenset({
    "delegate_task",
    "spawn_worker",
})

# All tools that ``should_parallelize`` may dispatch concurrently after
# the stream completes.  Superset of :data:`PARALLEL_TOOLS` plus
# delegation tools.  Used only for batched (post-stream) dispatch.
BATCH_PARALLEL_TOOLS: frozenset[str] = PARALLEL_TOOLS | DELEGATION_TOOLS

MAX_TOOL_WORKERS: int = 8

# Read-only tools that never participate in saga tracking — they have no
# side effects to compensate.
SAGA_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "ask_user_question",
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


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape literal control characters inside JSON string values."""
    out: list[str] = []
    in_string = False
    idx = 0
    while idx < len(raw):
        ch = raw[idx]
        if in_string:
            if ch == "\\" and idx + 1 < len(raw):
                out.append(ch)
                out.append(raw[idx + 1])
                idx += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        idx += 1
    return "".join(out)


def tool_call_arguments_look_incomplete(raw_args: str) -> bool:
    """Return True when argument JSON appears truncated mid-structure."""
    raw = raw_args.strip() if isinstance(raw_args, str) else ""
    if not raw:
        return False
    try:
        json.loads(raw)
        return False
    except json.JSONDecodeError:
        pass

    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in raw:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return bool(stack)
            stack.pop()
    return in_string or bool(stack)


def repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    """Repair common model-generated JSON damage in tool-call arguments."""
    raw = raw_args.strip() if isinstance(raw_args, str) else ""
    if not raw or raw == "None":
        return "{}"

    try:
        parsed = json.loads(raw, strict=False)
        return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    candidates: list[str] = []
    base = _escape_invalid_chars_in_json_strings(raw)
    candidates.append(base)
    candidates.append(re.sub(r",\s*([}\]])", r"\1", base))

    for candidate in list(candidates):
        fixed = candidate
        open_curly = fixed.count("{") - fixed.count("}")
        open_bracket = fixed.count("[") - fixed.count("]")
        if open_curly > 0:
            fixed += "}" * open_curly
        if open_bracket > 0:
            fixed += "]" * open_bracket
        candidates.append(fixed)

    for candidate in list(candidates):
        fixed = candidate
        for _ in range(50):
            try:
                parsed = json.loads(fixed)
                logger.warning(
                    "Repaired malformed tool_call arguments for %s",
                    tool_name,
                )
                return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
            except json.JSONDecodeError as exc:
                if exc.msg.startswith("Extra data"):
                    fixed = fixed[:exc.pos].rstrip()
                    continue
                if fixed.endswith(("}", "]")):
                    fixed = fixed[:-1].rstrip()
                    continue
                break

    return raw


def _parse_tool_args_for_guardrail(tc: dict[str, Any]) -> dict[str, Any]:
    fn = tc.get("function", {})
    tool_name = fn.get("name", "")
    raw_args = fn.get("arguments", "")
    try:
        repaired = repair_tool_call_arguments(raw_args, tool_name)
        parsed = json.loads(repaired) if repaired else {}
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


async def _emit_guardrail_tool_result(
    tc: dict[str, Any],
    *,
    decision: ToolGuardrailDecision,
    session: Session,
    lease: SessionLease,
    store: SessionStore,
) -> dict[str, str]:
    fn = tc.get("function", {})
    tool_name = fn.get("name", "")
    tool_call_id = tc.get("id", "")
    result_content = toolguard_synthetic_result(decision, tool_name=tool_name)

    await store.emit_event(
        session.id,
        EventType.TOOL_CALL,
        {
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "arguments": {},
        },
    )
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


def _batch_has_duplicate_signatures(tool_calls: list[dict[str, Any]]) -> bool:
    """Return ``True`` if any two calls in *tool_calls* share name + args.

    Repeated identical calls in a single batch are a model loop pattern that
    :class:`ToolGuardrails` is designed to detect via successive
    ``after_call``/``before_call`` state updates.  Parallel dispatch would
    fire all duplicates before any after_call ran, defeating that.  When
    duplicates are present, fall back to sequential so the guardrail can
    observe each result and block the loop.
    """
    seen: set[tuple[str, str]] = set()
    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "")
        args_raw = fn.get("arguments", "")
        try:
            parsed = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            canon = canonical_tool_args(parsed if isinstance(parsed, dict) else {})
        except (json.JSONDecodeError, TypeError, ValueError):
            canon = str(args_raw)
        key = (name, canon)
        if key in seen:
            return True
        seen.add(key)
    return False


def should_parallelize(tool_calls: list[dict[str, Any]]) -> bool:
    """Decide whether a batch of tool calls can be executed concurrently.

    Rules:
    - Single tool call -> sequential (no benefit from parallelism).
    - Any tool in ``NEVER_PARALLEL_TOOLS`` -> sequential.
    - All tools in ``BATCH_PARALLEL_TOOLS`` (read-only + sandbox +
      delegation) -> parallel.
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

    # All tools are parallelizable (read-only + sandbox + delegation)?
    if all(n in BATCH_PARALLEL_TOOLS for n in names):
        return True

    # Path-scoped tools can run in parallel if paths don't overlap.
    all_parallel_or_path = BATCH_PARALLEL_TOOLS | PATH_SCOPED_TOOLS
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
    credential_vault: Any | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    storage: Any | None = None,
    api_client: Any | None = None,
    session_factory: Any | None = None,
    llm_client: Any | None = None,
    model: str | None = None,
    vision_llm_client: Any | None = None,
    vision_model: str = "",
    summary_llm_client: Any | None = None,
    summary_model: str | None = None,
    media_gen: Any | None = None,
    saga: SagaOrchestrator | None = None,
    log_policy_allowed: bool = False,
    tool_guardrails: ToolGuardrails | None = None,
    bundle: Any | None = None,
    turn_gate: Any | None = None,
) -> list[dict]:
    """Execute tool calls, choosing parallel vs sequential.

    The parallel branch is taken when:

    - :func:`should_parallelize` accepts the batch (>1 call, all in
      :data:`BATCH_PARALLEL_TOOLS`, path-scoped tools don't overlap), AND
    - either no guardrails are active, or the batch has no duplicate
      ``(tool_name, args)`` signatures.  Duplicate signatures within one
      batch are a model loop pattern that :class:`ToolGuardrails` is
      designed to break via successive ``after_call``/``before_call``
      state updates — parallel dispatch would fire all duplicates before
      any after_call ran, defeating that, so duplicates force sequential
      so the guardrail can block them, AND
    - when *saga* is active, parallel execution is restricted to
      concurrency-safe (read-only) tools only — side-effecting tools
      must run sequentially so saga compensation has deterministic step
      ordering.  Read-only tools have no side effects to compensate,
      so they can still benefit from parallelism.
    """
    if (
        should_parallelize(tool_calls)
        and (tool_guardrails is None or not _batch_has_duplicate_signatures(tool_calls))
        and (saga is None or _all_concurrency_safe(tool_calls))
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
            credential_vault=credential_vault,
            browser_pool=browser_pool,
            browser_control=browser_control,
            storage=storage,
            api_client=api_client,
            session_factory=session_factory,
            llm_client=llm_client,
            model=model,
            vision_llm_client=vision_llm_client,
            vision_model=vision_model,
            summary_llm_client=summary_llm_client,
            summary_model=summary_model,
            media_gen=media_gen,
            tool_guardrails=tool_guardrails,
            log_policy_allowed=log_policy_allowed,
            bundle=bundle,
            turn_gate=turn_gate,
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
        credential_vault=credential_vault,
        browser_pool=browser_pool,
        browser_control=browser_control,
        storage=storage,
        api_client=api_client,
        session_factory=session_factory,
        llm_client=llm_client,
        model=model,
        vision_llm_client=vision_llm_client,
        vision_model=vision_model,
        summary_llm_client=summary_llm_client,
        summary_model=summary_model,
        media_gen=media_gen,
        saga=saga,
        log_policy_allowed=log_policy_allowed,
        tool_guardrails=tool_guardrails,
        bundle=bundle,
        turn_gate=turn_gate,
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
    credential_vault: Any | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    storage: Any | None = None,
    api_client: Any | None = None,
    session_factory: Any | None = None,
    llm_client: Any | None = None,
    model: str | None = None,
    vision_llm_client: Any | None = None,
    vision_model: str = "",
    summary_llm_client: Any | None = None,
    summary_model: str | None = None,
    media_gen: Any | None = None,
    saga: SagaOrchestrator | None = None,
    log_policy_allowed: bool = False,
    tool_guardrails: ToolGuardrails | None = None,
    bundle: Any | None = None,
    turn_gate: Any | None = None,
) -> list[dict]:
    """Execute tool calls one at a time, emitting events for each."""
    results: list[dict] = []

    for tc in tool_calls:
        # --- Interrupt check before each tool call ---
        if interrupt_check():
            results.append(make_skipped_tool_result(tc))
            continue

        guardrail_args = _parse_tool_args_for_guardrail(tc)
        tool_name = tc.get("function", {}).get("name", "")
        if tool_guardrails is not None:
            before = tool_guardrails.before_call(tool_name, guardrail_args)
            if not before.allows_execution:
                result_msg = await _emit_guardrail_tool_result(
                    tc,
                    decision=before,
                    session=session,
                    lease=lease,
                    store=store,
                )
                results.append(result_msg)
                if before.should_halt:
                    break
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
            credential_vault=credential_vault,
            browser_pool=browser_pool,
            browser_control=browser_control,
            storage=storage,
            api_client=api_client,
            session_factory=session_factory,
            llm_client=llm_client,
            model=model,
            vision_llm_client=vision_llm_client,
            vision_model=vision_model,
            summary_llm_client=summary_llm_client,
            summary_model=summary_model,
            media_gen=media_gen,
            saga=saga,
            log_policy_allowed=log_policy_allowed,
            bundle=bundle,
            turn_gate=turn_gate,
        )
        if tool_guardrails is not None:
            after = tool_guardrails.after_call(
                tool_name,
                guardrail_args,
                result_msg.get("content", ""),
            )
            result_msg = {
                **result_msg,
                "content": append_toolguard_guidance(
                    result_msg.get("content", ""),
                    after,
                ),
            }
        results.append(result_msg)
        if tool_guardrails is not None and tool_guardrails.halt_decision is not None:
            break

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
    credential_vault: Any | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    storage: Any | None = None,
    api_client: Any | None = None,
    session_factory: Any | None = None,
    llm_client: Any | None = None,
    model: str | None = None,
    vision_llm_client: Any | None = None,
    vision_model: str = "",
    summary_llm_client: Any | None = None,
    summary_model: str | None = None,
    media_gen: Any | None = None,
    tool_guardrails: ToolGuardrails | None = None,
    log_policy_allowed: bool = False,
    bundle: Any | None = None,
    turn_gate: Any | None = None,
) -> list[dict]:
    """Execute tool calls concurrently using asyncio.gather.

    Results are returned in the original tool-call order.
    If interrupted, remaining calls are skipped.

    Each concurrent tool runs inside a copied :mod:`contextvars` context
    so that ``new_span()`` inside ``execute_single_tool`` does not clobber
    sibling tasks' trace state.

    Guardrail integration mirrors :func:`execute_tool_calls_sequential`:
    ``before_call`` runs sequentially over the batch before dispatch (so a
    blocking decision skips the affected call, and ``should_halt`` truncates
    the batch); allowed calls fan out in parallel; ``after_call`` runs
    sequentially over the results in batch order to keep failure-counter
    updates deterministic.
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
                credential_vault=credential_vault,
                browser_pool=browser_pool,
                browser_control=browser_control,
                storage=storage,
                api_client=api_client,
                session_factory=session_factory,
                llm_client=llm_client,
                model=model,
                vision_llm_client=vision_llm_client,
                vision_model=vision_model,
                summary_llm_client=summary_llm_client,
                summary_model=summary_model,
                media_gen=media_gen,
                _parent_trace=parent_trace,
                log_policy_allowed=log_policy_allowed,
                bundle=bundle,
                turn_gate=turn_gate,
            )

    # Pre-pass: apply ``before_call`` sequentially so guardrail blocking
    # decisions are observed before any parallel work starts.  Build
    # ``results`` in original order; allowed calls get a None placeholder
    # that's filled in after the parallel gather completes.
    results: list[dict | None] = []
    parallel_slots: list[tuple[int, dict[str, Any]]] = []
    halt_in_pre_pass = False

    for tc in tool_calls:
        if interrupt_check():
            results.append(make_skipped_tool_result(tc))
            continue
        if tool_guardrails is not None:
            guardrail_args = _parse_tool_args_for_guardrail(tc)
            tool_name = tc.get("function", {}).get("name", "")
            before = tool_guardrails.before_call(tool_name, guardrail_args)
            if not before.allows_execution:
                results.append(
                    await _emit_guardrail_tool_result(
                        tc,
                        decision=before,
                        session=session,
                        lease=lease,
                        store=store,
                    )
                )
                if before.should_halt:
                    halt_in_pre_pass = True
                    break
                continue
        parallel_slots.append((len(results), tc))
        results.append(None)

    # Parallel dispatch of allowed calls.
    if parallel_slots:
        asyncio_loop = asyncio.get_running_loop()
        tasks = [
            asyncio_loop.create_task(_guarded(tc), context=_cv.copy_context())
            for _, tc in parallel_slots
        ]
        parallel_results = await asyncio.gather(*tasks)
        for (idx, _), result in zip(parallel_slots, parallel_results):
            results[idx] = result

    # Post-pass: apply ``after_call`` and any guidance suffix in batch
    # order so per-tool failure counters update deterministically.  Stops
    # appending guidance once a halt decision fires (matches the
    # sequential path's ``break``), but does not discard already-executed
    # results — the outer loop will see ``halt_decision`` and stop.
    if tool_guardrails is not None and not halt_in_pre_pass:
        for idx, tc in parallel_slots:
            result = results[idx]
            if result is None:
                continue
            tool_name = tc.get("function", {}).get("name", "")
            guardrail_args = _parse_tool_args_for_guardrail(tc)
            after = tool_guardrails.after_call(
                tool_name,
                guardrail_args,
                result.get("content", ""),
            )
            results[idx] = {
                **result,
                "content": append_toolguard_guidance(result.get("content", ""), after),
            }
            if tool_guardrails.halt_decision is not None:
                break

    return [r for r in results if r is not None]


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
    credential_vault: Any | None = None,
    browser_pool: BrowserPool | None = None,
    browser_control: BrowserControlStore | None = None,
    storage: Any | None = None,
    api_client: Any | None = None,
    session_factory: Any | None = None,
    llm_client: Any | None = None,
    model: str | None = None,
    vision_llm_client: Any | None = None,
    vision_model: str = "",
    summary_llm_client: Any | None = None,
    summary_model: str | None = None,
    media_gen: Any | None = None,
    _parent_trace: Any | None = None,
    saga: SagaOrchestrator | None = None,
    log_policy_allowed: bool = False,
    bundle: Any | None = None,
    turn_gate: Any | None = None,
) -> dict:
    """Execute a single tool call: emit events, dispatch, return result message.

    When *log_policy_allowed* is True, every governance check that passes
    also emits a ``policy.allowed`` event.  Off by default because each
    successful ``tool.call`` is already an implicit allow; enable for
    compliance audits that require an explicit per-decision record.
    """
    from surogates.trace import get_trace, new_span

    # Each tool call gets its own child span for fine-grained tracing.
    # When called from concurrent execution, _parent_trace is the
    # captured parent so siblings don't clobber each other.
    new_span(_parent_trace)

    fn = tc.get("function", {})
    tool_name: str = fn.get("name", "")
    tool_args_raw: str = fn.get("arguments", "")
    tool_call_id: str = tc.get("id", "")

    # Parse arguments.
    parse_error: json.JSONDecodeError | None = None
    try:
        repaired_args = repair_tool_call_arguments(tool_args_raw, tool_name)
        tool_args = json.loads(repaired_args) if repaired_args else {}
    except json.JSONDecodeError as exc:
        parse_error = exc
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

    _call_event_id = await store.emit_event(
        session.id,
        EventType.TOOL_CALL,
        tool_call_data,
    )

    if parse_error is not None:
        result_content = json.dumps(
            {
                "error": f"Invalid JSON arguments: {parse_error}",
                "tool": tool_name,
                "detail": str(parse_error),
            },
            ensure_ascii=False,
        )
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
            if decision.overridable:
                await store.emit_event(
                    session.id,
                    EventType.INBOX_GOVERNANCE_GATE,
                    {
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments_excerpt": _truncate_args(sanitized_args),
                        "deny_reason": decision.reason or "",
                        "policy_id": decision.policy_id,
                    },
                )

            result_payload = {"error": f"Blocked: {decision.reason}"}
            if decision.overridable:
                result_payload["error"] = "policy_blocked_overridable"
                result_payload["message"] = decision.reason
                result_payload["tool_call_id"] = tool_call_id
            result_content = json.dumps(result_payload)

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

    # --- Session tool allow-list check ---
    # When the session config declares a ``tool_allow_list``, any tool
    # outside the list is rejected before dispatch.  A missing or empty
    # list means "no per-session restriction"; a non-empty list enforces
    # strict membership.  This is a general harness primitive — any
    # code path that creates a session may populate the list (today the
    # public-website bootstrap is one such path; future channels or
    # programmatic API callers can use the same hook).  We emit
    # ``policy.denied`` for auditability and a ``tool.result`` carrying
    # the explanation so the LLM sees the refusal inline (without which
    # the model would keep calling the same forbidden tool on the next
    # turn).
    allow_list = session.config.get("tool_allow_list") if session.config else None
    if allow_list and tool_name not in allow_list:
        reason = (
            f"Tool '{tool_name}' is not in this session's allow-list. "
            f"Allowed: {sorted(allow_list)}"
        )
        logger.warning(
            "Session allow-list blocked %s for session %s", tool_name, session.id,
        )
        await store.emit_event(
            session.id,
            EventType.POLICY_DENIED,
            policy_denied_event(tool_name, reason),
        )
        result_content = json.dumps({"error": reason})
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
        # MCP tools are remote calls (HTTP/stdio) dispatched by the
        # in-process MCP client; they never need a sandbox pod. Without
        # this short-circuit the dict default (SANDBOX) tries to spin up
        # a k8s namespace + S3 secret for every `mcp__*` call.
        if tool_name.startswith("mcp__"):
            location = ToolLocation.HARNESS
        else:
            location = TOOL_LOCATIONS.get(tool_name, ToolLocation.SANDBOX)

        # ── read_file image branch ─────────────────────────────────────
        # Image analysis can't happen inside the sandbox because the
        # sandbox process has no LLM clients or vision configuration.
        # When read_file targets an image path, redirect to vision_analyze
        # in-process and reshape the response as a read_file envelope so
        # the LLM never sees vision_analyze unless it called it directly.
        image_dispatched = False
        if tool_name == "read_file" and isinstance(tool_args, dict):
            from surogates.tools.builtin.file_ops import IMAGE_EXTENSIONS
            image_path_arg = tool_args.get("path")
            if isinstance(image_path_arg, str) and image_path_arg:
                _ext = os.path.splitext(image_path_arg)[1].lower()
                if _ext in IMAGE_EXTENSIONS:
                    from surogates.harness.image_read import handle_image_read
                    result_content = await handle_image_read(
                        path=image_path_arg,
                        arguments=tool_args,
                        dispatch=tools.dispatch,
                        kwargs={
                            "session_id": str(session.id),
                            "agent_id": session.agent_id,
                            "tenant": tenant,
                            "session_store": store,
                            "redis": redis,
                            "budget": budget,
                            "memory_manager": memory_manager,
                            "sandbox_pool": sandbox_pool,
                            "browser_pool": browser_pool,
                            "browser_control": browser_control,
                            "storage": storage,
                            "workspace_path": workspace_path,
                            "api_client": api_client,
                            "session_factory": session_factory,
                            "llm_client": llm_client,
                            "model": model or getattr(session, "model", None),
                            "vision_llm_client": vision_llm_client,
                            "vision_model": vision_model,
                            "summary_llm_client": summary_llm_client,
                            "summary_model": summary_model,
                            "media_gen": media_gen,
                            "tools": tools,
                            "tool_call_id": tool_call_id,
                            "lease_token": lease.lease_token,
                            "session_config": session.config,
                        },
                    )
                    image_dispatched = True

        if image_dispatched:
            pass  # result_content already set by the image branch.
        elif location == ToolLocation.SANDBOX and sandbox_pool is not None:
            from surogates.sandbox.pool import sandbox_session_key
            sandbox_owner = sandbox_session_key(session)
            sandbox_spec = _build_session_sandbox_spec(session, tenant, sandbox_owner)
            await sandbox_pool.ensure(sandbox_owner, sandbox_spec)
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
                sandbox_owner, tool_name, args_str,
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
                credential_vault=credential_vault,
                browser_pool=browser_pool,
                browser_control=browser_control,
                storage=storage,
                workspace_path=workspace_path,
                api_client=api_client,
                session_factory=session_factory,
                llm_client=llm_client,
                model=model or getattr(session, "model", None),
                vision_llm_client=vision_llm_client,
                vision_model=vision_model,
                summary_llm_client=summary_llm_client,
                summary_model=summary_model,
                media_gen=media_gen,
                tools=tools,
                tool_call_id=tool_call_id,
                lease_token=lease.lease_token,
                session_config=session.config,
                bundle=bundle,
                turn_gate=turn_gate,
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

    # Sanitise the event payload — frontend SSE consumers must not see
    # real filesystem paths.  The LLM still receives the raw
    # ``result_content`` so its mental map matches the sandbox: when a
    # tool reports a path, the model can pass that exact path back into
    # the next call.  Sanitising the LLM-facing string created the
    # ``__WORKSPACE__`` confusion that triggered cascades of broken
    # commands (literal ``$HOME`` directories, ``__WORKSPACE__`` treated
    # as a real path, double-substitution of ``__WORKSPACE__/__WORKSPACE__``).
    sanitized_content = _sanitize_paths(result_content, workspace_path)

    # Emit TOOL_RESULT event.
    result_event_id = await store.emit_event(
        session.id,
        EventType.TOOL_RESULT,
        {
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": sanitized_content,
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
