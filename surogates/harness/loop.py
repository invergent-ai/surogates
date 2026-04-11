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

import logging
import os
import traceback
from typing import TYPE_CHECKING, Any, Callable
from uuid import UUID

from surogates.harness.connection_health import cleanup_dead_connections
from surogates.harness.cost_tracker import SessionCostTracker
from surogates.harness.credentials import CredentialPool
from surogates.harness.llm_call import apply_developer_role, call_llm_with_retry
from surogates.harness.message_utils import coerce_message_content, make_skipped_tool_result
from surogates.harness.prompt_cache import SystemPromptCache
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
    strip_budget_warnings,
)
from surogates.harness.subdirectory_hints import SubdirectoryHintTracker
from surogates.harness.tool_exec import execute_tool_calls
from surogates.session.events import EventType

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from redis.asyncio import Redis

    from surogates.harness.budget import IterationBudget
    from surogates.harness.context import ContextCompressor
    from surogates.harness.prompt import PromptBuilder
    from surogates.memory.manager import MemoryManager
    from surogates.sandbox.pool import SandboxPool
    from surogates.session.models import Event, Session, SessionLease
    from surogates.session.store import SessionStore
    from surogates.tools.registry import ToolRegistry
    from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Lease is renewed after this many LLM iterations to prevent expiry during
# long-running loops.
_LEASE_RENEWAL_INTERVAL: int = 3

# Default TTL (seconds) for lease acquisition and renewal.
_LEASE_TTL_SECONDS: int = 60

# Retry / resilience constants
_MAX_LENGTH_CONTINUATIONS: int = 3
_MAX_CONSECUTIVE_INVALID_TOOL_CALLS: int = 3
_LENGTH_CONTINUATION_PROMPT: str = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off. Do not "
    "restart or repeat prior text. Finish the answer directly.]"
)

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _is_valid_json_args(tc: dict) -> bool:
    """Check if a tool call's arguments are valid JSON."""
    import json as _json

    fn = tc.get("function", {})
    args_raw = fn.get("arguments", "")
    if not args_raw or not isinstance(args_raw, str):
        return True  # empty or already parsed — not invalid JSON
    args_raw = args_raw.strip()
    if not args_raw or args_raw == "{}":
        return True
    try:
        parsed = _json.loads(args_raw)
        return isinstance(parsed, dict)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class AgentHarness:
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
        checkpoints_enabled: bool = False,
        api_client: Any | None = None,
    ) -> None:
        self._store = session_store
        self._tools = tool_registry
        self._llm = llm_client
        self._tenant = tenant
        self._worker_id = worker_id
        self._budget = budget
        self._compressor = context_compressor
        self._prompt = prompt_builder
        self._redis: Redis | None = redis_client
        self._sandbox_pool: SandboxPool | None = sandbox_pool
        self._api_client = api_client

        # Checkpoint flag — when enabled, the harness tells the sandbox
        # to take filesystem snapshots before file-mutating operations.
        # The actual checkpoint logic runs inside the sandbox (not here).
        self._checkpoints_enabled = checkpoints_enabled

        # System prompt cache (shared across wake() calls for the same worker).
        self._system_prompt_cache: SystemPromptCache = (
            system_prompt_cache if system_prompt_cache is not None else SystemPromptCache()
        )

        # Streaming can be disabled via session config or env var.
        self._streaming_enabled: bool = True

        # Interrupt support -- thread-safe because only a single bool/str
        # is mutated and Python's GIL makes these assignments atomic.
        self._interrupt_requested: bool = False
        self._interrupt_message: str | None = None

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
        signal immediately.
        """
        self._interrupt_requested = True
        self._interrupt_message = message
        from surogates.tools.utils.interrupt import set_interrupt
        set_interrupt(True)

    def _check_interrupt(self) -> bool:
        """Return ``True`` if an interrupt has been requested."""
        return self._interrupt_requested

    def _clear_interrupt(self) -> None:
        """Reset the interrupt flag (called after handling)."""
        self._interrupt_requested = False
        self._interrupt_message = None
        from surogates.tools.utils.interrupt import set_interrupt
        set_interrupt(False)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def wake(self, session_id: UUID) -> None:
        """Entry point.  Acquire lease, replay events, run loop, release lease."""

        # Honour streaming preference from env.
        env_streaming = os.environ.get("SUROGATES_STREAMING_ENABLED", "").lower()
        if env_streaming in ("0", "false", "no"):
            self._streaming_enabled = False

        # Connection health: proactively clean up dead connections before
        # making any LLM requests.
        try:
            await cleanup_dead_connections(self._llm)
        except Exception:
            logger.debug("Connection health cleanup failed", exc_info=True)

        # 1. Fetch session metadata.
        session = await self._store.get_session(session_id)

        # Honour per-session streaming config.
        if not session.config.get("streaming", True):
            self._streaming_enabled = False

        # 2. Acquire exclusive lease -- return silently if another worker holds it.
        lease = await self._store.try_acquire_lease(
            session_id, self._worker_id, ttl_seconds=_LEASE_TTL_SECONDS,
        )
        if lease is None:
            logger.debug(
                "Session %s: lease held by another worker, skipping",
                session_id,
            )
            return

        try:
            # 3. Retrieve the harness cursor and the full event history.
            cursor = await self._store.get_harness_cursor(session_id)
            all_events = await self._store.get_events(session_id)

            # 4. Check for pending events (events after the cursor).
            pending = [e for e in all_events if e.id is not None and e.id > cursor]
            if not pending:
                logger.debug(
                    "Session %s: no pending events after cursor %d",
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

            # 7. Compress context if needed.
            messages = await self._engineer_context(
                session, all_events, messages,
            )

            # 8. Build the system prompt (with caching).
            system_prompt = await self._build_system_prompt(session)

            # 9. Create per-session cost tracker.
            cost_tracker = SessionCostTracker()

            # 10. Run the core LLM loop.
            await self._run_loop(session, messages, system_prompt, lease, cost_tracker=cost_tracker)

        except Exception:
            logger.exception("Harness crash for session %s", session_id)
            try:
                await self._store.emit_event(
                    session_id,
                    EventType.HARNESS_CRASH,
                    {
                        "worker_id": self._worker_id,
                        "error": traceback.format_exc()[-2000:],
                    },
                )
            except Exception:
                logger.exception(
                    "Failed to emit HARNESS_CRASH event for session %s",
                    session_id,
                )
            raise
        finally:
            # 10. Always release the lease.
            try:
                await self._store.release_lease(session_id, lease.lease_token)
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
        iteration = 0
        length_continuation_count = 0
        length_continuation_prefix = ""  # accumulated partial response across length retries
        consecutive_invalid_tool_calls = 0
        invalid_json_retries = 0  # API-level retries for malformed tool args
        thinking_prefill_retries = 0  # retries for thinking-only responses
        incomplete_scratchpad_retries = 0  # retries for unclosed REASONING_SCRATCHPAD
        content_with_tools_cache = ContentWithToolsCache()

        # Subdirectory hint tracker -- discovers context files as the agent navigates.
        hint_tracker = SubdirectoryHintTracker(
            initial_cwd=session.config.get("workspace_path"),
        )

        # --- Prefilled context injection ---
        # Ephemeral messages injected between system prompt and conversation
        # for few-shot examples or planning context. API-call-time only.
        prefill_messages: list[dict] = session.config.get("prefill_messages") or []

        # --- Memory prefetch (one-shot before loop) ---
        memory_context = await self._prefetch_memory()

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

            # --- Interrupt check at the top of each iteration ---
            if self._check_interrupt():
                reason_msg = self._interrupt_message or "interrupted"
                # Destroy the sandbox pod on interrupt.
                if self._sandbox_pool is not None:
                    try:
                        await self._sandbox_pool.destroy_for_session(str(session.id))
                    except Exception:
                        logger.debug("Sandbox cleanup on interrupt failed", exc_info=True)
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
                return

            # --- Checkpoint: reset per-turn dedup in sandbox ---
            if self._checkpoints_enabled and self._sandbox_pool:
                try:
                    await self._sandbox_pool.execute(
                        str(session.id), "_checkpoint",
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
                )
                return

            # 1. Emit LLM_REQUEST event.
            model_id = self._current_model or session.model or "gpt-4o"
            await self._store.emit_event(
                session.id,
                EventType.LLM_REQUEST,
                {"model": model_id, "iteration": iteration},
            )

            # 2. Call the LLM with retry (streaming or non-streaming).
            tool_schemas = self._tools.get_schemas()

            # Build the message list: system → prefill → memory → conversation.
            # Each message is cleaned for API compatibility: internal-only fields are stripped, reasoning
            # is passed back as ``reasoning_content`` for providers that need
            # it (Moonshot AI, Novita, OpenRouter).
            api_messages: list[dict] = [
                {"role": "system", "content": system_prompt},
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

            # Developer role swap for models that prefer it (e.g. GPT-5, Codex).
            api_messages = apply_developer_role(api_messages, model_id)

            create_kwargs: dict[str, Any] = {
                "model": model_id,
                "messages": api_messages,
                "temperature": session.config.get("temperature", 0.7),
            }
            if tool_schemas:
                create_kwargs["tools"] = tool_schemas

            try:
                assistant_message, usage_data = await call_llm_with_retry(
                    session=session,
                    create_kwargs=create_kwargs,
                    iteration=iteration,
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
                )
            except Exception as exc:
                logger.exception(
                    "LLM call failed for session %s (iteration %d)",
                    session.id,
                    iteration,
                )
                await self._store.emit_event(
                    session.id,
                    EventType.HARNESS_CRASH,
                    {
                        "worker_id": self._worker_id,
                        "error": f"LLM call failed: {exc}",
                        "iteration": iteration,
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
                    {"reasoning": reasoning_text},
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

            # 4. Emit LLM_RESPONSE event with usage data.
            input_tokens = usage_data.get("input_tokens", 0)
            output_tokens = usage_data.get("output_tokens", 0)
            finish_reason = usage_data.get("finish_reason", "stop")

            response_data: dict[str, Any] = {
                "message": assistant_message,
                "model": usage_data.get("model", model_id),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "finish_reason": finish_reason,
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
                    cache_read_tokens=usage_data.get("cache_read_tokens", 0),
                    reasoning_tokens=usage_data.get("reasoning_tokens", 0),
                )

            event_id = await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                response_data,
            )

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
                continue  # re-enter the loop

            # If we had accumulated a prefix, prepend it to the final content.
            if length_continuation_prefix:
                final_content = assistant_message.get("content", "") or ""
                assistant_message["content"] = length_continuation_prefix + final_content
                length_continuation_prefix = ""

            # Reset length continuation counter on a normal finish.
            length_continuation_count = 0

            # 5. If no tool calls -> session turn is complete.
            tool_calls_raw = assistant_message.get("tool_calls")
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

                        # Exhausted prefill attempts or no structured
                        # reasoning -- mark response as "(empty)".
                        assistant_message["content"] = "(empty)"
                        logger.info(
                            "Session %s: empty final response after %d "
                            "thinking prefill retries",
                            session.id, thinking_prefill_retries,
                        )

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
                await self._complete_session(
                    session, messages, lease, reason="completed",
                    through_event_id=event_id,
                    cost_tracker=cost_tracker,
                )
                return

            # 5a. Invalid JSON retry — if ALL tool calls have unparseable JSON,
            # retry the API call instead of sending error results.
            # The model often fixes its JSON on a second attempt.
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

            # 5c. Invalid tool call recovery -- check for unknown tools
            # or malformed JSON before executing (with fuzzy name repair).
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

            # 7. Execute tool calls (sequential or parallel) with truncation.
            # Checkpoint before file-mutating tools (write_file, patch).
            # The checkpoint hash is stashed on the tool call dict so
            # execute_single_tool can include it in the TOOL_CALL event,
            # enabling the web UI to offer per-tool-call rollback.
            if self._checkpoints_enabled and self._sandbox_pool:
                for tc in tool_calls_raw:
                    fn = tc.get("function", {})
                    tool_name = fn.get("name", "")
                    if tool_name in ("write_file", "patch"):
                        try:
                            import json as _json
                            args = _json.loads(fn.get("arguments", "{}"))
                            file_path = args.get("path", "")
                            if file_path:
                                cp_input = _json.dumps({
                                    "action": "take",
                                    "reason": f"before {tool_name}",
                                    "file_path": file_path,
                                })
                                cp_result = await self._sandbox_pool.execute(
                                    str(session.id), "_checkpoint", cp_input,
                                )
                                cp_data = _json.loads(cp_result)
                                cp_hash = cp_data.get("hash")
                                if cp_hash:
                                    tc["_checkpoint_hash"] = cp_hash
                        except Exception:
                            logger.debug("Checkpoint before %s failed", tool_name, exc_info=True)

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
                api_client=self._api_client,
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
                except Exception:
                    logger.debug("Memory manager sync_all failed", exc_info=True)

            # 9. Check if compression is needed.
            if self._compressor.should_compress(messages, system_prompt):
                # Memory manager: extract insights before compression.
                if self._memory_manager is not None:
                    try:
                        pre_compress_text = self._memory_manager.on_pre_compress(messages)
                        if pre_compress_text:
                            logger.debug(
                                "Session %s: memory pre-compress extracted %d chars",
                                session.id, len(pre_compress_text),
                            )
                    except Exception:
                        logger.debug("Memory manager on_pre_compress failed", exc_info=True)

                compressed, summary_data = await self._compressor.compress(
                    messages, self._llm,
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

            # 10. Renew lease periodically.
            if iteration % _LEASE_RENEWAL_INTERVAL == 0:
                try:
                    await self._store.renew_lease(
                        session.id, lease.lease_token, ttl_seconds=_LEASE_TTL_SECONDS,
                    )
                except Exception:
                    logger.warning(
                        "Failed to renew lease for session %s", session.id,
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
            session, messages, system_prompt, lease, cost_tracker=cost_tracker,
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

    # ------------------------------------------------------------------
    # Invalid tool call detection (delegates to resilience module)
    # ------------------------------------------------------------------

    def _find_invalid_tool_calls(
        self, tool_calls: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], str]]:
        """Return list of (tool_call, error_message) for invalid calls."""
        return find_invalid_tool_calls(tool_calls, self._tools)

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
    # Message reconstruction from event log
    # ------------------------------------------------------------------

    async def _prefetch_memory(self) -> str | None:
        """Prefetch user memory once before the loop.

        If a MemoryManager is available, delegates to it and wraps the
        result in a ``<memory-context>`` fence.  Otherwise falls back to
        direct file I/O.
        """
        # Use memory manager if available.
        if self._memory_manager is not None:
            try:
                raw = self._memory_manager.prefetch_all("")
                if raw and raw.strip():
                    from surogates.memory.manager import build_memory_context_block
                    return build_memory_context_block(raw)
            except Exception:
                logger.debug("Memory manager prefetch failed", exc_info=True)
            return None

        # Fall back to direct file read.
        try:
            memory_dir = self._tenant.asset_root
            if not memory_dir:
                return None
            from pathlib import Path

            # Try user-scoped memory first, fall back to org shared
            for subdir in (
                f"users/{self._tenant.user_id}/memories",
                "shared/memories",
            ):
                memory_path = Path(memory_dir) / subdir / "MEMORY.md"
                if memory_path.is_file():
                    content = memory_path.read_text(encoding="utf-8").strip()
                    if content:
                        logger.debug("Prefetched memory from %s (%d chars)", memory_path, len(content))
                        return content
        except Exception:
            logger.debug("Memory prefetch failed", exc_info=True)
        return None

    def _rebuild_messages(self, events: list[Event]) -> list[dict]:
        """Replay event log to reconstruct conversation messages.

        Processes events in order.  A ``CONTEXT_COMPACT`` event replaces
        all previously accumulated messages with the compacted set stored
        in its data payload.

        ``LLM_THINKING`` events are **skipped** during replay -- they are
        informational only and should not re-enter the conversation.

        ``LLM_DELTA`` events are likewise skipped; the full response is
        captured in the subsequent ``LLM_RESPONSE`` event.
        """
        messages: list[dict] = []

        for event in events:
            etype = event.type

            if etype == EventType.USER_MESSAGE.value:
                content = event.data.get("content", "")
                messages.append({"role": "user", "content": content})

            elif etype == EventType.LLM_RESPONSE.value:
                stored_message = event.data.get("message")
                if stored_message is not None:
                    messages.append(stored_message)

            elif etype == EventType.TOOL_RESULT.value:
                messages.append({
                    "role": "tool",
                    "tool_call_id": event.data.get("tool_call_id", ""),
                    "content": event.data.get("content", ""),
                })

            elif etype == EventType.CONTEXT_COMPACT.value:
                compacted = event.data.get("compacted_messages")
                if compacted is not None:
                    messages = list(compacted)

            # LLM_THINKING and LLM_DELTA are intentionally skipped.

        # Strip stale budget warnings from replayed tool results.
        strip_budget_warnings(messages)

        return messages

    # ------------------------------------------------------------------
    # Context engineering
    # ------------------------------------------------------------------

    async def _engineer_context(
        self,
        session: Session,
        events: list[Event],
        messages: list[dict],
    ) -> list[dict]:
        """Apply context compression if needed."""
        system_prompt = self._prompt.build()
        if not self._compressor.should_compress(messages, system_prompt):
            return messages

        compressed, summary_data = await self._compressor.compress(
            messages, self._llm,
        )

        await self._store.emit_event(
            session.id,
            EventType.CONTEXT_COMPACT,
            {
                **summary_data,
                "compacted_messages": compressed,
            },
        )

        # Invalidate system prompt cache -- conversation shape changed.
        self._system_prompt_cache.invalidate(session.id)

        return compressed

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    async def _build_system_prompt(self, session: Session) -> str:
        """Delegate to PromptBuilder, with per-session caching."""
        cached = self._system_prompt_cache.get(session.id)
        if cached is not None:
            return cached

        prompt = self._prompt.build()
        self._system_prompt_cache.set(session.id, prompt)
        return prompt

    # ------------------------------------------------------------------
    # Final summary on budget exhaustion
    # ------------------------------------------------------------------

    async def _request_final_summary(
        self,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        lease: SessionLease,
        *,
        cost_tracker: SessionCostTracker | None = None,
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

        model_id = self._current_model or session.model or "gpt-4o"

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
            api_messages = apply_developer_role(api_messages, model_id)

            create_kwargs: dict[str, Any] = {
                "model": model_id,
                "messages": api_messages,
                "temperature": session.config.get("temperature", 0.7),
                # No tools -- force a text-only response.
            }

            assistant_message, usage_data = await call_llm_with_retry(
                session=session,
                create_kwargs=create_kwargs,
                iteration=self._budget.used + 1,
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

            event_id = await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                {
                    "message": assistant_message,
                    "model": usage_data.get("model", model_id),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "finish_reason": "budget_exhausted",
                },
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
        )

    # ------------------------------------------------------------------
    # Session completion
    # ------------------------------------------------------------------

    async def _complete_session(
        self,
        session: Session,
        messages: list[dict],
        lease: SessionLease,
        *,
        reason: str,
        through_event_id: int | None = None,
        cost_tracker: SessionCostTracker | None = None,
    ) -> None:
        """Emit SESSION_COMPLETE and advance the cursor."""
        # Destroy the sandbox pod for this session.
        if self._sandbox_pool is not None:
            try:
                await self._sandbox_pool.destroy_for_session(str(session.id))
            except Exception:
                logger.debug("Sandbox cleanup failed for %s", session.id, exc_info=True)

        # Notify memory manager of session end.
        if self._memory_manager is not None:
            try:
                self._memory_manager.on_session_end(messages=[])
            except Exception:
                logger.debug("Memory manager on_session_end failed", exc_info=True)

        complete_data: dict[str, Any] = {
            "reason": reason,
            "worker_id": self._worker_id,
        }
        if cost_tracker is not None:
            complete_data["cost_summary"] = cost_tracker.summary()

        event_id = await self._store.emit_event(
            session.id,
            EventType.SESSION_COMPLETE,
            complete_data,
        )

        # Advance cursor to the latest event.
        cursor_target = through_event_id if through_event_id is not None else event_id
        try:
            await self._store.advance_harness_cursor(
                session.id, cursor_target, lease.lease_token,
            )
        except Exception:
            logger.warning(
                "Failed to advance cursor after session completion for %s",
                session.id,
            )

