"""Core agent harness loop -- the most critical module in the platform.

The :class:`AgentHarness` is a **stateless** processor.  On every
``wake()`` call it:

1. Acquires an exclusive lease on the session.
2. Replays the durable event log to reconstruct the LLM message list.
3. Runs the LLM loop (call -> tool execution -> repeat) until the model
   stops issuing tool calls, the iteration budget is exhausted, or an
   unrecoverable error occurs.
4. Releases the lease.

All side-effects are captured as events via :class:`SessionStore` so that
any crash can be recovered by replaying the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID, uuid4

from surogates.harness.agent_resolver import (
    apply_agent_def_to_session,
    resolve_agent_def,
)
from surogates.harness.connection_health import cleanup_dead_connections
from surogates.harness.cost_tracker import SessionCostTracker
from surogates.harness.credentials import CredentialPool
from surogates.harness.error_classify import classify_harness_error
from surogates.harness.llm_call import apply_developer_role, call_llm_with_retry
from surogates.harness.message_utils import (
    coerce_message_content,
    make_skipped_tool_result,
)
from surogates.harness.prompt_cache import SystemPromptCache
from surogates.harness.rate_limit_guard import ProviderRateLimitGuard
from surogates.harness.reasoning import (
    THINK_RE,
    ContentWithToolsCache,
    extract_reasoning,
    has_incomplete_scratchpad,
    is_thinking_budget_exhausted,
    is_thinking_only_response,
    strip_think_blocks,
)
from surogates.harness.resilience import (
    find_invalid_tool_calls,
    inject_budget_warning,
    try_activate_fallback,
    try_rotate_credential,
)
from surogates.harness.sanitize import (
    cap_delegate_calls,
    deduplicate_tool_calls,
)
from surogates.harness.slash_skill import (
    build_deep_research_message,
    expand_slash_skill,
    parse_deep_research_command,
)
from surogates.harness.subdirectory_hints import SubdirectoryHintTracker
from surogates.harness.streaming_executor import StreamingToolExecutor
from surogates.harness.structured_output import generate_structured
from surogates.harness.tool_exec import execute_tool_calls
from surogates.harness.tool_guardrails import ToolGuardrailConfig, ToolGuardrails
from surogates.harness.tool_schemas import filter_schemas_for_tenant
from surogates.harness.title_generator import maybe_generate_session_title
from surogates.runtime.context import SlashCommandConfig
from surogates.session import LeaseNotHeldError
from surogates.session.events import EventType

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from redis.asyncio import Redis

    from surogates.browser.control import BrowserControlStore
    from surogates.browser.pool import BrowserPool
    from surogates.harness.budget import IterationBudget
    from surogates.harness.context import ContextCompressor
    from surogates.harness.prompt import PromptBuilder
    from surogates.memory.manager import MemoryManager
    from surogates.sandbox.pool import SandboxPool, sandbox_session_key
    from surogates.session.models import Session, SessionLease
    from surogates.session.store import SessionStore
    from surogates.tools.registry import ToolRegistry
    from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

# Some names below are not used by this module but are re-exported here as a
# stable import surface for the test suite (`from surogates.harness.loop import ...`);
# those lines are marked with `# noqa: F401`.
from surogates.harness.loop_artifacts import (
    _FENCE_RE,  # noqa: F401
    _PROMOTABLE_FENCES,  # noqa: F401
    _derive_artifact_name,  # noqa: F401
)
from surogates.harness.loop_attachments import (
    _attachments_note,  # noqa: F401
    _render_inlined_attachments,  # noqa: F401
)
from surogates.harness.loop_constants import (
    _DYNAMIC_LOOP_EXCLUDED_TOOLS,
    _EMPTY_RESPONSE_NUDGE,
    _LEASE_RENEWAL_INTERVAL_SECONDS,
    _LEASE_TTL_SECONDS,
    _LENGTH_CONTINUATION_PROMPT,
    _MAX_CONSECUTIVE_INVALID_TOOL_CALLS,
    _MAX_EMPTY_RESPONSE_RETRIES,
    _MAX_LENGTH_CONTINUATIONS,
    _PRE_WAKE_HUB_TIMEOUT_SECONDS,
)
from surogates.harness.loop_deep_research import (
    DEEP_RESEARCH_NO_DELEGATE_NUDGE,
    _is_deep_research_planner,
    _planner_already_delegated_to_writer,
)
from surogates.harness.loop_messages import (
    _initial_system_message,
    _latest_user_event_data,
    _latest_user_event_text,
    _latest_user_message_text,
    _should_notify_parent_on_completion,  # noqa: F401
    _view_context_note,  # noqa: F401
    _view_context_note_from_metadata,  # noqa: F401
    maybe_inject_browser_pause,
)
from surogates.harness.loop_mission_evaluator import (
    MissionJudgeParseError,  # noqa: F401
    _MissionVerdict,  # noqa: F401
    _build_mission_judge as _build_mission_judge_impl,
    _maybe_run_mission_evaluator,  # noqa: F401
    _parse_judge_json,  # noqa: F401
)
from surogates.harness.loop_pending import (
    _actionable_pending_events,
    _slash_loop_already_processed,
)
from surogates.harness.loop_tool_recovery import (
    _is_valid_json_args,
    build_partial_tool_call_recovery_results,
)
from surogates.harness.loop_user_action import (
    _USER_ACTION_RESCUE_SYSTEM,
    _generate_user_action_rescue_structured,
)
from surogates.harness.loop_vision import (
    _prepare_messages_for_model_vision_support,
)


from surogates.harness.loop_advisor import AdvisorMixin
from surogates.harness.loop_artifact_completion import ArtifactCompletionMixin
from surogates.harness.loop_arbor import ArborHarvestMixin
from surogates.harness.loop_board import BoardMixin
from surogates.harness.loop_code_commands import CodeCommandMixin
from surogates.harness.loop_context_replay import (
    ContextReplayMixin,
    build_user_message_dict,
    coalesce_user_messages,
)
from surogates.harness.loop_iteration_summary import IterationSummaryMixin
from surogates.harness.loop_outcome_commands import OutcomeCommandMixin

def _build_mission_judge(*, llm_client: Any, eval_model: str) -> Any:
    """Compatibility facade that preserves old ``loop.generate_structured`` patches."""
    return _build_mission_judge_impl(
        llm_client=llm_client,
        eval_model=eval_model,
        structured_generator=generate_structured,
    )






def _format_loop_list(rows: list[Any]) -> str:
    if not rows:
        return "No active loops."
    lines = ["Active loops:"]
    for row in rows:
        reason = row.schedule.get("last_delay_reason") if row.schedule else None
        suffix = f" (last wait: {reason})" if reason else ""
        lines.append(
            f"- `{row.id}` {row.schedule_display}: {row.prompt} "
            f"(next: {row.next_run_at}){suffix}"
        )
    return "\n".join(lines)


def _skill_invoked_event_data(
    *,
    skill_name: str,
    raw_message: str,
    staged_at: str | None,
    session_config: dict | None,
) -> dict:
    """Build the ``skill.invoked`` event payload, tagging override use.

    When the session config carries a ``skill_overrides`` entry for the
    invoked skill, the SkillOpt run/candidate ids are added so rollouts
    can be joined back to candidates in observability.
    """
    data: dict = {
        "skill": skill_name,
        "raw_message": raw_message,
        "staged_at": staged_at,
    }
    ov = ((session_config or {}).get("skill_overrides") or {}).get(skill_name)
    if ov:
        data["override_source"] = ov.get("source", "skillopt")
        if ov.get("run_id") is not None:
            data["skillopt_run_id"] = ov["run_id"]
        if ov.get("candidate_id") is not None:
            data["candidate_id"] = ov["candidate_id"]
    return data


def _rewrite_user_content_preserving_attachments(
    last_user: dict | None,
    all_events: list,
    new_text: str,
) -> None:
    """Swap a rewritten directive into the latest user message in place,
    keeping this turn's attachment note, inlined content, and image blocks.

    The slash-skill and ``/deep-research`` paths replace the raw
    ``/command`` text with an expanded body.  Assigning that body directly
    drops the per-turn attachment binding that ``_rebuild_messages`` folds
    into the user content, so the model loses track of which file the user
    just attached and binds the request to an earlier upload still visible
    in history.  Rebuilding via ``build_user_message_dict`` with the body as
    ``base_content`` reattaches the binding.
    """
    if last_user is None:
        return
    data = _latest_user_event_data(all_events) or {}
    last_user["content"] = build_user_message_dict(
        data, base_content=new_text,
    )["content"]


# Reminder injected when the deep-research planner ends a turn with
# no tool calls AND has never actually delegated to research-writer.
# Phrased to push the model toward emitting the call rather than
# narrating it again -- repeating the same "hand off to the writer"
# language would let the same failure mode recur.




# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Fenced-block kinds the post-response promoter is willing to turn into
# an artifact when the model emits one in place of a ``create_artifact``
# call.  Keys are the fence language tags; values map to the artifact
# ``kind`` and the spec key that carries the raw body.




# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _slash_command_name(content: str | None) -> str | None:
    """Map a user message to the canonical id of the built-in slash
    command it invokes, or None when it is not a gateable command.

    Mirrors the dispatch matchers in ``AgentHarness.wake`` so the
    capability gate and the dispatcher agree on what counts as each
    command.
    """
    if not content:
        return None
    if content == "/compress":
        return "compress"
    if content == "/clear":
        return "clear"
    if content == "/goal" or content.startswith("/goal "):
        return "goal"
    if content == "/mission" or content.startswith("/mission "):
        return "mission"
    if content == "/auto-research" or content.startswith("/auto-research "):
        return "auto-research"
    if content == "/code" or content.startswith("/code "):
        return "code"
    if content.startswith("/loop"):
        return "loop"
    if parse_deep_research_command(content) is not None:
        return "deep-research"
    return None


class AgentHarness(
    AdvisorMixin,
    BoardMixin,
    ArborHarvestMixin,
    ContextReplayMixin,
    IterationSummaryMixin,
    OutcomeCommandMixin,
    CodeCommandMixin,
    ArtifactCompletionMixin,
):
    """Stateless agent loop that replays events, runs the LLM, and emits events.

    New capabilities beyond the original implementation:

    - **Streaming** -- ``_streaming_enabled`` flag (default ``True``) causes
      the LLM call to use ``stream=True``.  Each text delta emits an
      ``LLM_DELTA`` event.  The accumulated full response is emitted as
      ``LLM_RESPONSE`` at the end.
    - **Tool parallelisation** -- safe tool calls are executed concurrently
      via ``asyncio.gather``.
    - **Thinking extraction** -- reasoning blocks are extracted from LLM
      responses and emitted as ``LLM_THINKING`` events.
    - **Interrupt handling** -- the :meth:`interrupt` method requests the
      loop to stop, skipping remaining tool calls.
    """

    def __init__(
        self,
        session_store: SessionStore,
        tool_registry: ToolRegistry,
        llm_client: AsyncOpenAI,
        tenant: TenantContext,
        worker_id: str,
        budget: IterationBudget,
        context_compressor: ContextCompressor,
        prompt_builder: PromptBuilder,
        *,
        redis_client: Redis | None = None,
        system_prompt_cache: SystemPromptCache | None = None,
        memory_manager: MemoryManager | None = None,
        sandbox_pool: SandboxPool | None = None,
        browser_pool: BrowserPool | None = None,
        browser_control: BrowserControlStore | None = None,
        storage: Any | None = None,
        checkpoints_enabled: bool = False,
        saga_enabled: bool = False,
        saga_settings: Any | None = None,
        api_client: Any | None = None,
        credential_vault: Any | None = None,
        default_model: str = "gpt-4o",
        session_factory: Any | None = None,
        log_policy_allowed: bool = False,
        summary_client: AsyncOpenAI | None = None,
        summary_model: str = "",
        vision_client: AsyncOpenAI | None = None,
        vision_model: str = "",
        advisor_client: AsyncOpenAI | None = None,
        advisor_model: str = "",
        advisor_max_calls_per_turn: int = 2,
        advisor_max_tokens: int = 700,
        media_gen: Any | None = None,
        turn_summarizer: Any | None = None,
        bundle: Any | None = None,
        turn_gate: Any | None = None,
        mcp_tool_names: frozenset[str] | None = None,
        composio_tool_names: frozenset[str] | None = None,
        slash_commands: SlashCommandConfig | None = None,
    ) -> None:
        self._store = session_store
        self._tools = tool_registry
        # MCP tool names discovered for THIS session's agent via the
        # proxy.  The registry is process-wide; this scopes the
        # model-visible schema set to the agent's own MCP tools.
        self._mcp_tool_names: frozenset[str] = frozenset(mcp_tool_names or ())
        # Subset of ``_mcp_tool_names`` from the Composio tool-router; used to
        # hide a channel agent's own-platform Composio toolkit (see
        # ``_drop_native_channel_composio_tools``).
        self._composio_tool_names: frozenset[str] = frozenset(composio_tool_names or ())
        # Per-agent slash-command gating.  Defaults to permissive so a
        # session resolved before this was wired keeps every command.
        self._slash_commands: SlashCommandConfig = (
            slash_commands if slash_commands is not None else SlashCommandConfig()
        )
        self._llm = llm_client
        self._tenant = tenant
        self._worker_id = worker_id
        self._budget = budget
        self._compressor = context_compressor
        self._prompt = prompt_builder
        self._redis: Redis | None = redis_client
        self._sandbox_pool: SandboxPool | None = sandbox_pool
        self._browser_pool: BrowserPool | None = browser_pool
        self._browser_control: BrowserControlStore | None = browser_control
        self._storage = storage
        self._api_client = api_client
        self._credential_vault = credential_vault
        self._session_factory = session_factory
        # Per-session Hub-backed bundle shared by every catalogue
        # load inside the harness (sub-agent resolver, prompt
        # builder, future skill staging).  ``None`` for agents
        # whose first publish hasn't landed yet.
        self._bundle: Any | None = bundle

        # Per-tenant TurnConcurrencyGate.  Wired through the tool
        # executor's kwargs so handlers that block on something
        # external (delegate_task polling a child, future
        # long-running waits) can ``release()`` their slot while
        # idle and ``try_acquire()`` it back before the parent
        # resumes producing work.  Optional -- standalone harness
        # tests don't construct one and the tools no-op when it's
        # absent.
        self._turn_gate: Any | None = turn_gate

        # Optional dedicated vision client.  When the active LLM does not
        # support image input, ``_prepare_messages_for_model_vision_support``
        # routes images to this client (with ``vision_model``) to obtain
        # text descriptions, then strips the image parts.  When unset,
        # vision substitution falls back to ``llm_client`` and the
        # configured ``llm.vision_model`` setting; when both are absent
        # the helper just strips images.
        # Optional per-agent summary client — the resolved ``llm_summary``
        # slot from the per-session bundle.  Used for cheap side work that
        # must honour the agent's configured summary endpoint (session
        # title generation), instead of the static global summary client
        # built from ``Settings.llm.summary_*``.  When unset the title
        # path falls back to the main turn client.
        self._summary_client: AsyncOpenAI | None = summary_client
        self._summary_model: str = summary_model or ""
        self._vision_client: AsyncOpenAI | None = vision_client
        self._vision_model: str = vision_model or ""
        self._advisor_client: AsyncOpenAI | None = advisor_client
        self._advisor_model: str = advisor_model or ""
        self._advisor_max_calls_per_turn = max(0, int(advisor_max_calls_per_turn))
        self._advisor_max_tokens = max(1, int(advisor_max_tokens))

        # Per-session media-generation wiring (image client + video
        # endpoint), passed opaquely down the executor kwarg chain to
        # the generate_image / generate_video tools.
        self._media_gen: Any | None = media_gen

        # Optional per-turn LLM summarizer for the Simple chat view.
        # When ``None`` (no summary_model configured, or
        # WorkerSettings.emit_turn_summaries=False), the harness emits no
        # iteration.summary / turn.summary events and the SDK falls back
        # to its expanded live-state rendering.
        self._turn_summarizer: Any | None = turn_summarizer
        # Per-iteration background summary tasks, keyed by
        # iteration_index for the active turn. Reset at the top of each
        # wake() so a paused-and-resumed session can't reuse stale
        # tasks; drained at turn-end before emitting turn.summary.
        self._pending_iteration_summary_tasks: dict[int, asyncio.Task[Any]] = {}
        # Snapshot of resolved iteration summaries indexed by
        # iteration_index. Used to give later iteration summaries
        # context about earlier ones in the same turn.
        self._completed_iteration_summaries: dict[int, str] = {}
        # Wall-clock timestamp captured at _run_loop start. Used by
        # _scan_workspace_for_new_files to surface files modified
        # during the current turn even when produced indirectly
        # (terminal scripts, execute_code).
        self._turn_started_at: datetime | None = None

        # Checkpoint flag — when enabled, the harness tells the sandbox
        # to take filesystem snapshots before file-mutating operations.
        # The actual checkpoint logic runs inside the sandbox (not here).
        self._checkpoints_enabled = checkpoints_enabled

        # Saga orchestration flag — when enabled, side-effecting tool
        # calls are tracked as saga steps with automatic compensation
        # on failure/interrupt/crash.
        self._saga_enabled = saga_enabled
        self._saga_settings = saga_settings

        # Full governance decision trail — when True, every allowed tool
        # call emits a ``policy.allowed`` event alongside the existing
        # ``policy.denied`` on block.  Off by default (doubles audit
        # volume); sourced from ``settings.governance.log_allowed``.
        self._log_policy_allowed = log_policy_allowed

        # System prompt cache (shared across wake() calls for the same worker).
        self._system_prompt_cache: SystemPromptCache = (
            system_prompt_cache if system_prompt_cache is not None else SystemPromptCache()
        )

        # Per-session memory snapshot.  Memory is prefetched once on the
        # first wake() of a session and reused byte-identically on every
        # subsequent wake(), so the memory_context message stays in the
        # provider's prefix cache.  Invalidated alongside the system
        # prompt cache (compression / context overflow / explicit reset).
        self._memory_snapshot_cache: dict[UUID, str | None] = {}

        # Streaming can be disabled via session config or env var.
        self._streaming_enabled: bool = True

        # Interrupt support -- thread-safe because only a single bool/str
        # is mutated and Python's GIL makes these assignments atomic.
        self._interrupt_requested: bool = False
        self._interrupt_message: str | None = None

        # The streaming executor currently in flight, if any. Set by
        # ``_run_iteration`` while a tool batch is executing and cleared
        # in its ``finally``. ``interrupt()`` discards it so an in-flight
        # tool (sandbox exec, browser action) is cancelled immediately
        # instead of waiting for its own timeout to fire.
        self._active_executor: StreamingToolExecutor | None = None

        # Memory manager (optional).
        self._memory_manager: MemoryManager | None = memory_manager

        # Credential pool (optional -- for multi-key resilience).
        self._credential_pool: CredentialPool | None = None

        # Memory / skill nudge counters.
        # Memory nudge: after N user turns without a memory write, remind the
        # model to review memory.  Skill nudge: after N tool-calling iterations
        # without a skill_manage call, remind the model to save skills.
        # Counters persist across wake() calls for the same worker so nudge
        # logic accumulates correctly in long-running sessions.
        self._memory_nudge_interval: int = 10
        self._skill_nudge_interval: int = 10
        self._turns_since_memory: int = 0
        self._iters_since_skill: int = 0
        self._user_turn_count: int = 0

        # Fallback provider chain.
        self._fallback_chain: list[dict] = []
        self._fallback_index: int = 0
        self._fallback_activated: bool = False
        self._primary_config: dict | None = None

        # Current model (may change on fallback activation).
        self._current_model: str | None = None
        self._default_model: str = default_model

        # Fire-and-forget background tasks (title generation, etc.).
        # Tasks are tracked here to prevent garbage collection while pending
        # and are discarded automatically on completion.
        self._background_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Interrupt API (thread-safe)
    # ------------------------------------------------------------------

    def interrupt(self, message: str | None = None) -> None:
        """Request the agent to stop the current loop.

        The interrupt is checked at the top of each loop iteration and
        before every tool execution.  If *message* is provided it is
        stored so the next ``wake()`` can log why the loop was stopped.

        Also sets the global interrupt event so that tools polling
        :func:`surogates.tools.utils.interrupt.is_interrupted` see the
        signal immediately, and discards the active streaming executor
        (if any) so in-flight tool tasks are cancelled now rather than
        waiting on their own timeout. Without this, a follow-up user
        message can sit unread for minutes while a sandbox exec or
        browser action runs to its tool-level timeout.
        """
        self._interrupt_requested = True
        self._interrupt_message = message
        from surogates.tools.utils.interrupt import set_interrupt
        set_interrupt(True)
        executor = self._active_executor
        if executor is not None:
            executor.discard()

    def _check_interrupt(self) -> bool:
        """Return ``True`` if an interrupt has been requested."""
        return self._interrupt_requested

    def _clear_interrupt(self) -> None:
        """Reset the interrupt flag (called after handling)."""
        self._interrupt_requested = False
        self._interrupt_message = None
        from surogates.tools.utils.interrupt import set_interrupt
        set_interrupt(False)

    async def _has_stranded_user_message(self, session_id: UUID) -> bool:
        """Return True if a real user message landed past the harness cursor.

        Detects the completion race: a reply sent between the final
        ``llm.response`` and ``_complete_session``'s status flip is
        appended while the session still reads 'active', so the API
        never emits SESSION_RESUME — and a wake on the now-terminal
        session would bail before looking at pending events.

        Only non-synthetic messages count: mission continuations and
        harness nudges must never revive a terminal session (same rule
        as the dispatcher's crash-loop ``_has_user_signal_since``).
        Completion bookkeeping events (turn.summary, session.complete,
        inbox.task_complete) always sit past the cursor and are
        excluded by the type filter.
        """
        cursor = await self._store.get_harness_cursor(session_id)
        events = await self._store.get_events(
            session_id,
            after=cursor,
            types=[EventType.USER_MESSAGE],
        )
        return any(
            not (event.data or {}).get("synthetic") for event in events
        )

    async def _collect_steer_messages(
        self,
        session_id: UUID,
        after_event_id: int,
    ) -> tuple[dict | None, int]:
        """Pull user messages that arrived past the steer cursor.

        Reads non-synthetic ``user.message`` events appended after
        ``after_event_id``, renders each through the same path replay uses
        (:func:`build_user_message_dict`), and coalesces them into one user
        turn so a burst of follow-ups becomes a single steered turn.

        Returns ``(coalesced_message_or_None, new_cursor)``.  The cursor
        advances to the highest event id seen even when every message was
        synthetic, so synthetic events (mission continuations, harness
        nudges) are never re-examined and never steer.
        """
        events = await self._store.get_events(
            session_id,
            after=after_event_id,
            types=[EventType.USER_MESSAGE],
        )
        if not events:
            return None, after_event_id
        new_cursor = max(event.id for event in events)
        rendered = [
            build_user_message_dict(event.data)
            for event in events
            if not (event.data or {}).get("synthetic")
        ]
        if not rendered:
            return None, new_cursor
        return coalesce_user_messages(rendered), new_cursor

    async def _abort_iteration_with_pause(
        self,
        session: Session,
        saga: Any,
    ) -> None:
        """Tear down sandbox + sagas and emit SESSION_PAUSE, then clear.

        Shared by the iteration-top interrupt check and the pre-emission
        staleness guard so both paths perform the same cleanup before
        returning from the loop.
        """
        reason_msg = self._interrupt_message or "interrupted"
        if saga is not None and saga.active_sagas:
            await self._compensate_sagas(saga, session, "interrupt")
        if self._sandbox_pool is not None:
            try:
                await self._sandbox_pool.destroy_for_session(str(session.id))
            except Exception:
                logger.debug(
                    "Sandbox cleanup on interrupt failed", exc_info=True,
                )
        # Only emit SESSION_PAUSE if the session is still in 'paused'
        # status. The /pause endpoint already emitted SESSION_PAUSE and
        # set status='paused' before signalling the interrupt; if a
        # concurrent /messages call has since flipped status back to
        # 'active' (emitting SESSION_RESUME + USER_MESSAGE), this
        # cleanup pause would land *after* the resume in the event log
        # and leave the client's terminal flag stuck on, suppressing
        # the running indicator for the new turn's deltas.
        current = await self._store.get_session(session.id)
        if current.status == "paused":
            await self._store.emit_event(
                session.id,
                EventType.SESSION_PAUSE,
                {
                    "reason": "interrupted",
                    "message": reason_msg,
                    "worker_id": self._worker_id,
                },
            )
        self._clear_interrupt()

    # ------------------------------------------------------------------
    # Lease renewal (background task)
    # ------------------------------------------------------------------

    async def _renew_lease_forever(
        self,
        session_id: UUID,
        lease_token: UUID,
    ) -> None:
        """Periodically renew the session lease until cancelled.

        Runs in parallel with :meth:`_run_loop`.  Uses a time-based cadence
        (``_LEASE_RENEWAL_INTERVAL_SECONDS``) so a single long iteration
        -- e.g. a slow LLM call or streaming-to-non-streaming fallback --
        cannot let the lease expire.

        If renewal fails because the lease no longer belongs to us
        (:class:`LeaseNotHeldError`), another worker has taken over the
        session.  Request an interrupt so the main loop exits cleanly
        instead of racing against the new worker and writing duplicate
        events.  Transient DB errors are retried on the next tick.
        """
        while True:
            try:
                await asyncio.sleep(_LEASE_RENEWAL_INTERVAL_SECONDS)
                await self._store.renew_lease(
                    session_id, lease_token, ttl_seconds=_LEASE_TTL_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except LeaseNotHeldError:
                logger.warning(
                    "Session %s: lease stolen by another worker, "
                    "interrupting current loop",
                    session_id,
                )
                self.interrupt("lease lost — another worker took over")
                return
            except Exception:
                logger.debug(
                    "Transient lease renewal failure for session %s",
                    session_id,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Slash-command capability gate
    # ------------------------------------------------------------------

    def _slash_command_enabled(self, name: str) -> bool:
        """True when slash command *name* is enabled for this agent."""
        return name in self._slash_commands.commands

    def _slash_command_block_reason(self, content: str | None) -> str | None:
        """User-facing message when the slash command in *content* is gated
        off for this agent, else None (allowed, or not a gateable command)."""
        name = _slash_command_name(content)
        if name is None or self._slash_command_enabled(name):
            return None
        return f"/{name} is disabled for this agent."

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def _run_browser_setup(self, session: "Session") -> None:
        """Provision (or release) the interactive browser for a setup session.

        The browser is provisioned here — in the worker, the only process that
        owns a ``BrowserPool`` and the fleet credentials — rather than from the
        API. No agent loop runs: the human logs in through the live view and the
        ``/browser-profiles/{id}/capture`` route exports the result. Capture (or
        cancel) flips the session to a terminal status and re-enqueues it, which
        brings us back here to destroy the browser instead of provisioning it,
        so it stops billing well before its pod deadline.
        """
        if self._browser_pool is None:
            return
        browser_cfg = (session.config or {}).get("browser", {})
        owner = browser_cfg.get("setup_owner_user_id")
        if not owner:
            return
        sid = str(session.id)

        # Take the session lease so two workers can't double-provision or race
        # provision-vs-teardown for the same setup session. A no-op (skip) when
        # another worker already holds it.
        lease = await self._store.try_acquire_lease(
            session.id, self._worker_id, ttl_seconds=_LEASE_TTL_SECONDS
        )
        if lease is None:
            return
        try:
            if session.status in ("completed", "failed"):
                await self._browser_pool.destroy_for_session(sid)
                return

            from surogates.browser.base import BrowserSpec

            ttl = int(browser_cfg.get("setup_ttl_seconds") or 15 * 60)
            # No profile injection: the user logs in by hand from a fresh
            # context.
            await self._browser_pool.ensure(
                session_id=sid,
                org_id=str(session.org_id),
                user_id=str(owner),
                spec=BrowserSpec(active_deadline_seconds=ttl),
            )
            if self._browser_control is not None:
                await self._browser_control.acquire(sid, str(owner))
        finally:
            await self._store.release_lease(session.id, lease.lease_token)

    async def wake(self, session_id: UUID) -> str | None:
        """Entry point.  Acquire lease, replay events, run loop, release lease."""
        from surogates.trace import new_span

        # Create a child span for this wake cycle (parent was set by the
        # orchestrator or API middleware).
        new_span()

        # Honour streaming preference from env.
        env_streaming = os.environ.get("SUROGATES_STREAMING_ENABLED", "").lower()
        if env_streaming in ("0", "false", "no"):
            self._streaming_enabled = False

        # State declared up front so the except/finally blocks below can
        # reference them defensively: a crash in the pre-wake setup
        # leaves session/lease/renewal_task as None and we must not
        # blow up the cleanup path.
        session: Session | None = None
        lease: Any | None = None
        renewal_task: asyncio.Task[None] | None = None

        try:
            # Connection health: proactively clean up dead connections
            # before making any LLM requests.  Wrapped in its own
            # try because a stale-connection sweep failure should not
            # abort the wake.
            try:
                await cleanup_dead_connections(self._llm)
            except Exception:
                logger.debug(
                    "Connection health cleanup failed", exc_info=True,
                )

            # 1. Fetch session metadata.
            logger.debug(
                "wake step=fetch_session session=%s", session_id,
            )
            session = await self._store.get_session(session_id)

            # A browser-setup session is interactive-only: provision a fresh
            # browser + grant the user control on the first wake, and release it
            # once capture/cancel flips the status to terminal. No LLM loop runs.
            if session.channel == "browser_setup":
                await self._run_browser_setup(session)
                return None

            # Bail out if the session was already paused/completed/failed
            # before this wake cycle -- prevents re-running a session
            # the user stopped.  Exception: a real user message that
            # landed past the harness cursor on a completed/failed
            # session.  ``send_message`` only emits SESSION_RESUME when
            # it *sees* a terminal status; a reply racing with
            # ``_complete_session`` (sent between the final
            # ``llm.response`` and the status flip) is appended while
            # the session still reads 'active', so no resume ever lands
            # and the message would be stranded forever.  Resume here —
            # the message's own enqueue is what triggered this wake.
            # Paused stays a hard stop: pause is an explicit user
            # action, and send_message on a paused session resumes
            # through the API path.
            if session.status in ("paused", "completed", "failed"):
                if session.status in (
                    "completed", "failed",
                ) and await self._has_stranded_user_message(session_id):
                    logger.info(
                        "Session %s: status is '%s' but an unprocessed "
                        "user message sits past the cursor — resuming",
                        session_id,
                        session.status,
                    )
                    await self._store.update_session_status(
                        session_id, "active",
                    )
                    await self._store.emit_event(
                        session_id,
                        EventType.SESSION_RESUME,
                        {"source": "stranded_user_message"},
                    )
                    session.status = "active"
                else:
                    logger.info(
                        "Session %s: status is '%s', skipping wake",
                        session_id,
                        session.status,
                    )
                    return

            # Resolve the sub-agent type (if any) and hydrate session
            # config with its presets.  Wrapped in ``asyncio.wait_for``
            # because this step touches Hub via the bundle
            # (``bundle.list("agents/")`` + ``bundle.read_text(...)``);
            # an unhealthy Hub used to silently strand sessions here
            # with no event, no log, no recovery beyond the orphan
            # sweeper re-enqueueing forever.  A timeout surfaces the
            # next hang as a ``harness.crash`` event with
            # ``error_category=timeout``.
            logger.debug(
                "wake step=resolve_agent_def session=%s timeout=%.0fs",
                session_id, _PRE_WAKE_HUB_TIMEOUT_SECONDS,
            )
            active_agent_def = await asyncio.wait_for(
                resolve_agent_def(
                    session, self._tenant,
                    session_factory=self._session_factory,
                    bundle=self._bundle,
                ),
                timeout=_PRE_WAKE_HUB_TIMEOUT_SECONDS,
            )
            if active_agent_def is not None:
                apply_agent_def_to_session(session, active_agent_def)
            self._prompt.set_agent_def(active_agent_def)

            # Honour per-session streaming config.
            if not session.config.get("streaming", True):
                self._streaming_enabled = False

            # 2. Acquire exclusive lease -- return silently if another
            # worker holds it.
            logger.debug(
                "wake step=try_acquire_lease session=%s", session_id,
            )
            lease = await self._store.try_acquire_lease(
                session_id, self._worker_id, ttl_seconds=_LEASE_TTL_SECONDS,
            )
            if lease is None:
                logger.debug(
                    "Session %s: lease held by another worker, skipping",
                    session_id,
                )
                return "lease_held"

            # Start the background lease renewal task alongside the
            # main loop.  Cancelled in the ``finally`` block below so
            # the lease renews regardless of how long any single
            # iteration takes.
            renewal_task = asyncio.create_task(
                self._renew_lease_forever(session_id, lease.lease_token),
                name=f"lease-renewal-{session_id}",
            )

            # 3. Retrieve the harness cursor and the full event history.
            logger.debug(
                "wake step=load_events session=%s", session_id,
            )
            cursor = await self._store.get_harness_cursor(session_id)
            all_events = await self._store.get_events(session_id)

            # 4. Check for pending events (events after the cursor).
            pending = _actionable_pending_events(all_events, cursor)
            if not pending:
                logger.debug(
                    "Session %s: no actionable pending events after cursor %d",
                    session_id,
                    cursor,
                )
                return

            # 5. Emit HARNESS_WAKE event.
            await self._store.emit_event(
                session_id,
                EventType.HARNESS_WAKE,
                {"worker_id": self._worker_id, "cursor": cursor},
            )

            # 5a. Initialize memory manager if available.
            if self._memory_manager is not None:
                try:
                    self._memory_manager.initialize_all()
                except Exception:
                    logger.debug("Memory manager initialization failed", exc_info=True)

            # 6. Rebuild the message list from the full event history.
            messages = self._rebuild_messages(all_events)

            # 6a. Kick off title generation in the background as soon as we
            # see the user's first message.  Runs in parallel with context
            # engineering and the main LLM call, so the chat turn isn't
            # delayed waiting for the title.
            self._maybe_generate_title(
                session=session,
                messages=messages,
                model=session.model or self._default_model,
            )

            # 7. Compress context if needed.
            messages = await self._engineer_context(
                session, all_events, messages,
            )

            # 8. Build the system prompt (with caching).
            system_prompt = await self._build_system_prompt(session)

            # 9. Create per-session cost tracker.
            cost_tracker = SessionCostTracker()

            # 10. Handle /compress command — compress context without LLM call.
            #
            # Slash-command detection MUST look at the raw user text from the
            # event log, not the rebuilt-message content.  _rebuild_messages
            # prepends attachment / view-context notes to the user content;
            # a leading note pushes the "/" off the start and silently
            # disables every slash command (incl. /<skill>) when the message
            # carries a path-only attachment.  ``last_user`` (rebuilt message)
            # is still needed below for in-place mutation when a skill
            # expansion succeeds.
            last_user = next(
                (m for m in reversed(messages) if m.get("role") == "user"),
                None,
            )
            last_user_content = _latest_user_event_text(all_events)

            # Capability gate: refuse slash commands disabled for this
            # agent (master switch off, or this command individually off)
            # before the dispatch chain below would handle them.
            slash_block = self._slash_command_block_reason(last_user_content)
            if slash_block is not None:
                await self._emit_loop_response(
                    session, lease, slash_block, user_content=last_user_content
                )
                return

            if last_user_content == "/compress":
                await self._handle_compress_command(
                    session, messages, system_prompt, lease,
                )
                return

            if last_user_content == "/clear":
                await self._handle_clear_command(session, lease)
                return

            if last_user_content == "/goal" or last_user_content.startswith("/goal "):
                await self._handle_goal_command(session, last_user_content, lease)
                return

            if last_user_content == "/mission" or last_user_content.startswith("/mission "):
                await self._handle_mission_command(session, last_user_content, lease)
                return

            if last_user_content == "/auto-research" or last_user_content.startswith("/auto-research "):
                await self._handle_auto_research_command(session, last_user_content, lease)
                return

            if last_user_content == "/code" or last_user_content.startswith("/code "):
                await self._handle_code_command(
                    session, last_user_content, lease, all_events,
                )
                return

            if last_user_content.startswith("/loop"):
                # Idempotency guard: ``_handle_loop_command`` creates a fresh
                # scheduled-loop row each time it runs against a ``/loop ...``
                # user message.  If the harness wakes a second time on the
                # same message — e.g. after an orphan-sweeper recovery — we
                # must not create a duplicate schedule.
                if not _slash_loop_already_processed(all_events):
                    await self._handle_loop_command(
                        session, last_user_content, lease,
                    )
                return

            # 10b. /deep-research <topic> -- rewrite the user message to
            # a deterministic delegation directive so the base LLM hands
            # the topic to the ``deep-research`` sub-agent via
            # delegate_task rather than running the research itself.
            # No early return: the rewritten message flows into step 11
            # so the LLM still runs this turn.
            deep_research_topic = parse_deep_research_command(
                last_user_content,
            )
            if deep_research_topic is not None:
                _rewrite_user_content_preserving_attachments(
                    last_user,
                    all_events,
                    build_deep_research_message(topic=deep_research_topic),
                )

            # 10c. Eager /<skill> or /<expert> expansion.
            # See slash_skill.expand_slash_skill. ``kind`` distinguishes
            # the two paths so we don't double-emit a skill.invoked when
            # the service already emitted expert.delegation.
            elif last_user_content.startswith("/"):
                expansion = await expand_slash_skill(
                    text=last_user_content,
                    tools=self._tools,
                    tenant=self._tenant,
                    session_id=str(session.id),
                    api_client=self._api_client,
                    session_factory=self._session_factory,
                    session_config=session.config,
                    session_store=self._store,
                    sandbox_pool=self._sandbox_pool,
                    credential_vault=self._credential_vault,
                )
                if expansion is not None:
                    expanded_text, skill_name, staged_at, kind = expansion
                    _rewrite_user_content_preserving_attachments(
                        last_user, all_events, expanded_text,
                    )
                    if kind == "skill":
                        # Suppress duplicate audit events on crash-recovery wakes.
                        # skill_view itself is idempotent (staging short-circuits via
                        # an exists() check), but the SKILL_INVOKED event log row is
                        # not -- so guard it by scanning prior events.
                        already_emitted = any(
                            e.type == EventType.SKILL_INVOKED.value
                            and e.data.get("raw_message") == last_user_content
                            for e in all_events
                        )
                        if not already_emitted:
                            try:
                                await self._store.emit_event(
                                    session.id,
                                    EventType.SKILL_INVOKED,
                                    _skill_invoked_event_data(
                                        skill_name=skill_name,
                                        raw_message=last_user_content,
                                        staged_at=staged_at,
                                        session_config=session.config,
                                    ),
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to emit SKILL_INVOKED audit event "
                                    "for session %s skill=%s",
                                    session.id, skill_name,
                                )
                    # kind == "expert": the ExpertConsultationService has
                    # already emitted expert.delegation and (later) expert.result
                    # or expert.failure, so we intentionally skip the
                    # SKILL_INVOKED row here.

            # 11. Run the core LLM loop.
            await self._run_loop(session, messages, system_prompt, lease, cost_tracker=cost_tracker, all_events=all_events)

        except Exception as _harness_exc:
            logger.exception("Harness crash for session %s", session_id)
            info = classify_harness_error(_harness_exc)
            try:
                await self._store.emit_event(
                    session_id,
                    EventType.HARNESS_CRASH,
                    {
                        "worker_id": self._worker_id,
                        "error": traceback.format_exc()[-2000:],
                        "error_category": info.category,
                        "error_title": info.title,
                        "error_detail": info.detail,
                        "retryable": info.retryable,
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to emit HARNESS_CRASH event for session %s",
                    session_id,
                )
            # Notify parent if this is a worker session.  ``session`` is
            # ``None`` when the crash happened in the pre-wake setup
            # (e.g. a Hub timeout during ``resolve_agent_def``) -- the
            # row wasn't even fetched.  Skip the parent notification in
            # that case; the parent's delegation poll will time out
            # normally and the next pickup retries the wake.
            if session is not None and session.parent_id is not None:
                from surogates.harness.worker_notify import notify_parent_on_failure
                try:
                    await notify_parent_on_failure(
                        session_store=self._store,
                        worker_session_id=session_id,
                        parent_session_id=session.parent_id,
                        org_id=str(session.org_id),
                        agent_id=session.agent_id,
                        error=traceback.format_exc()[-500:],
                        redis=self._redis,
                        task_id=getattr(session, "task_id", None),
                    )
                except Exception:
                    logger.debug("Failed to notify parent on crash", exc_info=True)
            raise
        finally:
            # Stop the background renewal task before touching the
            # lease.  ``None`` when the wake bailed before the lease
            # was acquired (status=paused short-circuit, lease held
            # by another worker, or a pre-wake crash).
            if renewal_task is not None:
                renewal_task.cancel()
                try:
                    await renewal_task
                except (asyncio.CancelledError, Exception):
                    pass

            # Best-effort drain of fire-and-forget background tasks (title
            # generation, etc.) so they don't get cancelled mid-LLM-call when
            # the worker turns over.  Bounded by
            # ``_BACKGROUND_DRAIN_TIMEOUT_SECONDS``; anything still pending is
            # cancelled so lease release isn't delayed by a hung task.
            await self._drain_background_tasks(session_id)

            # Release the lease only if we acquired one.  Skipping when
            # ``lease is None`` mirrors the renewal_task gate above.
            if lease is not None:
                try:
                    await self._store.release_lease(
                        session_id, lease.lease_token,
                    )
                except Exception:
                    logger.warning(
                        "Failed to release lease for session %s", session_id,
                    )

    # ------------------------------------------------------------------
    # Core LLM loop
    # ------------------------------------------------------------------

    async def _run_loop(
        self,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        lease: SessionLease,
        *,
        cost_tracker: SessionCostTracker | None = None,
        all_events: list[Any] | None = None,
    ) -> None:
        """The core loop: call LLM -> process tool calls -> repeat until done.

        Production-hardening features:
        - Retry with jittered exponential backoff on transient errors
        - 429 rate limit handling with credential rotation and fallback
        - Response shape validation
        - Length continuation (finish_reason == "length")
        - Budget pressure warnings
        - Invalid tool call recovery
        - Per-session cost tracking
        """
        # --- Saga orchestrator ---
        saga = None
        if self._saga_enabled:
            from surogates.governance.saga import SagaOrchestrator
            saga_kwargs = {}
            if self._saga_settings is not None:
                saga_kwargs = {
                    "default_step_timeout": self._saga_settings.default_step_timeout,
                    "default_max_retries": self._saga_settings.default_max_retries,
                    "retry_delay": self._saga_settings.retry_delay,
                }
            saga = SagaOrchestrator(**saga_kwargs)
            # Reconstruct any in-progress saga from the event log.
            if all_events:
                saga_events = [
                    e for e in all_events
                    if str(e.type).startswith("saga.")
                ]
                if saga_events:
                    saga.reconstruct_from_events(saga_events)
            # Create a fresh saga for this wake cycle if none is active.
            if not saga.active_sagas:
                from surogates.governance.events import saga_start_event
                new_saga = saga.create_saga(session.id)
                await self._store.emit_event(
                    session.id,
                    EventType.SAGA_START,
                    saga_start_event(new_saga.saga_id, str(session.id)),
                )

        # One stable turn_id per user turn. The wake() body services
        # exactly one user turn (returns on session.complete/pause/fail),
        # so a single UUID covers every iteration in scope here. Threaded
        # into call_llm_with_retry and stamped on LLM_THINKING /
        # LLM_RESPONSE / LLM_REQUEST / LLM_DELTA payloads so the Simple
        # chat view can correlate iteration.summary events back to the
        # right assistant message.
        turn_id = str(uuid4())
        # Wall-clock turn start, used by _collect_candidate_artifacts to
        # surface workspace files modified during this turn even when
        # they were created indirectly (e.g. a python script written
        # by the terminal tool).
        self._turn_started_at = datetime.now(timezone.utc)

        # Reset per-turn summary tracking so a paused-and-resumed
        # session can't reuse stale tasks from a previous wake().
        self._pending_iteration_summary_tasks = {}
        self._completed_iteration_summaries = {}

        # Steer cursor: highest user-message event already folded into the
        # replayed ``messages``.  Mid-wake real follow-ups past this cursor
        # are incorporated at iteration boundaries (see the loop top).  Kept
        # separate from the durable session cursor, which tracks crash
        # recovery, not in-memory message incorporation.
        steer_cursor = max(
            (
                event.id
                for event in (all_events or [])
                if event.type == EventType.USER_MESSAGE.value
            ),
            default=0,
        )
        # Iteration index is reported per user turn, not per wake.  When a
        # steer message starts a new turn mid-wake, this base advances so the
        # new turn's first model call reports iteration_index 0.
        turn_base_iteration = 0

        iteration = 0
        length_continuation_count = 0
        length_continuation_prefix = ""  # accumulated partial response across length retries
        consecutive_invalid_tool_calls = 0
        invalid_json_retries = 0  # API-level retries for malformed tool args
        thinking_prefill_retries = 0  # retries for thinking-only responses
        incomplete_scratchpad_retries = 0  # retries for unclosed REASONING_SCRATCHPAD
        empty_response_retries = 0  # retries for empty LLM responses (no content, no tools, no reasoning)
        # One-shot safety net for the deep-research planner.  Fires when
        # the planner ends a turn with no tool calls AND has never
        # called ``delegate_task(agent_type="research-writer")`` in any
        # prior turn -- the model described the handoff in prose but
        # forgot to emit the actual tool call.  We inject a synthetic
        # user message reminding it that prose is not a tool call and
        # continue the loop one more time; if it still doesn't delegate
        # we let the session complete.  Tracked here (not via an event
        # marker) because the second-chance turn happens within the
        # same wake -- no re-enqueue, no re-rebuild of events.
        deep_research_delegate_nudge_fired = False
        content_with_tools_cache = ContentWithToolsCache()
        tool_guardrails = ToolGuardrails(
            ToolGuardrailConfig.from_mapping(
                session.config.get("tool_loop_guardrails")
                if session.config else None
            )
        )
        # Guardrail counters are per-wake; re-arm the consecutive
        # no-progress chain from history so an identical-call loop that
        # halted last wake is blocked immediately instead of being
        # allowed to grow again.
        tool_guardrails.seed_from_messages(messages)

        # Subdirectory hint tracker -- discovers context files as the agent navigates.
        hint_tracker = SubdirectoryHintTracker(
            initial_cwd=session.config.get("workspace_path"),
        )

        # --- Prefilled context injection ---
        # Ephemeral messages injected between system prompt and conversation
        # for few-shot examples or planning context. API-call-time only.
        prefill_messages: list[dict] = session.config.get("prefill_messages") or []

        # --- Coordination board cursor (persisted across wakes) ---
        board_cursor: int | None = session.config.get("board_cursor")

        # --- Memory prefetch (one-shot before loop; snapshotted per session) ---
        memory_context = await self._prefetch_memory(session.id)

        consulted_advisor_categories = self._advisor_categories_after_latest_user(
            all_events or [],
        )

        # NOTE: view-context and attachments notes are folded into each
        # user message's content during :meth:`_rebuild_messages`, so the
        # message bytes are determined by the durable event payload.
        # That keeps the provider's implicit prefix cache stable across
        # turns -- earlier versions inserted both notes ephemerally
        # before the latest user message, which broke the cache the
        # moment a new user turn shifted the insertion point.

        # --- Hidden advisor guidance for hard tasks (one-shot before loop) ---
        # Spawned as a background task so iteration 0 isn't blocked
        # waiting for the classifier + advisor LLM call. The task
        # mutates the shared ``messages`` list when it finishes
        # (``_consult_advisor_for_category`` appends an advisor
        # scaffold). The main loop rebuilds ``api_messages`` from
        # ``messages`` at the start of every iteration, so as soon
        # as the advisor completes the next iteration picks up its
        # guidance. If the task is still pending when wake()
        # returns, ``_drain_background_tasks`` bounds the wait at
        # ``_BACKGROUND_DRAIN_TIMEOUT_SECONDS``; advisor events
        # persist via the event log regardless.
        advisor_task = asyncio.create_task(
            self._maybe_consult_required_advisor(
                session,
                messages,
                all_events or [],
                system_prompt,
                consulted_advisor_categories,
            ),
            name=f"advisor-{session.id}",
        )
        self._background_tasks.add(advisor_task)
        advisor_task.add_done_callback(self._background_tasks.discard)

        # --- User turn tracking for memory nudge ---
        self._user_turn_count += 1
        should_review_memory = False
        if (
            self._memory_nudge_interval > 0
            and self._memory_manager is not None
        ):
            self._turns_since_memory += 1
            if self._turns_since_memory >= self._memory_nudge_interval:
                should_review_memory = True
                self._turns_since_memory = 0

        while self._budget.remaining > 0:
            iteration += 1
            # Capture the iteration start so the per-iteration summary
            # event carries a real wall-clock window for the row in the
            # Simple chat view.
            iteration_started_at = datetime.now(timezone.utc).isoformat()

            # Each LLM iteration gets its own span so tool calls and LLM
            # requests within this iteration share a parent.
            from surogates.trace import new_span as _new_iter_span
            _new_iter_span()

            # --- Interrupt check at the top of each iteration ---
            if self._check_interrupt():
                await self._abort_iteration_with_pause(session, saga)
                return

            # --- Mid-turn steering ---
            # Fold in any real user messages that arrived since the last
            # boundary as one coalesced new user turn, then keep going in
            # the same wake.  The interrupt check above already ran, so an
            # explicit Stop always wins over a steer.
            steer_message, steer_cursor = await self._collect_steer_messages(
                session.id, steer_cursor,
            )
            if steer_message is not None:
                messages.append(steer_message)
                turn_id = str(uuid4())
                turn_base_iteration = iteration - 1
                self._pending_iteration_summary_tasks = {}
                self._completed_iteration_summaries = {}
                logger.info(
                    "Session %s: incorporated steer message at iteration %d "
                    "(new turn_id=%s)",
                    session.id, iteration, turn_id,
                )

            # Turn-local iteration index: resets to 0 whenever a steer
            # message starts a new turn, while ``iteration`` keeps climbing
            # for budget/loop control.
            turn_iteration_index = iteration - 1 - turn_base_iteration

            # --- Checkpoint: reset per-turn dedup in sandbox ---
            if self._checkpoints_enabled and self._sandbox_pool:
                try:
                    await self._sandbox_pool.execute(
                        sandbox_session_key(session), "_checkpoint",
                        '{"action": "new_turn"}',
                    )
                except (ValueError, Exception):
                    pass  # No sandbox provisioned yet — that's fine.

            # --- Memory manager: on_turn_start hook ---
            if self._memory_manager is not None:
                try:
                    self._memory_manager.on_turn_start(turn_number=0, message="")
                except Exception:
                    logger.debug("Memory manager on_turn_start failed", exc_info=True)

            # --- Coordination board: join snapshot / delta delivery ---
            board_cursor = await self.maybe_emit_board_update(
                session, messages, board_cursor,
            )

            # --- Research missions: deterministic pre-LLM harvest ---
            # Folds finished experiments into the Idea Tree before the
            # coordinator LLM runs, so a dead executor or compacted context
            # can never strand a running node or stale the leaderboard.
            await self.maybe_harvest_research(session, messages)

            # --- Skill nudge tracking ---
            # Counter resets whenever skill_manage is actually used (in
            # tool_exec.py the reset would be done; here we just increment).
            if self._skill_nudge_interval > 0:
                self._iters_since_skill += 1

            # Consume one iteration from the budget.
            if not self._budget.consume():
                await self._request_final_summary(
                    session, messages, system_prompt, lease,
                    cost_tracker=cost_tracker,
                    turn_id=turn_id,
                    iteration_index=turn_iteration_index,
                )
                return

            # 1. Emit LLM_REQUEST event.
            model_id = self._current_model or session.model or self._default_model
            await self._store.emit_event(
                session.id,
                EventType.LLM_REQUEST,
                {
                    "model": model_id,
                    "iteration": iteration,
                    "turn_id": turn_id,
                    "iteration_index": turn_iteration_index,
                },
            )

            # 2. Call the LLM with retry (streaming or non-streaming).
            # Tool filtering:
            # - Coordinator sessions get all tools (soft mode — can delegate
            #   or do work directly).
            # - Worker sessions see all tools except coordinator tools
            #   (prevents recursive spawning).
            # - Normal sessions (no coordinator flag) also exclude coordinator
            #   tools — they're useless without the coordinator prompt and
            #   would confuse the LLM.
            # - Sessions with explicit allowed_tools get exactly those.
            tool_filter = self._tool_filter_for_session(session)

            tool_schemas = filter_schemas_for_tenant(
                self._tools.get_schemas(names=tool_filter),
                has_agents=self._prompt.has_agents,
            )

            # Build the message list: system → prefill → memory → conversation.
            # Each message is cleaned for API compatibility: internal-only fields are stripped, reasoning
            # is passed back as ``reasoning_content`` for providers that need
            # it (Moonshot AI, Novita, OpenRouter).
            browser_pause_notice = await maybe_inject_browser_pause(
                session=session,
                browser_control=self._browser_control,
            )
            api_messages: list[dict] = [
                _initial_system_message(system_prompt, browser_pause_notice),
            ]
            # Prefilled context (few-shot examples, planning context)
            if prefill_messages:
                api_messages.extend(prefill_messages)
            # Memory context (prefetched once before loop)
            if memory_context:
                api_messages.append({
                    "role": "user",
                    "content": f"[Recalled memory context]\n{memory_context}",
                })
                api_messages.append({
                    "role": "assistant",
                    "content": "Understood, I have the memory context.",
                })
            for msg in messages:
                api_msg = msg.copy()
                # For assistant messages, pass reasoning back to the API
                # via reasoning_content for multi-turn reasoning continuity
                # (Moonshot AI, Novita, OpenRouter).
                if msg.get("role") == "assistant":
                    reasoning_text = msg.get("reasoning")
                    if reasoning_text:
                        api_msg["reasoning_content"] = reasoning_text
                # Strip internal-only fields not accepted by any API.
                api_msg.pop("reasoning", None)
                api_msg.pop("finish_reason", None)
                api_msg.pop("_thinking_prefill", None)
                # Keep reasoning_details -- OpenRouter uses this for multi-turn
                # reasoning context with signature fields.
                api_messages.append(api_msg)

            await _prepare_messages_for_model_vision_support(
                api_messages,
                model_id=model_id,
                llm_client=self._llm,
                vision_client=self._vision_client,
                vision_model_override=self._vision_model,
            )

            # Developer role swap for models that prefer it (e.g. GPT-5, Codex).
            api_messages = apply_developer_role(api_messages, model_id)

            create_kwargs: dict[str, Any] = {
                "model": model_id,
                "messages": api_messages,
                "temperature": session.config.get("temperature", 0.3),
                "max_tokens": session.config.get("max_tokens", 16384),
            }
            if tool_schemas:
                create_kwargs["tools"] = tool_schemas

            # Create a streaming tool executor when eligible.  The executor
            # starts executing concurrency-safe (read-only) tools as their
            # tool_use blocks complete during LLM streaming, overlapping
            # tool execution with LLM generation for lower latency.
            # The executor is safe to use even with saga because it only
            # starts read-only tools during streaming — non-concurrent
            # (side-effecting) tools stay queued until get_all_results().
            streaming_executor: StreamingToolExecutor | None = None
            on_tool_call_cb: Callable[[dict[str, Any]], None] | None = None

            def _make_streaming_executor() -> StreamingToolExecutor:
                return StreamingToolExecutor(
                    session=session,
                    lease=lease,
                    store=self._store,
                    tools=self._tools,
                    tenant=self._tenant,
                    interrupt_check=self._check_interrupt,
                    redis=self._redis,
                    budget=self._budget,
                    memory_manager=self._memory_manager,
                    hint_tracker=hint_tracker,
                    sandbox_pool=self._sandbox_pool,
                    credential_vault=self._credential_vault,
                    browser_pool=self._browser_pool,
                    browser_control=self._browser_control,
                    storage=self._storage,
                    api_client=self._api_client,
                    session_factory=self._session_factory,
                    llm_client=self._llm,
                    model=model_id,
                    vision_llm_client=self._vision_client,
                    vision_model=self._vision_model,
                    summary_llm_client=self._summary_client,
                    summary_model=self._summary_model,
                    media_gen=self._media_gen,
                    saga=saga,
                    log_policy_allowed=self._log_policy_allowed,
                    tool_guardrails=tool_guardrails,
                    bundle=self._bundle,
                    turn_gate=self._turn_gate,
                )

            def _reset_streaming_executor() -> Callable[[dict[str, Any]], None]:
                nonlocal streaming_executor
                if streaming_executor is not None:
                    streaming_executor.discard()
                streaming_executor = _make_streaming_executor()
                return streaming_executor.add_tool

            if self._streaming_enabled:
                streaming_executor = _make_streaming_executor()
                on_tool_call_cb = streaming_executor.add_tool

            try:
                assistant_message, usage_data = await call_llm_with_retry(
                    session=session,
                    create_kwargs=create_kwargs,
                    iteration=iteration,
                    turn_id=turn_id,
                    iteration_index=turn_iteration_index,
                    llm_client=self._llm,
                    store=self._store,
                    streaming_enabled=self._streaming_enabled,
                    interrupt_check=self._check_interrupt,
                    rotate_credential=self._try_rotate_credential,
                    activate_fallback=self._try_activate_fallback,
                    get_current_model=lambda: self._current_model,
                    set_streaming_enabled=self._set_streaming_enabled,
                    compress_context=self._compress_context_callback(
                        session, messages, system_prompt, lease,
                    ),
                    context_compressor=self._compressor,
                    on_tool_call_complete=on_tool_call_cb,
                    on_stream_retry=(
                        _reset_streaming_executor
                        if self._streaming_enabled else None
                    ),
                    rate_limit_guard=self._provider_rate_limit_guard(),
                )
            except Exception as exc:
                logger.exception(
                    "LLM call failed for session %s (iteration %d, model %s): %s",
                    session.id,
                    iteration,
                    model_id,
                    exc,
                )
                info = classify_harness_error(exc)
                await self._store.emit_event(
                    session.id,
                    EventType.HARNESS_CRASH,
                    {
                        "worker_id": self._worker_id,
                        "error": f"LLM call failed: {exc}",
                        "iteration": iteration,
                        "error_category": info.category,
                        "error_title": info.title,
                        "error_detail": info.detail,
                        "retryable": info.retryable,
                    },
                )
                raise

            # 3. Coerce content to string (local backends may return dict/list).
            coerce_message_content(assistant_message)

            # 3a. Extract reasoning / thinking blocks.
            reasoning_text = extract_reasoning(assistant_message)
            if reasoning_text:
                await self._store.emit_event(
                    session.id,
                    EventType.LLM_THINKING,
                    {
                        "reasoning": reasoning_text,
                        "turn_id": turn_id,
                        "iteration_index": turn_iteration_index,
                    },
                )
                # Strip thinking blocks from content before storing.
                strip_think_blocks(assistant_message)

            # 3b. Incomplete scratchpad detection — model ran out of tokens
            # mid-reasoning. Retry up to 2 times.
            if has_incomplete_scratchpad(assistant_message):
                incomplete_scratchpad_retries += 1
                if incomplete_scratchpad_retries <= 2:
                    logger.info(
                        "Session %s: incomplete REASONING_SCRATCHPAD, retrying (%d/2)",
                        session.id, incomplete_scratchpad_retries,
                    )
                    if streaming_executor is not None:
                        streaming_executor.discard()
                    self._budget.refund()
                    continue
                logger.warning(
                    "Session %s: incomplete REASONING_SCRATCHPAD after 2 retries, proceeding",
                    session.id,
                )
            else:
                incomplete_scratchpad_retries = 0

            # 3c. Thinking-only response — model produced reasoning but no
            # visible content. Retry with thinking visible, or fall back to
            # cached content from a prior content-with-tools turn.
            if is_thinking_only_response(assistant_message):
                thinking_prefill_retries += 1
                if thinking_prefill_retries <= 2:
                    logger.info(
                        "Session %s: thinking-only response, retrying (%d/2)",
                        session.id, thinking_prefill_retries,
                    )
                    # Append the thinking as a visible assistant turn so the
                    # model can see its own reasoning on the next attempt.
                    if reasoning_text:
                        messages.append({
                            "role": "assistant",
                            "content": f"[My reasoning so far: {reasoning_text[:2000]}]",
                        })
                        messages.append({
                            "role": "user",
                            "content": "Please provide your actual response based on the reasoning above.",
                        })
                    if streaming_executor is not None:
                        streaming_executor.discard()
                    self._budget.refund()
                    continue
                # Exhausted retries — try content-with-tools fallback.
                cached = content_with_tools_cache.get_fallback()
                if cached:
                    logger.info(
                        "Session %s: using cached content-with-tools response (%d chars)",
                        session.id, len(cached),
                    )
                    assistant_message["content"] = cached
                    content_with_tools_cache.clear()
            else:
                thinking_prefill_retries = 0

            # 3d. Cache content from turns that have both content and tool calls.
            content_with_tools_cache.maybe_cache(assistant_message)

            # 3e. Preserve reasoning_details for multi-turn reasoning continuity.
            # Providers like OpenRouter/Anthropic include opaque fields (signature,
            # encrypted_content) that must be passed back on subsequent turns.
            reasoning_details = assistant_message.get("reasoning_details")
            if reasoning_details:
                # Keep in the message dict so it's sent back on the next API call.
                logger.debug(
                    "Session %s: preserving %d reasoning_details entries",
                    session.id, len(reasoning_details),
                )

            tool_calls_raw = assistant_message.get("tool_calls")

            # 4. Emit LLM_RESPONSE event with usage data.
            input_tokens = usage_data.get("input_tokens", 0)
            output_tokens = usage_data.get("output_tokens", 0)
            finish_reason = usage_data.get("finish_reason", "stop")

            reasoning_tokens = usage_data.get("reasoning_tokens", 0)
            cache_read_tokens = usage_data.get("cache_read_tokens", 0)

            response_data: dict[str, Any] = {
                "message": assistant_message,
                "model": usage_data.get("model", model_id),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "cache_read_tokens": cache_read_tokens,
                "finish_reason": finish_reason,
                "context_window": self._compressor.context_length,
            }

            # Compute cost estimate.
            from surogates.harness.model_metadata import estimate_cost

            cost = estimate_cost(model_id, input_tokens, output_tokens)
            if cost > 0:
                response_data["cost_usd"] = cost

            # Record in session cost tracker.
            if cost_tracker is not None:
                cost_tracker.record_call(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    cache_read_tokens=cache_read_tokens,
                    reasoning_tokens=reasoning_tokens,
                )

            if (
                not tool_calls_raw
                and finish_reason == "stop"
                and (
                    inbox_rescue_kind := await self._maybe_route_final_response_to_inbox(
                        session=session,
                        messages=messages,
                        assistant_message=assistant_message,
                        model=model_id,
                        tool_filter=tool_filter,
                    )
                )
            ):
                if inbox_rescue_kind == "ask_user_question":
                    tool_calls_raw = assistant_message.get("tool_calls")
                    finish_reason = "tool_calls"
                    usage_data["finish_reason"] = finish_reason
                    response_data["finish_reason"] = finish_reason
                    response_data["ask_user_question_rescue"] = True
                elif inbox_rescue_kind == "action_required":
                    response_data["action_required_rescue"] = True

            # 4a. Interrupt guard.  Abort only on an explicit interrupt
            # (Stop / pause / lease loss).  A user.message that arrived
            # mid-stream is no longer dropped — it is folded in as a new
            # user turn at the next iteration boundary by the steer
            # injector, so the buffered response is delivered, not discarded.
            if self._check_interrupt():
                await self._abort_iteration_with_pause(session, saga)
                return

            response_data["turn_id"] = turn_id
            response_data["iteration_index"] = turn_iteration_index
            event_id = await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                response_data,
            )

            if tool_calls_raw and usage_data.get("partial_tool_call"):
                logger.warning(
                    "Session %s: partial tool-call arguments for %s; "
                    "returning recovery tool results instead of executing",
                    session.id,
                    usage_data.get("partial_tool_names") or [],
                )
                if streaming_executor is not None:
                    streaming_executor.discard()
                self._budget.refund()
                messages.append(assistant_message)
                messages.extend(build_partial_tool_call_recovery_results(tool_calls_raw))
                invalid_json_retries = 0
                continue

            # 4a. Length continuation with prefix accumulation.
            # When finish_reason == "length", the response was truncated. We
            # accumulate the partial content and ask the model to continue.
            # However, if the model spent all its output tokens on reasoning
            # (thinking budget exhaustion), continuation retries are pointless.
            if (
                finish_reason == "length"
                and is_thinking_budget_exhausted(assistant_message)
            ):
                logger.warning(
                    "Session %s: thinking budget exhausted — model used all "
                    "output tokens on reasoning with none left for the response",
                    session.id,
                )
                assistant_message["content"] = (
                    "The model used all its output tokens on reasoning "
                    "and had none left for the actual response. "
                    "Try lowering reasoning effort or increasing max_tokens."
                )
                messages.append(assistant_message)
                await self._complete_session(
                    session, messages, lease, reason="thinking_budget_exhausted",
                    through_event_id=event_id,
                    cost_tracker=cost_tracker,
                    turn_id=turn_id,
                )
                return

            if finish_reason == "length" and length_continuation_count < _MAX_LENGTH_CONTINUATIONS:
                partial_content = assistant_message.get("content", "") or ""
                length_continuation_prefix += partial_content
                logger.info(
                    "Session %s: finish_reason='length', accumulated %d chars, "
                    "injecting continuation prompt (%d/%d)",
                    session.id, len(length_continuation_prefix),
                    length_continuation_count + 1, _MAX_LENGTH_CONTINUATIONS,
                )
                # Append the partial assistant message so the model sees
                # what it already produced.
                messages.append(assistant_message)
                messages.append({"role": "user", "content": _LENGTH_CONTINUATION_PROMPT})
                length_continuation_count += 1
                if streaming_executor is not None:
                    streaming_executor.discard()
                continue  # re-enter the loop

            # If we had accumulated a prefix, prepend it to the final content.
            if length_continuation_prefix:
                final_content = assistant_message.get("content", "") or ""
                assistant_message["content"] = length_continuation_prefix + final_content
                length_continuation_prefix = ""

            # Reset length continuation counter on a normal finish.
            length_continuation_count = 0

            # 5. If no tool calls -> session turn is complete.
            if not tool_calls_raw:
                final_content = (assistant_message.get("content") or "").strip()

                # Check if response only has thinking blocks with no actual
                # content after them.
                visible_content = THINK_RE.sub("", final_content).strip() if final_content else ""

                if not visible_content:
                    # If the previous turn already delivered real content
                    # alongside tool calls (e.g. "You're welcome!" + memory
                    # save), the model has nothing more to say.  Use the
                    # earlier content immediately instead of wasting API
                    # calls on retries that won't help.
                    cached_fallback = content_with_tools_cache.get_fallback()
                    if cached_fallback:
                        logger.debug(
                            "Session %s: empty follow-up after tool calls "
                            "-- using prior turn content as final response",
                            session.id,
                        )
                        assistant_message["content"] = THINK_RE.sub(
                            "", cached_fallback,
                        ).strip()
                        content_with_tools_cache.clear()
                    else:
                        # Thinking-only prefill continuation -- the model
                        # produced structured reasoning (via API fields)
                        # but no visible text content.  Rather than giving
                        # up, append the assistant message as-is and
                        # continue -- the model will see its own reasoning
                        # on the next turn and produce the text portion.
                        _has_structured = bool(
                            assistant_message.get("reasoning")
                            or assistant_message.get("reasoning_content")
                            or assistant_message.get("reasoning_details")
                        )
                        if _has_structured and thinking_prefill_retries < 2:
                            thinking_prefill_retries += 1
                            logger.info(
                                "Session %s: thinking-only final response, "
                                "prefilling to continue (%d/2)",
                                session.id, thinking_prefill_retries,
                            )
                            interim_msg = dict(assistant_message)
                            interim_msg["_thinking_prefill"] = True
                            messages.append(interim_msg)
                            continue

                        # Truly empty response -- no content, no tool calls,
                        # no structured reasoning.  Some models (observed
                        # with gpt-5.4-mini) stall on complex asks like SVG
                        # generation and return a 4-token no-op.  Retry a
                        # few times with a nudge; if still empty, fail the
                        # session so the UI's failure path engages.
                        if empty_response_retries < _MAX_EMPTY_RESPONSE_RETRIES:
                            empty_response_retries += 1
                            logger.warning(
                                "Session %s: empty LLM response, retrying "
                                "(%d/%d)",
                                session.id,
                                empty_response_retries,
                                _MAX_EMPTY_RESPONSE_RETRIES,
                            )
                            messages.append({
                                "role": "user",
                                "content": _EMPTY_RESPONSE_NUDGE,
                            })
                            continue

                        logger.error(
                            "Session %s: LLM returned empty response %d "
                            "times; emitting session.fail",
                            session.id, _MAX_EMPTY_RESPONSE_RETRIES,
                        )
                        await self._store.emit_event(
                            session.id,
                            EventType.SESSION_FAIL,
                            {
                                "reason": "empty_llm_response",
                                "attempts": _MAX_EMPTY_RESPONSE_RETRIES,
                            },
                        )
                        try:
                            await self._store.update_session_status(
                                session.id, "failed",
                            )
                        except Exception:
                            logger.warning(
                                "Failed to update session status to failed "
                                "for %s", session.id, exc_info=True,
                            )
                        return

                # Pop thinking-only prefill message(s) before appending
                # the final response.  This avoids consecutive assistant
                # messages which break strict-alternation providers
                # (Anthropic Messages API) and keeps history clean.
                while (
                    messages
                    and isinstance(messages[-1], dict)
                    and messages[-1].get("_thinking_prefill")
                ):
                    messages.pop()

                messages.append(assistant_message)

                # If the model emitted an SVG / HTML as a fenced code
                # block instead of calling ``create_artifact``, promote
                # it into a real artifact so the user sees the rendered
                # output alongside the source.  This fires only on the
                # final, no-tool-calls response of the turn.
                await self._promote_fenced_artifacts(
                    session,
                    (assistant_message.get("content") or ""),
                    messages,
                )

                if await self._maybe_continue_outcome(
                    session,
                    lease,
                    latest_response=assistant_message.get("content") or "",
                    response_event_id=event_id,
                    model=model_id,
                ):
                    return

                # /mission evaluator — only fires when triggered (a
                # mission task reached terminal state, or the coordinator
                # emitted the [[mission-complete]] marker). Failures here
                # must not break the response path; log and continue.
                try:
                    await self._maybe_run_mission_evaluator_for_session(
                        session=session,
                        latest_response=assistant_message.get("content") or "",
                        model=model_id,
                    )
                except Exception:
                    logger.exception(
                        "Mission evaluator hook failed for session %s; continuing",
                        session.id,
                    )

                # If the mission is still in flight, do NOT complete the
                # session — the worker.complete events that follow will
                # re-wake the coordinator so the evaluator hook above can
                # fire on the next no-tool-call response. Completing here
                # would set status=completed, and the next wake would bail
                # at the top of process_wake_cycle, leaving the mission
                # active forever even after its verifier task finishes.
                if await self._mission_has_pending_work(session.id):
                    logger.debug(
                        "Session %s: mission has in-flight tasks; deferring completion",
                        session.id,
                    )
                    return

                # One-shot deep-research orphan-completion guard.  The
                # planner is supposed to end its work by calling
                # ``delegate_task(agent_type="research-writer")``.  In
                # the wild we have seen the model describe the handoff
                # in prose without emitting the tool call -- it stops,
                # the harness completes the session, and the report is
                # never written.  Re-prompt once with an explicit
                # reminder; if it still doesn't delegate, fall through
                # to the normal completion path.
                if (
                    not deep_research_delegate_nudge_fired
                    and _is_deep_research_planner(session)
                    and not _planner_already_delegated_to_writer(messages)
                ):
                    deep_research_delegate_nudge_fired = True
                    try:
                        await self._store.emit_event(
                            session.id, EventType.USER_MESSAGE,
                            {
                                "content": DEEP_RESEARCH_NO_DELEGATE_NUDGE,
                                "synthetic": "deep_research_no_delegate_nudge",
                            },
                        )
                    except Exception:
                        logger.exception(
                            "Failed to persist deep-research nudge event for "
                            "session %s; in-memory nudge still applied",
                            session.id,
                        )
                    messages.append({
                        "role": "user",
                        "content": DEEP_RESEARCH_NO_DELEGATE_NUDGE,
                    })
                    self._budget.refund()
                    logger.info(
                        "Session %s: deep-research planner stopped without "
                        "delegating; injecting one-shot nudge",
                        session.id,
                    )
                    continue

                # Text-only iteration: kick off the iteration-summary
                # task before _complete_session so the drain in A8 can
                # await it.
                await self._maybe_summarize_iteration(
                    session_id=session.id,
                    turn_id=turn_id,
                    iteration_index=turn_iteration_index,
                    reasoning_text=reasoning_text or "",
                    tool_calls=[],
                    started_at=iteration_started_at,
                )

                # Before completing, fold in any follow-up that arrived while
                # this final response was being produced.  Deliver the
                # response (already emitted + appended above), then keep the
                # wake going as a new user turn instead of completing and
                # re-waking.
                followup, steer_cursor = await self._collect_steer_messages(
                    session.id, steer_cursor,
                )
                if followup is not None:
                    messages.append(followup)
                    turn_id = str(uuid4())
                    turn_base_iteration = iteration
                    self._pending_iteration_summary_tasks = {}
                    self._completed_iteration_summaries = {}
                    logger.info(
                        "Session %s: follow-up arrived at completion; "
                        "continuing as a new turn (turn_id=%s)",
                        session.id, turn_id,
                    )
                    continue

                # A response without tool calls completes the current
                # objective.  Follow-up messages revive the session into a
                # new objective rather than keeping completed work "active".
                await self._complete_session(
                    session, messages, lease, reason="completed",
                    through_event_id=event_id,
                    cost_tracker=cost_tracker,
                    turn_id=turn_id,
                    user_message=_latest_user_message_text(messages),
                )
                return

            # Response had tool calls, so it was not empty — reset the
            # empty-response retry counter so each "empty spell" gets a
            # fresh budget rather than accumulating across the session.
            empty_response_retries = 0

            # Determine whether to use the streaming executor for this turn.
            # The executor is used when it has tools (i.e., streaming was
            # active and tool blocks were detected during the stream).
            use_streaming_exec = (
                streaming_executor is not None
                and streaming_executor.has_tools
            )

            # 5a. Invalid JSON retry — if ALL tool calls have unparseable
            # JSON, retry the API call instead of sending error results.
            # When the streaming executor is active, skip this — the
            # executor's execute_single_tool handles parse errors naturally.
            if not use_streaming_exec:
                all_json_invalid = tool_calls_raw and all(
                    not _is_valid_json_args(tc) for tc in tool_calls_raw
                )
                if all_json_invalid and invalid_json_retries < 3:
                    invalid_json_retries += 1
                    logger.warning(
                        "Session %s: all %d tool calls have invalid JSON args, "
                        "retrying API call (%d/3)",
                        session.id, len(tool_calls_raw), invalid_json_retries,
                    )
                    self._budget.refund()  # don't count this iteration
                    continue  # re-enter the loop without appending anything
                invalid_json_retries = 0  # reset on a turn with valid args

            # 5b. Deduplicate tool calls and cap delegate_task calls.
            tool_calls_raw = deduplicate_tool_calls(tool_calls_raw)
            tool_calls_raw = cap_delegate_calls(tool_calls_raw)
            assistant_message["tool_calls"] = tool_calls_raw

            # 5c. Invalid tool call recovery — check for unknown tools
            # or malformed JSON before executing (with fuzzy name repair).
            # Skipped when the streaming executor is active because some
            # tools may have already started executing during streaming.
            # Invalid calls get natural error results from execute_single_tool.
            if not use_streaming_exec:
                invalid_calls = self._find_invalid_tool_calls(tool_calls_raw)
                if invalid_calls:
                    consecutive_invalid_tool_calls += 1
                    if consecutive_invalid_tool_calls >= _MAX_CONSECUTIVE_INVALID_TOOL_CALLS:
                        logger.error(
                            "Session %s: aborting after %d consecutive invalid tool calls",
                            session.id, consecutive_invalid_tool_calls,
                        )
                        await self._complete_session(
                            session, messages, lease, reason="invalid_tool_calls",
                            through_event_id=event_id,
                            cost_tracker=cost_tracker,
                            turn_id=turn_id,
                        )
                        return

                    # Return helpful error messages without consuming budget.
                    self._budget.refund()
                    messages.append(assistant_message)
                    for tc, error_msg in invalid_calls:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": error_msg,
                        })
                    continue
                else:
                    consecutive_invalid_tool_calls = 0

            # 6. Pop thinking-only prefill message(s) before appending
            # the tool-call assistant message (same rationale as the
            # final-response path).
            while (
                messages
                and isinstance(messages[-1], dict)
                and messages[-1].get("_thinking_prefill")
            ):
                messages.pop()

            # Append assistant message to the in-memory message list.
            messages.append(assistant_message)

            # 7. Execute tool calls.
            # Checkpoint before file-mutating tools (write_file, patch).
            # The checkpoint hash is stashed on the tool call dict so
            # execute_single_tool can include it in the TOOL_CALL event,
            # enabling the web UI to offer per-tool-call rollback.
            await self._inject_checkpoint_hashes(tool_calls_raw, session)

            if use_streaming_exec:
                # ── Streaming executor path ──────────────────────────
                # Some or all tools started executing during LLM streaming.
                # Checkpoint hashes were injected above — non-concurrent
                # tools (write_file, patch) are still QUEUED at this point
                # because they are never concurrency-safe.

                # Wait for all tools to complete (concurrent ones may
                # already be done, sequential ones start now). Publish the
                # executor on the harness so ``interrupt()`` can preempt
                # in-flight tools (sandbox exec, browser actions) instead
                # of letting them run to their tool-level timeout.
                self._active_executor = streaming_executor
                try:
                    all_results = await streaming_executor.get_all_results()
                finally:
                    self._active_executor = None

                # Filter results to match the deduped tool call list.
                # Dedup is rare but possible — if a tool was deduped,
                # its result is harmlessly discarded (read-only tools
                # have no side effects).
                valid_ids = {tc.get("id") for tc in tool_calls_raw}
                tool_results = [
                    r for r in all_results
                    if r.get("tool_call_id") in valid_ids
                ]

                # Log streaming executor stats for observability.
                stats = streaming_executor.stats
                if stats["overlapped_with_streaming"] > 0:
                    logger.info(
                        "Session %s: streaming executor completed — "
                        "%d/%d tools overlapped with streaming",
                        session.id,
                        stats["overlapped_with_streaming"],
                        stats["total"],
                    )
            else:
                # ── Existing path ────────────────────────────────────
                tool_results = await execute_tool_calls(
                    tool_calls_raw,
                    session=session,
                    lease=lease,
                    store=self._store,
                    tools=self._tools,
                    tenant=self._tenant,
                    interrupt_check=self._check_interrupt,
                    redis=self._redis,
                    budget=self._budget,
                    memory_manager=self._memory_manager,
                    hint_tracker=hint_tracker,
                    sandbox_pool=self._sandbox_pool,
                    credential_vault=self._credential_vault,
                    browser_pool=self._browser_pool,
                    browser_control=self._browser_control,
                    storage=self._storage,
                    api_client=self._api_client,
                    session_factory=self._session_factory,
                    llm_client=self._llm,
                    model=model_id,
                    vision_llm_client=self._vision_client,
                    vision_model=self._vision_model,
                    summary_llm_client=self._summary_client,
                    summary_model=self._summary_model,
                    media_gen=self._media_gen,
                    saga=saga,
                    log_policy_allowed=self._log_policy_allowed,
                    bundle=self._bundle,
                    turn_gate=self._turn_gate,
                )

            dynamic_loop_wait_done = self._dynamic_loop_wait_succeeded(
                session, tool_calls_raw, tool_results,
            )

            # 7a. Reset nudge counters when relevant tools are used
            for tr_tc in tool_calls_raw:
                tc_name = tr_tc.get("function", {}).get("name", "")
                if tc_name == "memory":
                    self._turns_since_memory = 0
                elif tc_name == "skill_manage":
                    self._iters_since_skill = 0

            # 7b. Layer 3: enforce aggregate turn budget -- persist oversized results.
            from surogates.tools.utils.tool_result_storage import enforce_turn_budget
            tool_results = enforce_turn_budget(tool_results)

            # 7c. Budget pressure warning -- inject into the last tool result.
            tool_results = inject_budget_warning(tool_results, self._budget)

            # 8. Append tool results to messages.
            messages.extend(tool_results)

            # A guardrail hard stop ends the turn: continuing would let
            # the model re-issue the same call, and providers reject
            # conversations that accumulate identical consecutive tool
            # calls. The halt guidance is already in the last tool result.
            if tool_guardrails.halt_decision is not None:
                halt = tool_guardrails.halt_decision
                logger.warning(
                    "Session %s: tool loop guardrail halt (%s, count=%d) — "
                    "ending turn",
                    session.id, halt.code, halt.count,
                )
                await self._complete_session(
                    session, messages, lease,
                    reason="tool_loop_halt",
                    cost_tracker=cost_tracker,
                    turn_id=turn_id,
                )
                return

            last_tool_name = ""
            for tc in reversed(tool_calls_raw):
                last_tool_name = tc.get("function", {}).get("name", "")
                if last_tool_name:
                    break
            await self._maybe_emit_progress_checkin(
                session,
                messages,
                iteration_count=iteration,
                last_tool=last_tool_name,
            )

            # 8a. Memory manager: sync turn to external providers.
            if self._memory_manager is not None:
                try:
                    # Extract user content from the last user message in the history.
                    user_content = ""
                    for m in reversed(messages):
                        if m.get("role") == "user":
                            user_content = m.get("content", "")
                            break
                    assistant_content = assistant_message.get("content", "") or ""
                    self._memory_manager.sync_all(user_content, assistant_content)
                    await self._memory_manager.flush_async_providers()
                except Exception:
                    logger.debug("Memory manager sync_all failed", exc_info=True)

            if dynamic_loop_wait_done:
                await self._complete_session(
                    session,
                    messages,
                    lease,
                    reason="loop_wait",
                    cost_tracker=cost_tracker,
                    turn_id=turn_id,
                )
                return

            # 9. Check if compression is needed.
            if self._compressor.should_compress(messages, system_prompt):
                # Memory manager: extract insights before compression and feed
                # them into the summary so they survive compaction.
                pre_compress_text = ""
                if self._memory_manager is not None:
                    try:
                        pre_compress_text = self._memory_manager.on_pre_compress(messages)
                    except Exception:
                        logger.debug("Memory manager on_pre_compress failed", exc_info=True)

                compressed, summary_data = await self._compressor.compress(
                    messages, self._llm, pre_compress_guidance=pre_compress_text,
                )
                await self._store.emit_event(
                    session.id,
                    EventType.CONTEXT_COMPACT,
                    {
                        **summary_data,
                        "compacted_messages": compressed,
                    },
                )
                messages = compressed
                # Invalidate system prompt cache -- conversation shape changed.
                self._system_prompt_cache.invalidate(session.id)
                self._memory_snapshot_cache.pop(session.id, None)

            # Lease renewal is handled by a background task started in
            # ``wake()``; no per-iteration renewal needed here.

            # Tool batch resolved — kick off the iteration summary
            # before the next iteration starts. Fire-and-forget so the
            # next LLM call isn't blocked on the cheap summarizer.
            # Pass tool_results so the summarizer can distinguish
            # identical-looking calls by their outcome (e.g. four
            # `python3 -c \"...\"` calls that inspect different
            # things should get four different labels).
            await self._maybe_summarize_iteration(
                session_id=session.id,
                turn_id=turn_id,
                iteration_index=turn_iteration_index,
                reasoning_text=reasoning_text or "",
                tool_calls=tool_calls_raw or [],
                started_at=iteration_started_at,
                tool_results=tool_results or [],
            )

        # --- Post-loop skill nudge check ---
        should_review_skills = False
        if (
            self._skill_nudge_interval > 0
            and self._iters_since_skill >= self._skill_nudge_interval
        ):
            should_review_skills = True
            self._iters_since_skill = 0

        # --- Background memory/skill review ---
        # In the server architecture this translates to emitting a review
        # event so the worker can process it asynchronously.
        if should_review_memory or should_review_skills:
            try:
                await self._store.emit_event(
                    session.id,
                    EventType.HARNESS_WAKE,
                    {
                        "worker_id": self._worker_id,
                        "review_memory": should_review_memory,
                        "review_skills": should_review_skills,
                    },
                )
            except Exception:
                logger.debug("Background review event emission failed", exc_info=True)

        # Budget exhausted after the while loop.  Request one final
        # summary with no tools.
        await self._request_final_summary(
            session, messages, system_prompt, lease,
            cost_tracker=cost_tracker,
            turn_id=turn_id,
            iteration_index=max(iteration - 1 - turn_base_iteration, 0),
        )

        # --- Saga finalization ---
        # Mark all active sagas as completed on normal loop exit.
        if saga is not None:
            await self._finalize_sagas(saga, session)

    # ------------------------------------------------------------------
    # Checkpoint injection
    # ------------------------------------------------------------------

    async def _inject_checkpoint_hashes(
        self,
        tool_calls: list[dict[str, Any]],
        session: Session,
    ) -> None:
        """Stash checkpoint hashes on file-mutating tool call dicts.

        Before ``write_file`` or ``patch`` execute, a filesystem snapshot
        is taken via the sandbox's ``_checkpoint`` command.  The resulting
        hash is stored on the tool call dict so ``execute_single_tool``
        can include it in the ``TOOL_CALL`` event, enabling per-tool-call
        rollback from the web UI.

        No-op when checkpoints are disabled or no sandbox is available.
        """
        if not self._checkpoints_enabled or self._sandbox_pool is None:
            return

        import json as _json

        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            if tool_name not in ("write_file", "patch"):
                continue
            try:
                args = _json.loads(fn.get("arguments", "{}"))
                file_path = args.get("path", "")
                if not file_path:
                    continue
                cp_input = _json.dumps({
                    "action": "take",
                    "reason": f"before {tool_name}",
                    "file_path": file_path,
                })
                cp_result = await self._sandbox_pool.execute(
                    sandbox_session_key(session), "_checkpoint", cp_input,
                )
                cp_data = _json.loads(cp_result)
                cp_hash = cp_data.get("hash")
                if cp_hash:
                    tc["_checkpoint_hash"] = cp_hash
            except Exception:
                logger.debug("Checkpoint before %s failed", tool_name, exc_info=True)

    # ------------------------------------------------------------------
    # Saga lifecycle helpers
    # ------------------------------------------------------------------

    async def _finalize_sagas(self, saga: Any, session: Any) -> None:
        """Finalize all active sagas on normal loop completion.

        Marks active sagas as COMPLETED and emits SAGA_COMPLETE events.
        """
        from surogates.governance.events import saga_complete_event
        from surogates.governance.saga.state_machine import SagaState

        for active in list(saga.active_sagas):
            try:
                active.transition(SagaState.COMPLETED)
                await self._store.emit_event(
                    session.id,
                    EventType.SAGA_COMPLETE,
                    saga_complete_event(
                        active.saga_id,
                        status="completed",
                        steps_executed=len(active.steps),
                    ),
                )
            except Exception:
                logger.debug(
                    "Failed to finalize saga %s", active.saga_id, exc_info=True,
                )

    async def _compensate_sagas(self, saga: Any, session: Any, reason: str) -> None:
        """Compensate all active sagas on interrupt/crash/failure.

        Runs compensation for committed steps in reverse order and emits
        SAGA_COMPENSATE events.  Checkpoint restores go through the
        sandbox pool (same path as ``_checkpoint`` take/restore).
        """
        from functools import partial

        from surogates.governance.events import saga_compensate_event
        from surogates.governance.saga.compensator import compensate_step
        from surogates.governance.saga.state_machine import SagaState

        for active in list(saga.active_sagas):
            # Guard against double-compensation: if a prior crash happened
            # mid-compensation, reconstruction leaves the saga in
            # COMPENSATING state.  Skip it — the committed steps that
            # were already compensated are in terminal states and the
            # remaining ones will be picked up by a future attempt.
            if active.state == SagaState.COMPENSATING:
                logger.warning(
                    "Saga %s already compensating (prior crash?) — skipping",
                    active.saga_id,
                )
                continue

            try:
                # Ensure the sandbox is still available for compensation
                # (it may have been destroyed on a prior crash).
                if self._sandbox_pool is not None:
                    try:
                        from surogates.harness.tool_exec import _build_session_sandbox_spec
                        sandbox_owner = sandbox_session_key(session)
                        sandbox_spec = _build_session_sandbox_spec(
                            session, self._tenant, sandbox_owner,
                        )
                        await self._sandbox_pool.ensure(sandbox_owner, sandbox_spec)
                    except Exception:
                        logger.warning(
                            "Cannot provision sandbox for saga compensation "
                            "in session %s — marking saga as escalated",
                            session.id,
                        )
                        active.transition(SagaState.ESCALATED)
                        active.error = "Sandbox unavailable for compensation"
                        continue

                # Capture count before compensate() transitions steps
                # away from COMMITTED (after which committed_steps is empty).
                committed_count = len(active.committed_steps)
                compensator = partial(
                    compensate_step,
                    sandbox_pool=self._sandbox_pool,
                    session_id=sandbox_session_key(session),
                )
                failed = await saga.compensate(active.saga_id, compensator)
                failed_ids = [s.step_id for s in failed]
                await self._store.emit_event(
                    session.id,
                    EventType.SAGA_COMPENSATE,
                    saga_compensate_event(
                        active.saga_id,
                        steps_rolled_back=committed_count - len(failed),
                        reason=reason,
                        failed_steps=failed_ids if failed_ids else None,
                    ),
                )
            except Exception:
                logger.exception(
                    "Saga compensation failed for %s", active.saga_id,
                )

    # ------------------------------------------------------------------
    # Credential rotation and fallback (delegates to resilience module)
    # ------------------------------------------------------------------

    def _try_rotate_credential(
        self,
        status_code: int,
        exc: Exception,
        error_context: dict[str, Any] | None = None,
    ) -> bool:
        """Try to rotate to the next credential in the pool."""
        new_client, rotated = try_rotate_credential(
            self._credential_pool, self._llm, status_code, exc,
            error_context=error_context,
        )
        if rotated and new_client is not None:
            self._llm = new_client
            return True
        return False

    def _try_activate_fallback(self) -> bool:
        """Switch to the next fallback in the chain. Returns True if activated."""
        new_client, new_model, new_index, primary_config, activated = try_activate_fallback(
            self._fallback_chain,
            self._fallback_index,
            self._llm,
            self._primary_config,
            self._current_model,
            self._fallback_activated,
        )
        if new_client is None:
            return False
        self._llm = new_client
        self._current_model = new_model
        self._fallback_index = new_index
        self._primary_config = primary_config
        self._fallback_activated = activated
        return True

    def _provider_rate_limit_guard(self) -> ProviderRateLimitGuard | None:
        """Return a Redis-backed guard keyed to the active LLM provider."""
        if self._redis is None:
            return None

        provider_key = ""
        if self._primary_config:
            provider_key = str(
                self._primary_config.get("provider")
                or self._primary_config.get("base_url")
                or ""
            )
        if not provider_key:
            provider_key = str(
                getattr(self._llm, "base_url", "")
                or self._current_model
                or self._default_model
            )
        return ProviderRateLimitGuard(self._redis, provider_key)

    # ------------------------------------------------------------------
    # Invalid tool call detection (delegates to resilience module)
    # ------------------------------------------------------------------

    def _find_invalid_tool_calls(
        self, tool_calls: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], str]]:
        """Return list of (tool_call, error_message) for invalid calls."""
        return find_invalid_tool_calls(tool_calls, self._tools)

    async def _maybe_route_final_response_to_inbox(
        self,
        *,
        session: Session,
        messages: list[dict],
        assistant_message: dict[str, Any],
        model: str,
        tool_filter: set[str] | None,
    ) -> str | None:
        """Route final plain-text user blocks into the appropriate inbox path.

        Text answers become ask_user_question tool calls. User actions such
        as login or approval become first-class action_required inbox items.
        """
        content = (assistant_message.get("content") or "").strip()
        if not content:
            return None
        if assistant_message.get("tool_calls"):
            return None
        if session.parent_id is not None or session.channel == "scheduled":
            return None
        # Route for either principal: user sessions (agent chat) and
        # service-account sessions (ops chats) both get the judge rescue.
        if session.user_id is None and session.service_account_id is None:
            return None

        decision = await self._judge_final_response_user_action(
            messages=messages,
            assistant_content=content,
            model=model,
        )
        action_kind = str(decision.get("action_kind") or "").strip()
        if not action_kind:
            action_kind = (
                "ask_user_question"
                if decision.get("needs_ask_user_question")
                else "none"
            )
        if action_kind == "none":
            return None

        if action_kind == "action_required":
            instructions = str(decision.get("instructions") or "").strip()
            if not instructions:
                instructions = str(decision.get("context") or content).strip()
            if not instructions:
                return None
            await self._store.emit_event(
                session.id,
                EventType.INBOX_ACTION_REQUIRED,
                {
                    "title": str(
                        decision.get("title") or "Action required"
                    ).strip(),
                    "instructions": instructions[:1000],
                    "context": str(
                        decision.get("context") or content
                    ).strip()[:1000],
                    "action_type": str(
                        decision.get("action_type") or "manual"
                    ).strip(),
                    "target": str(decision.get("target") or "session").strip(),
                    "reason": str(decision.get("reason") or "user_action"),
                },
            )
            logger.info(
                "Session %s: emitted action_required inbox item (reason=%s)",
                session.id,
                decision.get("reason") or "user_action",
            )
            return "action_required"

        if action_kind != "ask_user_question":
            return None
        if "ask_user_question" not in self._tools.tool_names:
            return None
        if tool_filter is not None and "ask_user_question" not in tool_filter:
            return None

        question = str(decision.get("question") or "").strip()
        if not question:
            return None
        context = str(decision.get("context") or content).strip()

        tool_call_id = f"call_ask_user_question_rescue_{uuid4().hex[:24]}"
        assistant_message["content"] = None
        assistant_message["tool_calls"] = [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "ask_user_question",
                    "arguments": json.dumps(
                        {
                            "questions": [
                                {
                                    "prompt": question[:1000],
                                    "allow_other": True,
                                },
                            ],
                            "context": context[:1000],
                        },
                        ensure_ascii=False,
                    ),
                },
            },
        ]
        logger.info(
            "Session %s: converted final response into ask_user_question tool call "
            "(reason=%s)",
            session.id,
            decision.get("reason") or "user_input",
        )
        return "ask_user_question"

    async def _maybe_convert_final_response_to_ask_user_question(
        self,
        *,
        session: Session,
        messages: list[dict],
        assistant_message: dict[str, Any],
        model: str,
        tool_filter: set[str] | None,
    ) -> bool:
        """Compatibility wrapper for tests/callers that only need
        ask_user_question."""
        routed = await self._maybe_route_final_response_to_inbox(
            session=session,
            messages=messages,
            assistant_message=assistant_message,
            model=model,
            tool_filter=tool_filter,
        )
        return routed == "ask_user_question"

    async def _judge_final_response_user_action(
        self,
        *,
        messages: list[dict],
        assistant_content: str,
        model: str,
    ) -> dict[str, Any]:
        """Ask the configured LLM whether a draft final response needs user input."""
        recent_messages = [
            {
                "role": str(m.get("role", "")),
                "content": self._text_excerpt(m.get("content"), limit=1000),
            }
            for m in messages[-6:]
            if isinstance(m, dict) and m.get("role") in {"user", "assistant", "tool"}
        ]
        judge_payload = {
            "recent_messages": recent_messages,
            "assistant_draft": assistant_content[:3000],
        }
        judge_messages = [
            {"role": "system", "content": _USER_ACTION_RESCUE_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(judge_payload, ensure_ascii=False),
            },
        ]
        structured = await _generate_user_action_rescue_structured(
            llm_client=self._llm,
            model=model,
            messages=judge_messages,
        )
        if structured is not None:
            return self._normalize_user_action_decision(structured)

        for attempt in range(2):
            try:
                response = await self._llm.chat.completions.create(
                    model=model,
                    messages=judge_messages,
                    temperature=0,
                    max_tokens=300,
                )
                content = self._extract_chat_message_content(response)
                parsed = self._parse_json_object(content)
                break
            except Exception as exc:
                if attempt == 0:
                    logger.info(
                        "User-action rescue judge returned unparsable output; "
                        "retrying once: %s",
                        exc,
                    )
                    judge_messages.append({
                        "role": "user",
                        "content": (
                            "Your previous judge response was empty or not "
                            "valid JSON. Return only the required JSON object."
                        ),
                    })
                    continue
                logger.warning(
                    "User-action rescue judge failed; leaving final response "
                    "unchanged: %s",
                    exc,
                )
                return {
                    "needs_ask_user_question": False,
                    "reason": "judge_error",
                }

        return self._normalize_user_action_decision(parsed)

    async def _judge_final_response_needs_ask_user_question(
        self,
        *,
        messages: list[dict],
        assistant_content: str,
        model: str,
    ) -> dict[str, Any]:
        """Compatibility wrapper for callers that only inspect the
        ask_user_question fields."""
        return await self._judge_final_response_user_action(
            messages=messages,
            assistant_content=assistant_content,
            model=model,
        )

    @staticmethod
    def _normalize_user_action_decision(parsed: dict[str, Any]) -> dict[str, Any]:
        action_kind = str(parsed.get("action_kind") or "").strip()
        decision_text = " ".join(
            str(parsed.get(key) or "")
            for key in (
                "reason",
                "question",
                "title",
                "instructions",
                "context",
                "action_type",
                "target",
            )
        )
        if action_kind not in {
            "", "none", "ask_user_question", "action_required",
        }:
            action_kind = ""
        if (
            action_kind in {"", "none"}
            and parsed.get("needs_ask_user_question")
        ):
            action_kind = (
                "action_required"
                if AgentHarness._looks_like_user_action_requirement(decision_text)
                else "ask_user_question"
            )
        elif not action_kind:
            action_kind = "none"
        action_type = parsed.get("action_type")
        target = parsed.get("target")
        if action_kind == "action_required":
            action_type = action_type or AgentHarness._infer_user_action_type(
                decision_text,
            )
            target = target or ("browser" if action_type == "browser" else "session")
        return {
            "action_kind": action_kind,
            "needs_ask_user_question": action_kind == "ask_user_question",
            "reason": str(parsed.get("reason") or "user_input"),
            "question": parsed.get("question"),
            "title": parsed.get("title"),
            "instructions": parsed.get("instructions"),
            "context": parsed.get("context"),
            "action_type": action_type,
            "target": target,
        }

    @staticmethod
    def _looks_like_user_action_requirement(text: str) -> bool:
        lowered = text.lower()
        action_markers = (
            "take over",
            "open the browser",
            "browser session",
            "sign in",
            "signin",
            "log in",
            "login",
            "mfa",
            "2fa",
            "oauth",
            "captcha",
            "enter your password",
            "enter your credentials",
            "authorize",
            "authorization",
            "consent",
            "approve in",
            "approval prompt",
            "permission prompt",
            "file picker",
            "complete the action",
            "complete this action",
            "manual action",
        )
        return any(marker in lowered for marker in action_markers)

    @staticmethod
    def _infer_user_action_type(text: str) -> str:
        lowered = text.lower()
        if any(
            marker in lowered
            for marker in (
                "browser",
                "sign in",
                "signin",
                "log in",
                "login",
                "mfa",
                "oauth",
                "captcha",
                "password",
                "2fa",
            )
        ):
            return "browser"
        if any(
            marker in lowered
            for marker in (
                "approve",
                "approval",
                "authorize",
                "authorization",
                "consent",
                "permission",
            )
        ):
            return "approval"
        return "manual"

    @staticmethod
    def _extract_chat_message_content(response: Any) -> str:
        choice = response.choices[0]
        message = choice.message
        if isinstance(message, dict):
            return str(
                message.get("content")
                or message.get("reasoning_content")
                or message.get("reasoning")
                or ""
            )
        return str(
            getattr(message, "content", None)
            or getattr(message, "reasoning_content", None)
            or getattr(message, "reasoning", None)
            or ""
        )

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        if not text:
            raise ValueError("User-action rescue judge returned empty content")
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end + 1]
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("User-action rescue judge returned non-object JSON")
        return parsed

    @staticmethod
    def _text_excerpt(value: Any, *, limit: int) -> str:
        if isinstance(value, str):
            text = value
        elif isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text") or ""))
                    elif item.get("text"):
                        parts.append(str(item.get("text")))
                else:
                    parts.append(str(item))
            text = "\n".join(parts)
        else:
            text = str(value or "")
        return text[:limit]



    def _ensure_always_available_tools(
        self,
        tool_filter: set[str] | None,
        *,
        explicit_allowed: bool = False,
    ) -> set[str] | None:
        """Keep platform control-plane tools available after filtering.

        When *explicit_allowed* is True the caller's allowlist came
        from an admin-defined ``allowed_tools`` config (typically an
        AgentDef's ``tools`` list).  Respect that contract: do not
        force-add ``ask_user_question`` to an allowlist that
        deliberately omits it.  Symptom in the wild: the
        research-writer sub-agent's AGENT.md lists only
        ``[research_memory, create_artifact]`` so the writer cannot
        ask the user clarifying questions.  Force-adding
        ``ask_user_question`` defeated that contract and the writer
        stalled the workflow asking format questions instead of
        producing the report.
        """
        if tool_filter is None:
            return None
        if explicit_allowed:
            return tool_filter
        if "ask_user_question" not in self._tools.tool_names:
            return tool_filter
        updated = set(tool_filter)
        updated.add("ask_user_question")
        return updated

    def _apply_mcp_schema_filter(
        self, tool_filter: set[str] | None, *, explicit_allowed: bool,
    ) -> set[str] | None:
        """Restrict model-visible MCP tool schemas to this agent's set.

        The worker shares one ``ToolRegistry`` across every agent it
        serves, so the registry can accumulate ``mcp__*`` tools
        discovered for *other* agents.  ``self._mcp_tool_names`` is the
        set discovered for this session's agent.

        When the registry holds no foreign ``mcp__*`` tools there is
        nothing to hide, so the caller's filter is returned verbatim —
        including ``None`` (the "no filter applied" contract that
        non-strict coordinator sessions rely on).  Only when a foreign
        MCP tool is present do we materialise and filter:

        * ``None`` is expanded to the full registry so the foreign
          ``mcp__*`` tools can be subtracted.
        * Without an explicit ``allowed_tools`` config, this agent's full
          discovered MCP set is advertised.
        * With an explicit ``allowed_tools`` config, only the ``mcp__*``
          entries that are BOTH allowed and discovered survive.
        """
        all_names = set(self._tools.tool_names)
        foreign_mcp = {
            t for t in all_names
            if t.startswith("mcp__") and t not in self._mcp_tool_names
        }
        if not foreign_mcp:
            return tool_filter

        base = all_names if tool_filter is None else set(tool_filter)
        non_mcp = {t for t in base if not t.startswith("mcp__")}
        if explicit_allowed:
            mcp_allowed = {
                t for t in base if t.startswith("mcp__")
            } & self._mcp_tool_names
        else:
            mcp_allowed = self._mcp_tool_names & all_names
        return non_mcp | mcp_allowed

    def _tool_filter_for_session(self, session: Session) -> set[str] | None:
        """Return the tool allow-list for a session."""
        config = session.config or {}
        explicit_allowed = bool(config.get("allowed_tools"))

        if config.get("coordinator"):
            tool_filter: set[str] | None = None
            # ``strict_coordinator`` is the structural-enforcement flag the
            # ``subagent-task-orchestrator`` skill assumes: implementation
            # tools (terminal, file I/O, web, browser, vision, KB) are
            # stripped so the LLM can only delegate, not "fix it quickly"
            # in-band.  ``/mission`` sets it; AgentDef-driven coordinators
            # leave it off and keep the legacy full-tool behaviour.
            if config.get("strict_coordinator"):
                from surogates.tools.builtin.coordinator import (
                    COORDINATOR_IMPLEMENTATION_TOOLS,
                )

                excluded = set(config.get("excluded_tools") or [])
                excluded.update(COORDINATOR_IMPLEMENTATION_TOOLS)
                # Research coordinators get READ access back for OBSERVE
                # forensics (failure logs, eval output). Writes, terminal,
                # web, and browser stay stripped — the strict-mode incident
                # class was the model DOING the work, which still needs tools
                # it does not have. Reads the user explicitly excluded stay
                # excluded.
                if config.get("active_research_run_id"):
                    user_excluded = set(config.get("excluded_tools") or [])
                    excluded -= (
                        {"read_file", "search_files", "list_files"}
                        - user_excluded
                    )
                tool_filter = set(self._tools.tool_names) - excluded
        elif explicit_allowed:
            tool_filter = set(config["allowed_tools"])
        else:
            from surogates.tools.builtin.coordinator import WORKER_EXCLUDED_TOOLS

            excluded = set(config.get("excluded_tools") or [])
            excluded.update(WORKER_EXCLUDED_TOOLS)
            tool_filter = set(self._tools.tool_names) - excluded

        # Any session running as one iteration of a schedule (``/loop`` or
        # cron_create-spawned) must not be able to create new schedules.
        # Otherwise the LLM can spawn nested cron jobs from inside a wake —
        # observed in the wild on a ``/loop 1m`` run that called
        # ``cron_create`` to build a parallel cron for the same task.
        is_scheduled_child = bool(config.get("scheduled_session_id"))
        if is_scheduled_child:
            if tool_filter is None:
                tool_filter = set(self._tools.tool_names)
            else:
                tool_filter = set(tool_filter)
            tool_filter.difference_update(_DYNAMIC_LOOP_EXCLUDED_TOOLS)
            if config.get("scheduled_dynamic_loop"):
                if "loop_wait" in self._tools.tool_names:
                    tool_filter.add("loop_wait")
                # Dynamic loops self-terminate via ``loop_wait(completed=true)``.
                tool_filter.discard("loop_complete")
            else:
                # Fixed-cron children have no use for ``loop_wait`` — the
                # cron expression controls cadence — but they need a way
                # to self-terminate when their prompt's stop condition is
                # met; ``loop_complete`` is the canonical control surface.
                tool_filter.discard("loop_wait")
                if "loop_complete" in self._tools.tool_names:
                    tool_filter.add("loop_complete")
            tool_filter = self._apply_mcp_schema_filter(
                tool_filter, explicit_allowed=explicit_allowed,
            )
            return self._ensure_always_available_tools(
                tool_filter, explicit_allowed=explicit_allowed,
            )

        if tool_filter is not None and not explicit_allowed:
            tool_filter = set(tool_filter)
            tool_filter.discard("loop_wait")
            tool_filter.discard("loop_complete")
        tool_filter = self._apply_mcp_schema_filter(
            tool_filter, explicit_allowed=explicit_allowed,
        )
        return self._ensure_always_available_tools(
            tool_filter, explicit_allowed=explicit_allowed,
        )

    @staticmethod
    def _dynamic_loop_wait_succeeded(
        session: Session,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> bool:
        """Return true when a dynamic loop child successfully scheduled its next run."""
        if not (session.config or {}).get("scheduled_dynamic_loop"):
            return False

        loop_wait_ids = {
            str(tool_call.get("id") or "")
            for tool_call in tool_calls
            if tool_call.get("function", {}).get("name") == "loop_wait"
        }
        if not loop_wait_ids:
            return False

        for result in tool_results:
            if str(result.get("tool_call_id") or "") not in loop_wait_ids:
                continue
            content = result.get("content")
            if not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("success") is True:
                return True

        return False

    # ------------------------------------------------------------------
    # Budget pressure warning (delegates to resilience module)
    # ------------------------------------------------------------------

    def _inject_budget_warning(self, tool_results: list[dict]) -> list[dict]:
        """If budget is below threshold, append a warning to the last tool result."""
        return inject_budget_warning(tool_results, self._budget)

    # ------------------------------------------------------------------
    # Context compression callback (for LLM call retry module)
    # ------------------------------------------------------------------

    def _compress_context_callback(
        self,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        lease: SessionLease,
    ) -> Callable:
        """Return an async callable that compresses context in place.

        The callback is passed to :func:`call_llm_with_retry` so it can
        trigger compression on 413 / context-length errors without coupling
        the retry module to the full harness.

        Returns the compressed message list on success, or ``None`` if
        compression could not reduce the context further.
        """
        async def _compress(api_messages: list[dict]) -> list[dict] | None:
            original_len = len(api_messages)
            if not self._compressor.should_compress(api_messages, system_prompt):
                # Force compress even if under threshold -- we're in error recovery.
                pass
            compressed, summary_data = await self._compressor.compress(
                api_messages, self._llm,
            )
            if len(compressed) >= original_len:
                return None  # Compression didn't help.
            # Emit event.
            try:
                await self._store.emit_event(
                    session.id,
                    EventType.CONTEXT_COMPACT,
                    {
                        **summary_data,
                        "compacted_messages": compressed,
                    },
                )
                self._system_prompt_cache.invalidate(session.id)
                self._memory_snapshot_cache.pop(session.id, None)
            except Exception:
                logger.debug("Failed to emit CONTEXT_COMPACT event", exc_info=True)
            return compressed
        return _compress

    # ------------------------------------------------------------------
    # Streaming control
    # ------------------------------------------------------------------

    def _set_streaming_enabled(self, enabled: bool) -> None:
        """Set the streaming flag (called by LLM call module on fallback)."""
        self._streaming_enabled = enabled

    # ------------------------------------------------------------------
    # Interrupt helper (delegates to message_utils)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_skipped_tool_result(tc: dict[str, Any]) -> dict:
        """Return a synthetic tool result for a skipped (interrupted) call."""
        return make_skipped_tool_result(tc)

    # ------------------------------------------------------------------
    # Hidden advisor routing
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Message reconstruction from event log
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Final summary on budget exhaustion
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # /compress command handler
    # ------------------------------------------------------------------

    async def _request_final_summary(
        self,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        lease: SessionLease,
        *,
        cost_tracker: SessionCostTracker | None = None,
        turn_id: str | None = None,
        iteration_index: int | None = None,
    ) -> None:
        """Request one final LLM response with no tools when the budget is exhausted.

        The model is asked to summarise its work so far without issuing
        any more tool calls.  The summary is emitted as an ``LLM_RESPONSE``
        event.  If the summary call fails, the session is completed with
        the ``budget_exhausted`` reason and no summary.
        """
        logger.info(
            "Session %s: budget exhausted, requesting final summary",
            session.id,
        )

        summary_request = (
            "You've reached the maximum number of tool-calling iterations allowed. "
            "Please provide a final response summarizing what you've found and accomplished so far, "
            "without calling any more tools."
        )
        messages.append({"role": "user", "content": summary_request})

        model_id = self._current_model or session.model or self._default_model

        try:
            api_messages: list[dict] = [
                {"role": "system", "content": system_prompt},
            ]
            # Clean internal-only fields before sending to API
            # (same treatment as the main loop).
            for msg in messages:
                api_msg = msg.copy()
                if msg.get("role") == "assistant":
                    reasoning_text = msg.get("reasoning")
                    if reasoning_text:
                        api_msg["reasoning_content"] = reasoning_text
                api_msg.pop("reasoning", None)
                api_msg.pop("finish_reason", None)
                api_msg.pop("_thinking_prefill", None)
                api_messages.append(api_msg)
            await _prepare_messages_for_model_vision_support(
                api_messages,
                model_id=model_id,
                llm_client=self._llm,
                vision_client=self._vision_client,
                vision_model_override=self._vision_model,
            )

            api_messages = apply_developer_role(api_messages, model_id)

            create_kwargs: dict[str, Any] = {
                "model": model_id,
                "messages": api_messages,
                "temperature": session.config.get("temperature", 0.7),
                "max_tokens": session.config.get("max_tokens", 16384),
                # No tools -- force a text-only response.
            }

            assistant_message, usage_data = await call_llm_with_retry(
                session=session,
                create_kwargs=create_kwargs,
                iteration=self._budget.used + 1,
                turn_id=turn_id,
                iteration_index=iteration_index,
                llm_client=self._llm,
                store=self._store,
                streaming_enabled=self._streaming_enabled,
                interrupt_check=self._check_interrupt,
                rotate_credential=self._try_rotate_credential,
                activate_fallback=self._try_activate_fallback,
                get_current_model=lambda: self._current_model,
                set_streaming_enabled=self._set_streaming_enabled,
                compress_context=self._compress_context_callback(
                    session, messages, system_prompt, lease,
                ),
                context_compressor=self._compressor,
                rate_limit_guard=self._provider_rate_limit_guard(),
            )

            # Strip thinking blocks from the summary.
            reasoning_text = extract_reasoning(assistant_message)
            if reasoning_text:
                strip_think_blocks(assistant_message)

            # Coerce content type.
            coerce_message_content(assistant_message)

            # Emit the summary as an LLM_RESPONSE event.
            input_tokens = usage_data.get("input_tokens", 0)
            output_tokens = usage_data.get("output_tokens", 0)

            from surogates.harness.model_metadata import estimate_cost

            cost = estimate_cost(model_id, input_tokens, output_tokens)

            if cost_tracker is not None:
                cost_tracker.record_call(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                )

            final_payload: dict[str, Any] = {
                "message": assistant_message,
                "model": usage_data.get("model", model_id),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "finish_reason": "budget_exhausted",
            }
            if turn_id is not None:
                final_payload["turn_id"] = turn_id
                final_payload["iteration_index"] = (
                    max(int(iteration_index), 0)
                    if iteration_index is not None
                    else max(self._budget.used - 1, 0)
                )
            await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                final_payload,
            )

            messages.append(assistant_message)

        except Exception as exc:
            logger.warning(
                "Session %s: final summary request failed: %s",
                session.id,
                exc,
            )

        await self._complete_session(
            session, messages, lease, reason="budget_exhausted",
            cost_tracker=cost_tracker,
            turn_id=turn_id,
            user_message=_latest_user_message_text(messages),
        )

    async def _handle_clear_command(
        self,
        session: Session,
        lease: SessionLease,
    ) -> None:
        """Handle the /clear slash command.

        Emits a CONTEXT_COMPACT event with an empty message list, effectively
        clearing all conversation history.  The next wake() will rebuild from
        the compacted (empty) state.
        """
        # Destroy the sandbox if one exists.
        if self._sandbox_pool is not None:
            try:
                await self._sandbox_pool.destroy_for_session(str(session.id))
            except Exception:
                logger.debug("Sandbox cleanup on /clear failed", exc_info=True)

        # Emit a CONTEXT_COMPACT event with empty messages — this replaces
        # the entire conversation history on next replay.
        await self._store.emit_event(
            session.id,
            EventType.CONTEXT_COMPACT,
            {
                "compacted_messages": [],
                "strategy": "clear",
                "original_message_count": 0,
                "compressed_message_count": 0,
            },
        )

        # Emit an assistant message confirming the clear.
        await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {
                "message": {
                    "role": "assistant",
                    "content": "Conversation cleared.",
                },
                "input_tokens": 0,
                "output_tokens": 0,
                "context_window": self._compressor.context_length,
            },
        )
        # Lease released by the outer wake() finally block.


    async def _handle_loop_command(
        self,
        session: Session,
        content: str,
        lease: SessionLease,
    ) -> None:
        from surogates.scheduled.prompt_guard import (
            ScheduledPromptBlocked,
            validate_scheduled_prompt,
        )
        from surogates.scheduled.schedule import (
            DYNAMIC_LOOP_EXPIRY_DAYS,
            DEFAULT_LOOP_EXPIRY_DAYS,
            parse_dynamic_loop_schedule,
            parse_loop_command,
            parse_schedule,
        )
        from surogates.scheduled.store import ScheduledSessionStore

        principal_user_id = self._tenant.user_id
        principal_sa_id = self._tenant.service_account_id
        if principal_user_id is None and principal_sa_id is None:
            # Anonymous-channel sessions have neither a user nor a
            # service-account principal — there is no durable owner that
            # outlives a recurring loop.  Reject explicitly with a
            # message that matches the /mission gate's phrasing.
            message = (
                "/loop requires a user or service-account session — "
                "anonymous channel sessions cannot own schedules."
            )
            await self._emit_loop_response(
                session, lease, message, user_content=content,
            )
            return

        raw = content[len("/loop"):].strip()
        store = ScheduledSessionStore(self._session_factory)
        if not raw or raw == "help":
            message = "Usage: /loop [interval] <prompt>. Example: /loop 5m /babysit-prs"
        elif raw == "list":
            rows = await store.list_for_user(
                org_id=self._tenant.org_id,
                user_id=principal_user_id,
                service_account_id=principal_sa_id,
                agent_id=session.agent_id,
            )
            message = _format_loop_list(rows)
        elif raw.startswith("cancel "):
            schedule_id_raw = raw.split(None, 1)[1].strip()
            try:
                schedule_id = UUID(schedule_id_raw)
            except ValueError:
                message = f"Loop {schedule_id_raw} was not found."
            else:
                deleted = await store.delete_for_user(
                    schedule_id,
                    org_id=self._tenant.org_id,
                    user_id=principal_user_id,
                    service_account_id=principal_sa_id,
                    agent_id=session.agent_id,
                )
                message = (
                    f"Loop {schedule_id} cancelled."
                    if deleted
                    else f"Loop {schedule_id} was not found."
                )
        else:
            try:
                parsed = parse_loop_command(raw)
                validate_scheduled_prompt(parsed.prompt, source="loop")
                if parsed.interval is None:
                    schedule = parse_dynamic_loop_schedule(timezone_name="UTC")
                    created = await store.create_dynamic_loop(
                        org_id=self._tenant.org_id,
                        user_id=principal_user_id,
                        service_account_id=principal_sa_id,
                        agent_id=session.agent_id,
                        prompt=parsed.prompt,
                        schedule=schedule,
                        created_from_session_id=session.id,
                    )
                    message = (
                        f"Loop scheduled: `{created.id}`\n\n"
                        f"- Prompt: {parsed.prompt}\n"
                        f"- Cadence: dynamic, chosen after each run with `loop_wait`\n"
                        f"- Next run: {created.next_run_at}\n"
                        f"- Auto-expires: {DYNAMIC_LOOP_EXPIRY_DAYS} days\n"
                        f"- Cancel: `/loop cancel {created.id}`"
                    )
                else:
                    schedule = parse_schedule(parsed.interval, timezone_name="UTC")
                    created = await store.create_loop(
                        org_id=self._tenant.org_id,
                        user_id=principal_user_id,
                        service_account_id=principal_sa_id,
                        agent_id=session.agent_id,
                        prompt=parsed.prompt,
                        schedule=schedule,
                        created_from_session_id=session.id,
                    )
                    cadence_line = f"- Cadence: {created.schedule_display}\n"
                    if schedule.adjusted_from:
                        cadence_line += (
                            f"- Requested cadence: {schedule.adjusted_from}; "
                            f"using {created.schedule_display}\n"
                        )
                    message = (
                        f"Loop scheduled: `{created.id}`\n\n"
                        f"- Prompt: {parsed.prompt}\n"
                        f"{cadence_line}"
                        f"- Next run: {created.next_run_at}\n"
                        f"- Auto-expires: {DEFAULT_LOOP_EXPIRY_DAYS} days\n"
                        f"- Cancel: `/loop cancel {created.id}`"
                    )
            except (ValueError, ScheduledPromptBlocked) as exc:
                message = str(exc)

        await self._emit_loop_response(
            session, lease, message, user_content=content,
        )

    async def _emit_loop_response(
        self,
        session: Session,
        lease: SessionLease,
        message: str,
        *,
        user_content: str | None = None,
    ) -> None:
        assistant_message = {"role": "assistant", "content": message}
        event_id = await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": assistant_message},
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=event_id,
            lease_token=lease.lease_token,
        )

    async def _handle_compress_command(
        self,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        lease: SessionLease,
    ) -> None:
        """Handle the /compress slash command.

        Forces context compression regardless of threshold, emits the
        result as an assistant message so the user sees what happened.
        """
        original_count = len(messages)

        # Remove the /compress message itself — it's not real conversation.
        # Dispatch only reaches here once the latest user *event* text is
        # exactly "/compress", so the command is the last user message.
        # Drop it by identity rather than by content match: rebuilt content
        # may carry attachment / view-context note prefixes (so a string
        # compare would miss it) and may be a multimodal *list* of blocks
        # (so ``.strip()`` would raise ``AttributeError``).
        last_user = next(
            (m for m in reversed(messages) if m.get("role") == "user"),
            None,
        )
        messages = [m for m in messages if m is not last_user]

        if len(messages) <= 5:
            # Too few messages to compress.
            await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                {
                    "message": {
                        "role": "assistant",
                        "content": "Context is too small to compress — only "
                                   f"{len(messages)} messages.",
                    },
                },
            )
            # Lease released by the outer wake() finally block.
            return

        try:
            compressed, summary_data = await self._compressor.compress(
                messages, self._llm,
            )
        except Exception as exc:
            logger.error("Compress command failed: %s", exc, exc_info=True)
            await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                {
                    "message": {
                        "role": "assistant",
                        "content": f"Compression failed: {exc}",
                    },
                },
            )
            # Lease released by the outer wake() finally block.
            return

        compressed_count = len(compressed)
        saved = original_count - compressed_count

        # Emit the compacted messages as a CONTEXT_COMPACT event.
        await self._store.emit_event(
            session.id,
            EventType.CONTEXT_COMPACT,
            {
                **summary_data,
                "compacted_messages": compressed,
            },
        )

        # Emit an assistant message summarising the result.
        await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {
                "message": {
                    "role": "assistant",
                    "content": (
                        f"Context compressed: {original_count} → {compressed_count} messages "
                        f"({saved} removed). "
                        f"Strategy: {summary_data.get('strategy', 'unknown')}."
                    ),
                },
                "input_tokens": 0,
                "output_tokens": 0,
                "context_window": self._compressor.context_length,
            },
        )
        # Lease released by the outer wake() finally block.

    # ------------------------------------------------------------------
    # Fenced-artifact promotion
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Session completion
    # ------------------------------------------------------------------


    def _maybe_generate_title(
        self,
        *,
        session: Session,
        messages: list[dict],
        model: str,
    ) -> None:
        """Schedule auto-title generation as a fire-and-forget background task.

        Title generation issues its own LLM call which can take several seconds.
        Running it inline would block the chat turn (delaying the
        SESSION_COMPLETE event the UI uses to clear the busy indicator).
        It is triggered as soon as the harness sees the user's first message,
        so the title can land in parallel with the main LLM response.

        The task writes the title atomically via
        ``update_session_title_if_empty`` and emits
        :data:`EventType.SESSION_TITLE_UPDATED` on success so the per-session
        SSE stream surfaces the new title without waiting for a manual session
        list refresh.  Messages are snapshotted so the chat thread can keep
        mutating the live list without racing the background reader.
        """
        if (session.title or "").strip():
            return
        task = asyncio.create_task(
            self._run_title_generation(
                session=session,
                messages=list(messages),
                model=model,
            ),
            name=f"title-gen-{session.id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_title_generation(
        self,
        *,
        session: Session,
        messages: list[dict],
        model: str,
    ) -> None:
        """Body of the background title-generation task.

        ``maybe_generate_session_title`` returns the title only when the DB
        write actually replaced an empty value, so emitting the event from this
        branch avoids double-emission when two workers race the same session.
        """
        try:
            title = await maybe_generate_session_title(
                store=self._store,
                llm_client=self._llm,
                session=session,
                messages=messages,
                model=model,
                summary_client=self._summary_client,
                summary_model=self._summary_model,
            )
            if title:
                await self._store.emit_event(
                    session.id,
                    EventType.SESSION_TITLE_UPDATED,
                    {"title": title},
                )
                logger.debug("Auto-generated title for session %s: %s", session.id, title)
        except Exception:
            logger.warning(
                "Auto-title generation failed for session %s",
                session.id,
                exc_info=True,
            )

