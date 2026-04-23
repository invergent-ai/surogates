"""Streaming tool executor — overlaps tool execution with LLM streaming.

While the LLM is still generating its response, tool_use blocks that are
fully received can begin executing if they are concurrency-safe (read-only).
This overlaps tool execution with LLM generation, reducing end-to-end
latency — especially valuable when sandbox tools require K8s pod
provisioning (5-30s) that can happen during streaming.

The executor maintains strict insertion-order result delivery and implements
a sibling abort mechanism: errors from ``terminal`` cancel all
concurrently-running sibling tools.

Concurrency rules:
- Concurrency-safe tools can run in parallel with each other.
- A non-concurrent tool blocks the queue until it finishes.
- A non-concurrent tool cannot start while concurrent tools are running.

Usage in the harness loop::

    executor = StreamingToolExecutor(session=session, lease=lease, ...)

    # Pass executor.add_tool as callback to LLM streaming
    assistant_msg, usage = await call_llm_with_retry(
        ..., on_tool_call_complete=executor.add_tool,
    )

    # After streaming, drain remaining results
    if executor.has_tools:
        results = await executor.get_all_results()
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from surogates.session.events import EventType

if TYPE_CHECKING:
    from collections.abc import Callable

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

# Concurrency classification constants live in tool_exec (single source of
# truth for all tool execution policy).  Import here for use by the executor.
from surogates.harness.tool_exec import (  # noqa: E402 — after TYPE_CHECKING block
    SIBLING_ABORT_TOOLS,
    is_parallelizable,
)


# ---------------------------------------------------------------------------
# Tracked tool state
# ---------------------------------------------------------------------------


class ToolStatus(str, Enum):
    """Lifecycle states for a tracked tool execution."""

    QUEUED = "queued"
    EXECUTING = "executing"
    COMPLETED = "completed"


@dataclass
class TrackedTool:
    """A tool call being tracked by the streaming executor."""

    tool_call: dict[str, Any]
    is_parallelizable: bool = False
    status: ToolStatus = ToolStatus.QUEUED
    task: asyncio.Task[None] | None = None
    result: dict[str, Any] | None = None
    errored: bool = False
    started_at: float = 0.0
    completed_at: float = 0.0


# ---------------------------------------------------------------------------
# StreamingToolExecutor
# ---------------------------------------------------------------------------


class StreamingToolExecutor:
    """Manages concurrent tool execution during LLM streaming.

    Tools are added via :meth:`add_tool` (typically called from a streaming
    callback as tool_use blocks complete).  Concurrency-safe tools start
    executing immediately.  Non-concurrent tools are queued until all
    concurrent tools finish.

    After streaming completes, :meth:`get_all_results` waits for all
    executions to finish and returns results in insertion order.

    Parameters match :func:`~surogates.harness.tool_exec.execute_single_tool`
    so the executor can delegate tool execution directly.
    """

    def __init__(
        self,
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
    ) -> None:
        self._session = session
        self._lease = lease
        self._store = store
        self._tools = tools
        self._tenant = tenant
        self._interrupt_check = interrupt_check
        self._redis = redis
        self._budget = budget
        self._log_policy_allowed = log_policy_allowed
        self._memory_manager = memory_manager
        self._hint_tracker = hint_tracker
        self._sandbox_pool = sandbox_pool
        self._api_client = api_client
        self._session_factory = session_factory
        self._saga = saga

        self._tracked: list[TrackedTool] = []
        self._sibling_aborted: bool = False
        self._discarded: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def has_tools(self) -> bool:
        """Return ``True`` if at least one tool has been added."""
        return len(self._tracked) > 0

    @property
    def tool_count(self) -> int:
        """Return the number of tracked tools."""
        return len(self._tracked)

    def add_tool(self, tool_call: dict[str, Any]) -> None:
        """Register a tool call and start executing if concurrency-safe.

        Called synchronously from the streaming callback.  Must be called
        from within a running event loop (which is guaranteed when called
        from an async streaming context).
        """
        if self._discarded:
            return

        fn = tool_call.get("function", {})
        tool_name = fn.get("name", "")

        parallel = is_parallelizable(tool_name)
        tracked = TrackedTool(
            tool_call=tool_call,
            is_parallelizable=parallel,
        )
        self._tracked.append(tracked)

        if self._can_execute(tracked):
            self._start_execution(tracked)

    async def get_all_results(self) -> list[dict[str, Any]]:
        """Wait for all tools to complete and return results in insertion order.

        Starts any queued tools that can execute now, then waits for all
        tasks to finish.  Tools that were never executed (due to sibling
        abort, discard, or interrupt) get synthetic "skipped" results.
        """
        self._process_queue()

        # Wait for all tasks, including ones created dynamically by
        # _process_queue (called from _run_tool's finally block when a
        # task completes).  Each gather wave collects tasks created so
        # far; completing tasks may spawn new ones via _process_queue.
        seen: set[asyncio.Task[None]] = set()
        while True:
            current = {
                t.task for t in self._tracked
                if t.task is not None and t.task not in seen
            }
            if not current:
                break
            seen.update(current)
            await asyncio.gather(*current, return_exceptions=True)

        # Fill in results for tools that never executed.
        from surogates.harness.message_utils import make_skipped_tool_result

        for tool in self._tracked:
            if tool.result is None:
                tool.result = make_skipped_tool_result(tool.tool_call)

        return [t.result for t in self._tracked]  # type: ignore[misc]

    def discard(self) -> None:
        """Cancel all in-flight executions.

        Called when the streaming response is discarded (e.g., model
        fallback mid-stream) or when the harness needs to abort.
        """
        self._discarded = True
        for tool in self._tracked:
            if tool.task is not None and not tool.task.done():
                tool.task.cancel()

    @property
    def stats(self) -> dict[str, Any]:
        """Return execution statistics for logging and telemetry."""
        completed = [t for t in self._tracked if t.status == ToolStatus.COMPLETED]
        concurrent_count = sum(1 for t in self._tracked if t.is_parallelizable)
        overlapped = sum(
            1 for t in self._tracked
            if t.is_parallelizable and t.status == ToolStatus.COMPLETED
        )
        return {
            "total": len(self._tracked),
            "concurrent": concurrent_count,
            "sequential": len(self._tracked) - concurrent_count,
            "completed": len(completed),
            "overlapped_with_streaming": overlapped,
            "errored": sum(1 for t in self._tracked if t.errored),
            "sibling_aborted": self._sibling_aborted,
        }

    # ------------------------------------------------------------------
    # Internal concurrency control
    # ------------------------------------------------------------------

    def _can_execute(self, tool: TrackedTool) -> bool:
        """Check if a tool can start executing now.

        Rules:
        - Aborted/discarded/interrupted → no.
        - No tools currently executing → yes.
        - Tool is concurrent-safe AND all executing tools are concurrent-safe → yes.
        - Otherwise → no (must wait for executing tools to finish).
        """
        if self._sibling_aborted or self._discarded:
            return False
        if self._interrupt_check():
            return False

        executing = [t for t in self._tracked if t.status == ToolStatus.EXECUTING]
        if not executing:
            return True
        if tool.is_parallelizable and all(t.is_parallelizable for t in executing):
            return True
        return False

    def _start_execution(self, tool: TrackedTool) -> None:
        """Create an asyncio task to execute the tool."""
        tool.status = ToolStatus.EXECUTING
        tool.started_at = time.monotonic()
        # Each task gets its own contextvars copy so trace spans
        # don't clobber each other across concurrent tools.
        tool.task = asyncio.get_running_loop().create_task(
            self._run_tool(tool),
            context=contextvars.copy_context(),
        )

    async def _run_tool(self, tool: TrackedTool) -> None:
        """Execute a single tool and update its state.

        On :class:`asyncio.CancelledError` (typically raised by
        :meth:`_abort_siblings` after a peer fails), the cancellation
        usually arrives *after* the call's ``TOOL_CALL`` event has been
        emitted but *before* its ``TOOL_RESULT`` event.  We therefore
        synthesise a skipped result and write it to the event log so the
        UI, SSE consumers, and replay logic see a coherent
        call/result pair instead of a permanently pending tool call.
        """
        from surogates.harness.tool_exec import execute_single_tool

        try:
            result = await execute_single_tool(
                tool.tool_call,
                session=self._session,
                lease=self._lease,
                store=self._store,
                tools=self._tools,
                tenant=self._tenant,
                redis=self._redis,
                budget=self._budget,
                memory_manager=self._memory_manager,
                hint_tracker=self._hint_tracker,
                sandbox_pool=self._sandbox_pool,
                api_client=self._api_client,
                session_factory=self._session_factory,
                saga=self._saga,
                log_policy_allowed=self._log_policy_allowed,
            )
            tool.result = result
            tool.errored = _is_error_result(result)
        except asyncio.CancelledError:
            from surogates.harness.message_utils import make_skipped_tool_result
            tool.result = make_skipped_tool_result(
                tool.tool_call, reason="cancelled (sibling error)",
            )
            tool.errored = True
            await self._emit_cancelled_result_event(tool)
        except Exception as exc:
            logger.exception(
                "Streaming executor: tool %s failed",
                tool.tool_call.get("function", {}).get("name", "?"),
            )
            tool.result = {
                "role": "tool",
                "tool_call_id": tool.tool_call.get("id", ""),
                "content": json.dumps({"error": f"Tool execution failed: {exc}"}),
            }
            tool.errored = True
        finally:
            tool.status = ToolStatus.COMPLETED
            tool.completed_at = time.monotonic()

            # Environment failures (terminal) likely affect all
            # concurrent work — cancel siblings to avoid stale results.
            if tool.errored:
                fn_name = tool.tool_call.get("function", {}).get("name", "")
                if fn_name in SIBLING_ABORT_TOOLS:
                    self._abort_siblings(tool)

            # Start queued tools that can now execute.
            self._process_queue()

    async def _emit_cancelled_result_event(self, tool: TrackedTool) -> None:
        """Persist a ``TOOL_RESULT`` event for a cancelled sibling.

        Called from the ``CancelledError`` handler in :meth:`_run_tool`.
        Without this, the event log would contain a ``TOOL_CALL`` with no
        matching ``TOOL_RESULT`` — leaving the UI stuck on
        "Running command…" forever and breaking any consumer that pairs
        calls with results.

        Narrow race: if cancellation arrives *after*
        :func:`execute_single_tool` has already written its own
        ``TOOL_RESULT`` row but before the function returns, the same
        ``tool_call_id`` will be written twice — once by the dispatch
        path, once here.  Consumers must dedupe by ``tool_call_id``;
        the duplicate-row outcome is preferable to the prior bug where
        the call was permanently orphaned.
        """
        result = tool.result
        try:
            await self._store.emit_event(
                self._session.id,
                EventType.TOOL_RESULT,
                {
                    "tool_call_id": result.get("tool_call_id", ""),
                    "name": tool.tool_call.get("function", {}).get("name", ""),
                    "content": result.get("content", ""),
                    "elapsed_ms": int(
                        (time.monotonic() - tool.started_at) * 1000
                    ) if tool.started_at else 0,
                    "cancelled": True,
                },
            )
        except Exception:
            logger.warning(
                "Failed to emit cancelled tool.result for session %s",
                self._session.id,
                exc_info=True,
            )

    def _abort_siblings(self, failed_tool: TrackedTool) -> None:
        """Cancel all concurrently-executing sibling tools."""
        self._sibling_aborted = True
        for t in self._tracked:
            if t is not failed_tool and t.status == ToolStatus.EXECUTING:
                if t.task is not None and not t.task.done():
                    t.task.cancel()

    def _process_queue(self) -> None:
        """Start queued tools that can execute now.

        Scans tools in insertion order.  Concurrent-safe tools are started
        freely.  The first non-concurrent tool blocks further scanning
        (it must run alone).
        """
        for tool in self._tracked:
            if tool.status != ToolStatus.QUEUED:
                continue
            if not self._can_execute(tool):
                if not tool.is_parallelizable:
                    break  # Non-concurrent tool blocks the queue
                continue
            self._start_execution(tool)
            if not tool.is_parallelizable:
                break  # Non-concurrent tool runs alone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_error_result(result: dict[str, Any]) -> bool:
    """Check if a tool result indicates an error.

    Tools (notably ``terminal``) include an ``"error"`` key in their result
    schema even on success, with value ``null`` or ``""``.  We must therefore
    test for a truthy value, not mere key presence — otherwise every
    successful terminal call would be misclassified as errored and trigger
    sibling-abort against its concurrent peers.
    """
    content = result.get("content", "")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
            return isinstance(parsed, dict) and bool(parsed.get("error"))
        except (json.JSONDecodeError, TypeError):
            pass
    return False


