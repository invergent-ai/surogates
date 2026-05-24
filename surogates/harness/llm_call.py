"""LLM calling with retry, streaming, and backoff for the agent harness.

Provides standalone async functions for calling LLMs with:
- Jittered exponential backoff on transient errors
- Rate-limit (429) handling with credential rotation and provider fallback
- Streaming with delta event emission and automatic non-streaming fallback
- Response shape validation
- Helper functions for HTTP status extraction and transient error detection
- Mid-stream interrupt support (cancels HTTP stream on interrupt)
- Provider API mode detection (chat_completions vs anthropic_messages)
- Anthropic prompt caching (extra_body injection for cacheable models)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from surogates.harness.message_utils import (
    message_to_dict,
    reconstruct_message_from_deltas,
)
from surogates.harness.error_classifier import FailoverReason, classify_api_error
from surogates.harness.expert_routing import model_supports_thinking_toggle
from surogates.harness.image_shrink import shrink_image_parts_in_messages
from surogates.harness.model_metadata import (
    get_next_probe_tier,
    parse_context_limit_from_error,
)
from surogates.harness.prompt_cache import build_cache_extra_body, is_cacheable_model
from surogates.harness.provider import APIMode, detect_api_mode
from surogates.harness.resilience import extract_api_error_context, summarize_api_error
from surogates.harness.retry import jittered_backoff
from surogates.harness.sanitize import (
    sanitize_messages,
    sanitize_response_message,
    sanitize_tool_pairs,
)
from surogates.harness.stream_scrubbers import (
    StreamingContextScrubber,
    StreamingThinkScrubber,
)
from surogates.harness.tool_exec import tool_call_arguments_look_incomplete
from surogates.session.events import EventType

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from surogates.session.models import Session
    from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)


def _stamp_turn_meta(
    payload: dict[str, Any],
    *,
    iteration: int,
    turn_id: str | None,
) -> dict[str, Any]:
    """Add ``turn_id`` and ``iteration_index`` to an LLM event payload.

    No-op when ``turn_id`` is ``None`` (callers that haven't opted into
    the Simple chat-mode summary plumbing). Returns the same payload
    dict to allow inline use at emit sites.
    """
    if turn_id is not None:
        payload["turn_id"] = turn_id
        # iteration is 1-based in this module; the SDK consumes a
        # 0-based ``iteration_index`` keyed off the message order, so
        # clamp at zero defensively.
        payload["iteration_index"] = max(int(iteration) - 1, 0)
    return payload


# Retry constants
MAX_LLM_RETRIES: int = 3

# Stale stream detection timeout (seconds).  If no real streaming chunk
# arrives within this window the stream is considered stale and will be
# cancelled.  Configurable via ``SUROGATES_STREAM_STALE_TIMEOUT`` env var.
STREAM_STALE_TIMEOUT_EXPLICIT: bool = "SUROGATES_STREAM_STALE_TIMEOUT" in os.environ
STREAM_STALE_TIMEOUT: float = float(
    os.environ.get("SUROGATES_STREAM_STALE_TIMEOUT", "180.0")
)

# Reasoning-capable models (GLM-5, Qwen3, QwQ) often go silent on the
# wire for several minutes during their reasoning phase -- the upstream
# only emits chunks once thinking resolves.  Bump the watchdog ceiling
# for these models so legitimate long reasoning isn't killed.  Verified
# against PROD session 5274a540: iter 8 was killed by the 180s watchdog
# while GLM-5.1 was still reasoning silently.
STREAM_STALE_TIMEOUT_REASONING: float = 600.0

# Polling interval (seconds) used to wake up between chunk reads so the
# stale-stream and interrupt checks run even when the upstream provider
# stops sending bytes entirely.  Short enough that user-initiated stops
# feel responsive; long enough that it doesn't burn CPU on a healthy
# stream.
STREAM_CHUNK_POLL_INTERVAL: float = 1.0

# Heartbeat interval (seconds).  When the stream is silent past this
# threshold but still inside the stale-timeout window, the watchdog
# emits an LLM_HEARTBEAT event so the UI can distinguish "model is
# silently reasoning" from "stream is dead".  Picked to be short
# enough that users see motion within ~15s of silence.
STREAM_HEARTBEAT_INTERVAL: float = 15.0

# Maximum reasoning_content characters allowed before any visible
# content or tool-call delta has arrived.  Above this, the stream is
# treated as a runaway-reasoning failure and cancelled so the outer
# retry layer can retry with thinking disabled.  ~4 chars/token on
# GLM-5.1, so 16 000 chars ≈ 4 000 reasoning tokens.  PROD iter-6
# dead-end was ~60 KB (well over); iter-4 legitimate was ~5 KB (well
# under).  Configurable for testing only -- no env var.
RUNAWAY_REASONING_CHAR_THRESHOLD: int = 16_000

_LOCAL_STREAM_HOSTS: frozenset[str] = frozenset({
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
})


class PartialToolCallStreamError(ConnectionError):
    """A retryable stream drop after tool names but before complete args."""

    def __init__(self, partial_tool_names: list[str], original: BaseException) -> None:
        self.partial_tool_names = partial_tool_names
        self.original = original
        names = ", ".join(partial_tool_names)
        super().__init__(f"network connection lost after partial tool call: {names}")


def compute_stream_stale_timeout(
    messages: list[dict[str, Any]] | None,
    *,
    base_url: str = "",
    model: str = "",
    explicit_timeout: float | None = None,
) -> float:
    """Return the stale-stream watchdog timeout for one request."""
    if explicit_timeout is not None:
        return float(explicit_timeout)

    if not STREAM_STALE_TIMEOUT_EXPLICIT and _is_local_base_url(base_url):
        return float("inf")

    # Reasoning-capable upstreams need a much higher ceiling -- they
    # routinely go silent for multiple minutes during the reasoning
    # phase.  The env-var explicit override (handled above) still wins.
    if not STREAM_STALE_TIMEOUT_EXPLICIT and model_supports_thinking_toggle(model):
        baseline = STREAM_STALE_TIMEOUT_REASONING
    else:
        baseline = STREAM_STALE_TIMEOUT

    approx_tokens = _estimate_message_tokens(messages or [])
    if approx_tokens > 100_000:
        return max(baseline, 300.0)
    if approx_tokens > 50_000:
        return max(baseline, 240.0)
    return baseline


def _is_local_base_url(base_url: str) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    host = parsed.hostname
    if host is None and "://" not in base_url:
        host = base_url.split("/", 1)[0].rsplit(":", 1)[0]
    return (host or "").lower() in _LOCAL_STREAM_HOSTS


def _estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        chars += len(_message_content_as_text(message.get("content", "")))
    return chars // 4


def _message_content_as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content) if content is not None else ""


def _partial_tool_names_from_accumulator(
    tool_calls_acc: dict[int, dict[str, Any]],
    *,
    treat_empty_args_as_partial: bool = False,
) -> list[str]:
    partial_tool_names: list[str] = []
    for slot in sorted(tool_calls_acc):
        entry = tool_calls_acc[slot]
        fn = entry.get("function", {})
        name = fn.get("name", "")
        arguments = fn.get("arguments", "")
        if name and (
            tool_call_arguments_look_incomplete(arguments)
            or (treat_empty_args_as_partial and not arguments)
        ):
            partial_tool_names.append(name)
    return partial_tool_names

# ---------------------------------------------------------------------------
# Developer role routing
# ---------------------------------------------------------------------------

DEVELOPER_ROLE_MODELS: frozenset[str] = frozenset({"gpt-5", "codex", "o3", "o4"})


def _should_use_developer_role(model_id: str) -> bool:
    """Return ``True`` if the model prefers 'developer' over 'system' role."""
    model_lower = model_id.lower()
    return any(m in model_lower for m in DEVELOPER_ROLE_MODELS)


def apply_developer_role(messages: list[dict], model_id: str) -> list[dict]:
    """Swap 'system' role to 'developer' for models that require it.

    Returns a new list with the role swapped on system messages.
    Does not mutate the input list.
    """
    if not _should_use_developer_role(model_id):
        return messages
    result = []
    for msg in messages:
        if msg.get("role") == "system":
            result.append({**msg, "role": "developer"})
        else:
            result.append(msg)
    return result


# ---------------------------------------------------------------------------
# Status / error helpers
# ---------------------------------------------------------------------------


def extract_status_code(exc: Exception) -> int | None:
    """Extract HTTP status code from OpenAI/httpx exceptions."""
    # Check for openai.APIStatusError.status_code
    status = getattr(exc, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass
    # Check for httpx response
    response = getattr(exc, "response", None)
    if response is not None:
        sc = getattr(response, "status_code", None)
        if sc is not None:
            try:
                return int(sc)
            except (TypeError, ValueError):
                pass
    return None


def is_transient_error(exc: Exception) -> bool:
    """Check if the error is a transient network error worth retrying.

    Handles both standard httpx transport errors and SSE error events
    from proxies (e.g. OpenRouter sends ``{"error":{"message":"Network
    connection lost."}}`` which the OpenAI SDK raises as APIError with
    no status_code).
    """
    try:
        import httpx
        transient_types: tuple[type, ...] = (
            httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout,
            httpx.ConnectError, httpx.RemoteProtocolError,
            ConnectionError, TimeoutError,
        )
    except ImportError:
        transient_types = (ConnectionError, TimeoutError)

    if isinstance(exc, transient_types):
        return True

    # SSE connection errors: APIError from SSE has no status_code, while
    # APIStatusError (4xx/5xx) always has one.
    try:
        from openai import APIError as _APIError
        if isinstance(exc, _APIError) and not getattr(exc, "status_code", None):
            err_lower = str(exc).lower()
            _SSE_CONN_PHRASES = (
                "connection lost",
                "connection reset",
                "connection closed",
                "connection terminated",
                "network error",
                "network connection",
                "terminated",
                "peer closed",
                "broken pipe",
                "upstream connect error",
            )
            if any(phrase in err_lower for phrase in _SSE_CONN_PHRASES):
                return True
    except ImportError:
        pass

    return False


def extract_retry_after(exc: Exception) -> float | None:
    """Parse Retry-After from error response headers or body."""
    # From headers
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra:
            try:
                return min(float(ra), 120.0)  # Cap at 2 minutes
            except (TypeError, ValueError):
                pass
    # From error body
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error", {})
        if isinstance(error, dict):
            ra = error.get("retry_after")
            if ra is not None:
                try:
                    return min(float(ra), 120.0)
                except (TypeError, ValueError):
                    pass
    return None


def _rate_limit_cooldown_seconds(
    exc: Exception,
    error_context: dict[str, Any] | None,
) -> float | None:
    retry_after = extract_retry_after(exc)
    if retry_after is not None:
        return retry_after
    if isinstance(error_context, dict):
        reset_at = error_context.get("reset_at")
        if isinstance(reset_at, (int, float)):
            return max(1.0, float(reset_at) - time.time())
    return None


async def interruptible_sleep(
    seconds: float,
    interrupt_flag_getter: Any,
) -> None:
    """Sleep in 0.2s increments, checking for interrupts."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if callable(interrupt_flag_getter) and interrupt_flag_getter():
            return
        if not callable(interrupt_flag_getter) and interrupt_flag_getter:
            return
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.2, remaining))


# ---------------------------------------------------------------------------
# LLM call with retry
# ---------------------------------------------------------------------------


async def call_llm_with_retry(
    *,
    session: Session,
    create_kwargs: dict[str, Any],
    iteration: int,
    llm_client: AsyncOpenAI,
    store: SessionStore,
    streaming_enabled: bool,
    interrupt_check: Callable[[], bool],
    rotate_credential: Callable[..., bool],
    activate_fallback: Callable[[], bool],
    get_current_model: Callable[[], str | None],
    set_streaming_enabled: Callable[[bool], None],
    compress_context: Callable[..., Any] | None = None,
    context_compressor: Any | None = None,
    on_tool_call_complete: Callable[[dict[str, Any]], None] | None = None,
    on_stream_retry: (
        Callable[[], Callable[[dict[str, Any]], None] | None] | None
    ) = None,
    rate_limit_guard: Any | None = None,
    turn_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the LLM with retry, backoff, rate-limit handling, and credential rotation.

    Production-hardening features:

    - Jittered exponential backoff on transient errors (429, 500, 502, 503, 529)
    - Credential rotation on 401/402/429 via *rotate_credential*
    - Fallback provider chain via *activate_fallback*
    - Context-length error detection (400/413 with context-related phrases) --
      triggers context compression via *compress_context* callback
    - Thinking-block signature recovery (Anthropic 400 with "signature" +
      "thinking") -- strips ``reasoning_details`` and retries once
    - Anthropic Sonnet long-context tier gate (429 "extra usage" + "long context")
    - Server-disconnect-as-context-overflow heuristic for large sessions
    - Generic Anthropic 400 heuristic for large sessions
    - Non-retryable client error detection (401/403/404/422) with fallback
    - SSE stream-drop detection and guidance

    Parameters
    ----------
    on_tool_call_complete:
        Optional callback fired each time a complete tool_use block is
        detected during streaming.  Receives the tool call dict
        ``{"id": ..., "type": "function", "function": {"name": ..., "arguments": ...}}``.
        Used by :class:`~surogates.harness.streaming_executor.StreamingToolExecutor`
        to start executing concurrency-safe tools before the full LLM
        response has finished streaming.

    Returns ``(assistant_message_dict, usage_data_dict)``.
    """
    has_retried_429 = False
    thinking_sig_retry_attempted = False
    compression_attempts = 0
    max_compression_attempts = 3
    active_on_tool_call_complete = on_tool_call_complete
    # Runaway-reasoning soft retry: we allow at most one in-flight
    # thinking-off retry per call.  See the success-path branch below.
    runaway_retry_used: bool = False

    # Pre-call sanitization: clean surrogates and fix orphaned tool pairs.
    if "messages" in create_kwargs:
        sanitize_messages(create_kwargs["messages"])
        create_kwargs["messages"] = sanitize_tool_pairs(create_kwargs["messages"])

    for attempt in range(1, MAX_LLM_RETRIES + 1):
        if rate_limit_guard is not None:
            remaining = await rate_limit_guard.remaining_seconds()
            if remaining is not None:
                raise RuntimeError(
                    "Provider is rate-limited for "
                    f"{int(remaining)} more seconds; skipping API call."
                )

        try:
            if streaming_enabled:
                result = await call_llm_streaming(
                    session=session,
                    create_kwargs=create_kwargs,
                    iteration=iteration,
                    llm_client=llm_client,
                    store=store,
                    interrupt_check=interrupt_check,
                    set_streaming_enabled=set_streaming_enabled,
                    on_tool_call_complete=active_on_tool_call_complete,
                    turn_id=turn_id,
                )
            else:
                result = await call_llm_non_streaming(
                    session=session,
                    create_kwargs=create_kwargs,
                    iteration=iteration,
                    llm_client=llm_client,
                )

            # Response shape validation.
            assistant_message, usage_data = result
            if not isinstance(assistant_message, dict):
                raise ValueError(
                    f"Invalid LLM response shape: expected dict, got {type(assistant_message).__name__}"
                )
            sanitize_response_message(assistant_message)

            # Empty response -- likely provider issue.  Try fallback
            # immediately instead of burning through retries.
            if result is None or not assistant_message:
                if activate_fallback():
                    current_model = get_current_model()
                    if current_model:
                        create_kwargs["model"] = current_model
                    continue
                raise ValueError("LLM returned empty response")

            # Runaway-reasoning soft retry: model produced > threshold
            # of reasoning_content without any visible content/tool_call.
            # Re-issue once with enable_thinking=False; the same task
            # plus thinking-on would runaway again.  Per-call budget of
            # one silent retry; further runaways fall through as
            # interrupted responses (the loop layer surfaces them).
            if (
                usage_data.get("stream_error_reason") == "runaway_reasoning"
                and not runaway_retry_used
                and attempt < MAX_LLM_RETRIES
            ):
                runaway_retry_used = True
                from surogates.harness.expert_routing import (
                    build_thinking_extra_body,
                    merge_extra_body,
                )
                thinking_extra = build_thinking_extra_body(
                    enable_thinking=False,
                )
                create_kwargs["extra_body"] = merge_extra_body(
                    create_kwargs.get("extra_body"),
                    thinking_extra,
                )
                logger.warning(
                    "Runaway reasoning on session %s iter %d; retrying "
                    "with enable_thinking=False (attempt %d).",
                    session.id, iteration, attempt + 1,
                )
                continue

            # If a runaway retry succeeded, stamp the response so the
            # outer loop knows to flip its per-turn flag.
            if runaway_retry_used:
                usage_data["thinking_disabled_due_to_runaway"] = True

            return result

        except UnicodeEncodeError:
            # Surrogates slipped through -- re-sanitize and retry.
            logger.warning(
                "UnicodeEncodeError on attempt %d; re-sanitizing messages",
                attempt,
            )
            if "messages" in create_kwargs:
                sanitize_messages(create_kwargs["messages"])
                create_kwargs["messages"] = sanitize_tool_pairs(create_kwargs["messages"])
            if attempt >= MAX_LLM_RETRIES:
                raise
            continue

        except Exception as exc:
            status_code = extract_status_code(exc)
            error_msg = str(exc).lower()
            error_type = type(exc).__name__

            # Extract structured error context for credential rotation
            # (e.g. rate-limit reset time from response headers/body).
            error_context = extract_api_error_context(exc)

            # Approximate session size for heuristics.
            api_messages = create_kwargs.get("messages", [])
            approx_tokens = sum(
                len(m.get("content", "") or "") // 4
                for m in api_messages
                if isinstance(m, dict)
            )
            context_window = int(
                getattr(context_compressor, "_context_window", 200_000)
                if context_compressor is not None
                else 200_000
            )
            classified = classify_api_error(
                exc,
                provider=str(create_kwargs.get("provider", "")),
                model=str(create_kwargs.get("model", "")),
                approx_tokens=approx_tokens,
                context_length=context_window,
                num_messages=len(api_messages),
            )

            if isinstance(exc, PartialToolCallStreamError):
                if not classified.retryable or attempt >= MAX_LLM_RETRIES:
                    raise
                await store.emit_event(
                    session.id,
                    EventType.LLM_DELTA,
                    _stamp_turn_meta(
                        {
                            "iteration": iteration,
                            "reconnect": True,
                            "partial_tool_names": exc.partial_tool_names,
                        },
                        iteration=iteration,
                        turn_id=turn_id,
                    ),
                )
                if on_stream_retry is not None:
                    active_on_tool_call_complete = on_stream_retry()
                wait = extract_retry_after(exc) or jittered_backoff(attempt)
                logger.warning(
                    "Stream dropped after partial tool call(s) %s "
                    "(attempt %d/%d). Retrying in %.1fs",
                    exc.partial_tool_names,
                    attempt,
                    MAX_LLM_RETRIES,
                    wait,
                )
                await interruptible_sleep(wait, interrupt_check)
                continue

            # ── Thinking block signature recovery ──────────────────
            # Anthropic signs thinking blocks against the full turn
            # content.  Any upstream mutation (context compression,
            # session truncation, message merging) invalidates the
            # signature -> HTTP 400.  Recovery: strip reasoning_details
            # from all messages so the next retry sends no thinking
            # blocks at all.  One-shot -- don't retry infinitely.
            if (
                classified.reason == FailoverReason.thinking_signature
                and not thinking_sig_retry_attempted
            ):
                thinking_sig_retry_attempted = True
                stripped_count = 0
                for _m in api_messages:
                    if isinstance(_m, dict) and "reasoning_details" in _m:
                        _m.pop("reasoning_details", None)
                        stripped_count += 1
                if stripped_count:
                    logger.warning(
                        "Thinking block signature invalid -- stripped "
                        "reasoning_details from %d messages, retrying",
                        stripped_count,
                    )
                    continue

            # Rate limit (429)
            if status_code == 429:
                if rate_limit_guard is not None:
                    await rate_limit_guard.record_cooldown(
                        _rate_limit_cooldown_seconds(exc, error_context),
                    )
                # ── Anthropic Sonnet long-context tier gate ──────────
                # Anthropic returns HTTP 429 "Extra usage is required
                # for long context requests" when a Claude Max (or
                # similar) subscription doesn't include the 1M-context
                # tier.  This is NOT a transient rate limit -- retrying
                # or switching credentials won't help.  Reduce context
                # and compress.
                _is_long_context_tier_error = (
                    classified.reason == FailoverReason.long_context_tier
                )
                if (
                    _is_long_context_tier_error
                    and context_compressor is not None
                    and compress_context is not None
                ):
                    _reduced_ctx = 200_000
                    old_ctx = getattr(context_compressor, "_context_window", 0)
                    if old_ctx > _reduced_ctx:
                        context_compressor._context_window = _reduced_ctx
                        logger.warning(
                            "Anthropic long-context tier requires extra usage "
                            "-- reducing context: %d -> %d tokens",
                            old_ctx, _reduced_ctx,
                        )
                    compression_attempts += 1
                    if compression_attempts <= max_compression_attempts:
                        compressed = await compress_context(api_messages)
                        if compressed is not None:
                            create_kwargs["messages"] = compressed
                            continue
                    # Fall through to normal 429 handling.

                if not has_retried_429:
                    has_retried_429 = True
                    # First 429: retry with same credential
                else:
                    # Second 429: try credential rotation
                    if rotate_credential(status_code, exc, error_context):
                        has_retried_429 = False
                        continue
                    # Try fallback provider
                    if activate_fallback():
                        has_retried_429 = False
                        current_model = get_current_model()
                        if current_model:
                            create_kwargs["model"] = current_model
                        continue

                # Eager fallback for rate-limit errors when credential
                # pool has no more keys.
                if activate_fallback():
                    has_retried_429 = False
                    current_model = get_current_model()
                    if current_model:
                        create_kwargs["model"] = current_model
                    continue

            # Billing exhausted (402)
            elif status_code == 402:
                if rotate_credential(status_code, exc, error_context):
                    continue
                if activate_fallback():
                    current_model = get_current_model()
                    if current_model:
                        create_kwargs["model"] = current_model
                    continue

            # Auth failure (401)
            elif status_code == 401:
                if rotate_credential(status_code, exc, error_context):
                    continue

            # ── Oversized image recovery ────────────────────────────
            if classified.reason == FailoverReason.image_too_large:
                shrink_count = shrink_image_parts_in_messages(
                    create_kwargs.get("messages", []),
                )
                if shrink_count:
                    logger.warning(
                        "Image payload too large -- shrank %d image part(s) "
                        "and retrying",
                        shrink_count,
                    )
                    continue
                raise

            # ── 413 Payload too large ──────────────────────────────
            is_payload_too_large = (
                classified.reason == FailoverReason.payload_too_large
                or status_code == 413
                or "request entity too large" in error_msg
                or "payload too large" in error_msg
                or "error code: 413" in error_msg
            )
            if is_payload_too_large and compress_context is not None:
                compression_attempts += 1
                if compression_attempts <= max_compression_attempts:
                    logger.warning(
                        "Payload too large (413) -- compression attempt %d/%d",
                        compression_attempts, max_compression_attempts,
                    )
                    compressed = await compress_context(api_messages)
                    if compressed is not None:
                        create_kwargs["messages"] = compressed
                        continue
                # Compression exhausted or didn't help.
                raise

            # ── Context-length error detection ─────────────────────
            # Local backends (LM Studio, Ollama, llama.cpp) often
            # return HTTP 400 with messages like "Context size has been
            # exceeded" which must trigger compression, not abort.
            _CONTEXT_PHRASES = (
                "context length", "context size", "maximum context",
                "token limit", "too many tokens", "reduce the length",
                "exceeds the limit", "context window",
                "request entity too large",
                "prompt is too long",
                "prompt exceeds max length",
            )
            is_context_length_error = (
                classified.reason == FailoverReason.context_overflow
                or any(phrase in error_msg for phrase in _CONTEXT_PHRASES)
            )

            # Fallback heuristic: Anthropic sometimes returns a generic
            # 400 invalid_request_error with just "Error" as the message
            # when the context is too large.  If the error message is
            # very short/generic AND the session is large, treat it as
            # a probable context-length error.
            if not is_context_length_error and status_code == 400:
                is_large_session = (
                    approx_tokens > 80_000
                    or len(api_messages) > 80
                )
                is_generic_error = len(error_msg.strip()) < 30
                if is_large_session and is_generic_error:
                    is_context_length_error = True
                    logger.warning(
                        "Generic 400 with large session (~%d tokens, %d msgs) "
                        "-- treating as probable context overflow",
                        approx_tokens, len(api_messages),
                    )

            # Server disconnects on large sessions are often caused by
            # the request exceeding the provider's context/payload
            # limit without a proper HTTP error response.
            if not is_context_length_error and not status_code:
                _is_server_disconnect = (
                    "server disconnected" in error_msg
                    or "peer closed connection" in error_msg
                    or error_type in (
                        "ReadError", "RemoteProtocolError",
                        "ServerDisconnectedError",
                    )
                )
                if _is_server_disconnect:
                    _is_large = (
                        approx_tokens > 120_000
                        or len(api_messages) > 200
                    )
                    if _is_large:
                        is_context_length_error = True
                        logger.warning(
                            "Server disconnected with large session "
                            "(~%d tokens, %d msgs) -- treating as "
                            "context-length error",
                            approx_tokens, len(api_messages),
                        )

            if is_context_length_error and compress_context is not None:
                # Try to parse the actual limit from the error message
                # and step down the context window.
                if context_compressor is not None:
                    old_ctx = getattr(
                        context_compressor, "_context_window",
                        128_000,
                    )
                    parsed_limit = parse_context_limit_from_error(error_msg)
                    if parsed_limit and parsed_limit < old_ctx:
                        new_ctx = parsed_limit
                    else:
                        new_ctx = get_next_probe_tier(old_ctx)

                    if new_ctx and new_ctx < old_ctx:
                        context_compressor._context_window = new_ctx
                        logger.warning(
                            "Context length exceeded -- stepping down: "
                            "%d -> %d tokens",
                            old_ctx, new_ctx,
                        )

                compression_attempts += 1
                if compression_attempts <= max_compression_attempts:
                    logger.warning(
                        "Context too large (~%d tokens) -- compressing "
                        "(%d/%d)",
                        approx_tokens,
                        compression_attempts,
                        max_compression_attempts,
                    )
                    compressed = await compress_context(api_messages)
                    if compressed is not None:
                        create_kwargs["messages"] = compressed
                        continue
                # Compression exhausted or didn't help.

            # ── Non-retryable client errors ────────────────────────
            # 4xx errors (except 413/429/529) indicate a problem with
            # the request itself (bad model ID, invalid API key,
            # forbidden, etc.) and will never succeed on retry.
            _RETRYABLE_STATUS_CODES = {413, 429, 529}
            is_local_validation_error = (
                isinstance(exc, (ValueError, TypeError))
                and not isinstance(exc, UnicodeEncodeError)
            )
            # Detect generic 400s from Anthropic OAuth (transient
            # server-side failures).
            _err_body = getattr(exc, "body", None) or {}
            _err_message = (
                _err_body.get("error", {}).get("message", "")
                if isinstance(_err_body, dict) else ""
            )
            _is_generic_400 = (
                status_code == 400
                and _err_message.strip().lower() in ("error", "")
            )
            is_client_status_error = (
                isinstance(status_code, int)
                and 400 <= status_code < 500
                and status_code not in _RETRYABLE_STATUS_CODES
                and not _is_generic_400
            )
            is_client_error = (
                is_local_validation_error
                or is_client_status_error
                or any(
                    phrase in error_msg
                    for phrase in (
                        "error code: 401", "error code: 403",
                        "error code: 404", "error code: 422",
                        "is not a valid model", "invalid model",
                        "model not found", "invalid api key",
                        "invalid_api_key", "authentication",
                        "unauthorized", "forbidden", "not found",
                    )
                )
            ) and not is_context_length_error

            if is_client_error:
                # Try fallback before aborting -- a different provider
                # may not have the same issue.
                if activate_fallback():
                    current_model = get_current_model()
                    if current_model:
                        create_kwargs["model"] = current_model
                    continue
                raise  # Non-retryable

            # Transient server errors (500, 502, 503, 529)
            is_rate_limited = (
                status_code == 429
                or "rate limit" in error_msg
                or "too many requests" in error_msg
                or "rate_limit" in error_msg
                or "usage limit" in error_msg
                or "quota" in error_msg
            )
            is_retryable = (
                classified.retryable
                or
                status_code in (429, 500, 502, 503, 529)
                or is_transient_error(exc)
            )

            if not is_retryable or attempt >= MAX_LLM_RETRIES:
                # Try fallback as last resort
                if activate_fallback():
                    current_model = get_current_model()
                    if current_model:
                        create_kwargs["model"] = current_model
                    continue

                # ── SSE stream-drop guidance ───────────────────────
                _is_stream_drop = (
                    not status_code
                    and any(
                        p in error_msg
                        for p in (
                            "connection lost", "connection reset",
                            "connection closed", "network connection",
                            "network error", "terminated",
                        )
                    )
                )
                if _is_stream_drop:
                    logger.error(
                        "Provider stream connection keeps dropping. "
                        "This often happens when the model tries to "
                        "write a very large file in a single tool call.",
                    )

                raise  # Give up

            # Compute wait with Retry-After or jittered backoff.
            wait = extract_retry_after(exc) or jittered_backoff(attempt)
            logger.warning(
                "LLM call failed (attempt %d/%d, status=%s): %s. "
                "Retrying in %.1fs",
                attempt, MAX_LLM_RETRIES, status_code,
                summarize_api_error(exc), wait,
            )

            # Interruptible sleep
            await interruptible_sleep(wait, interrupt_check)

    raise RuntimeError("LLM call exhausted all retries")


# ---------------------------------------------------------------------------
# Streaming LLM call
# ---------------------------------------------------------------------------


async def call_llm_streaming(
    *,
    session: Session,
    create_kwargs: dict[str, Any],
    iteration: int,
    llm_client: AsyncOpenAI,
    store: SessionStore,
    interrupt_check: Callable[[], bool],
    set_streaming_enabled: Callable[[bool], None],
    on_tool_call_complete: Callable[[dict[str, Any]], None] | None = None,
    turn_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the LLM with ``stream=True``, emit deltas, return full message + usage.

    If streaming fails (e.g. provider does not support it), fall back
    to non-streaming automatically.
    """
    try:
        return await call_llm_streaming_inner(
            session=session,
            create_kwargs=create_kwargs,
            iteration=iteration,
            llm_client=llm_client,
            store=store,
            interrupt_check=interrupt_check,
            on_tool_call_complete=on_tool_call_complete,
            turn_id=turn_id,
        )
    except PartialToolCallStreamError:
        raise
    except Exception as exc:
        logger.warning(
            "Streaming failed for session %s (iteration %d), "
            "falling back to non-streaming: %s",
            session.id,
            iteration,
            exc,
        )
        set_streaming_enabled(False)
        return await call_llm_non_streaming(
            session=session,
            create_kwargs=create_kwargs,
            iteration=iteration,
            llm_client=llm_client,
        )


async def call_llm_streaming_inner(
    *,
    session: Session,
    create_kwargs: dict[str, Any],
    iteration: int,
    llm_client: AsyncOpenAI,
    store: SessionStore,
    interrupt_check: Callable[[], bool] | None = None,
    on_tool_call_complete: Callable[[dict[str, Any]], None] | None = None,
    turn_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inner streaming implementation.

    Iterates over the async stream, emitting ``LLM_DELTA`` events for
    every text chunk.  Accumulates tool call deltas into a complete
    tool_calls list.  Emits the full reconstructed message at the end.

    If *interrupt_check* is provided and returns ``True`` during streaming,
    the HTTP stream is cancelled via ``response.close()`` and whatever has
    been accumulated so far is returned as a partial response.

    If *on_tool_call_complete* is provided, it is called each time a
    complete tool_use block is detected during streaming.  A tool block
    is considered complete when a higher-index tool call begins or when
    the stream ends.  This enables the
    :class:`~surogates.harness.streaming_executor.StreamingToolExecutor`
    to start executing concurrency-safe tools before the full response
    is available.
    """
    # Inject prompt cache extra_body for cacheable models.
    final_kwargs = dict(create_kwargs)
    model_id = final_kwargs.get("model", "")
    base_url = str(
        getattr(llm_client, "base_url", "")
        or final_kwargs.get("base_url", "")
        or ""
    )
    explicit_stale_timeout = (
        STREAM_STALE_TIMEOUT if STREAM_STALE_TIMEOUT_EXPLICIT else None
    )
    stale_timeout = compute_stream_stale_timeout(
        final_kwargs.get("messages", []),
        base_url=base_url,
        model=model_id,
        explicit_timeout=explicit_stale_timeout,
    )
    cache_extra = build_cache_extra_body(model_id)
    if cache_extra is not None:
        existing_extra = final_kwargs.get("extra_body") or {}
        merged = {**existing_extra, **cache_extra}
        final_kwargs["extra_body"] = merged

    # Detect API mode (Phase 1: always chat_completions).
    api_mode = detect_api_mode(model_id)
    if api_mode != APIMode.CHAT_COMPLETIONS:
        # Phase 2: route to call_anthropic_messages.
        pass  # pragma: no cover

    response = await llm_client.chat.completions.create(
        **final_kwargs,
        stream=True,
        stream_options={"include_usage": True},
    )

    # Accumulators
    content_parts: list[str] = []
    tool_calls_acc: dict[int, dict[str, Any]] = {}  # index -> partial tool call
    role: str = "assistant"
    finish_reason: str | None = None
    model: str = final_kwargs.get("model", "")
    interrupted: bool = False

    # Ollama tool-call deduplication tracking.
    # Ollama-compatible endpoints reuse index 0 for every tool call
    # in a parallel batch, distinguishing them only by id.
    _last_id_at_idx: dict[int, str] = {}      # raw_index -> last seen non-empty id
    _active_slot_by_idx: dict[int, int] = {}  # raw_index -> current slot in tool_calls_acc

    # Reasoning content from streaming deltas (DeepSeek, Qwen, Moonshot).
    reasoning_parts: list[str] = []
    think_scrubber = StreamingThinkScrubber()
    context_scrubber = StreamingContextScrubber()

    # Streaming tool execution: track which tool call slots have been
    # notified to the on_tool_call_complete callback.  A slot is
    # considered complete when a higher-index slot appears (the LLM
    # generates tool calls sequentially).
    _notified_slots: set[int] = set()
    _highest_known_slot: int = -1

    # Usage data from the stream (some providers include it in the
    # final chunk via ``stream_options``).
    input_tokens: int = 0
    output_tokens: int = 0

    # Stale stream detection: wall-clock timestamp of the last real
    # streaming chunk.  If no real chunk arrives within the timeout,
    # the stream is considered stale and will be cancelled.
    last_chunk_time: float = time.monotonic()

    # Runaway-reasoning detection: count reasoning_content chars
    # received and whether any content or tool-call delta has landed.
    # If the model emits a large amount of reasoning_content before
    # any visible output, the stream is cancelled and the outer retry
    # re-issues with enable_thinking=False (see Task 7).
    reasoning_char_count: int = 0
    content_or_tool_emitted: bool = False

    async def _close_stream() -> None:
        close_method = getattr(response, "aclose", None) or getattr(
            response, "close", None,
        )
        if close_method is None:
            return
        try:
            result = close_method()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    # Watchdog: wakes up every ``STREAM_CHUNK_POLL_INTERVAL`` to check
    # whether the stream has gone silent past the request-specific
    # stale timeout
    # or whether the caller asked us to interrupt.  Uses a plain
    # ``asyncio.Event`` to signal the main ``async for`` that it should
    # stop -- closing the response forces the iterator to raise, which
    # we catch outside.  We deliberately don't wrap ``__anext__`` in
    # ``asyncio.wait_for``: cancelling an SDK's in-flight read can
    # leave the underlying stream in a weird state, whereas closing
    # the response is the SDK-sanctioned way to abort.
    stop_reason: str | None = None
    stop_event = asyncio.Event()
    last_heartbeat_time: float = time.monotonic()

    async def _watchdog() -> None:
        nonlocal stop_reason, last_heartbeat_time
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=STREAM_CHUNK_POLL_INTERVAL,
                )
                return  # stop_event fired -- main loop already done
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if (now - last_chunk_time) > stale_timeout:
                logger.warning(
                    "Stream stale for %.0fs (threshold %.0fs) — no chunks "
                    "received. Cancelling stream for session %s (iteration %d).",
                    now - last_chunk_time,
                    stale_timeout,
                    session.id,
                    iteration,
                )
                stop_reason = "stale"
                await _close_stream()
                return
            if interrupt_check is not None and interrupt_check():
                logger.info(
                    "Mid-stream interrupt for session %s (iteration %d); "
                    "cancelling stream",
                    session.id,
                    iteration,
                )
                stop_reason = "interrupt"
                await _close_stream()
                return

            # Heartbeat: stream is silent but still inside the stale
            # window.  Surface a transient signal so the UI can show
            # "model is still working" rather than appearing dead.
            silent_for = now - last_chunk_time
            since_last_beat = now - last_heartbeat_time
            if (
                silent_for >= STREAM_HEARTBEAT_INTERVAL
                and since_last_beat >= STREAM_HEARTBEAT_INTERVAL
            ):
                last_heartbeat_time = now
                try:
                    await store.emit_event(
                        session.id,
                        EventType.LLM_HEARTBEAT,
                        {
                            "iteration": iteration,
                            "silent_for_seconds": round(silent_for, 1),
                        },
                    )
                except Exception:
                    # Heartbeat is best-effort -- never fail the stream
                    # because the store transiently rejected an event.
                    logger.debug(
                        "Heartbeat emit failed for session %s",
                        session.id,
                        exc_info=True,
                    )

    watchdog_task = asyncio.create_task(
        _watchdog(), name=f"llm-stream-watchdog-{session.id}",
    )

    try:
        async for chunk in response:
            last_chunk_time = time.monotonic()
            last_heartbeat_time = last_chunk_time
            if stop_reason is not None:
                interrupted = True
                break
            # Mid-stream interrupt check also runs on chunk-receive so a
            # user who hits Stop during a healthy stream doesn't have to
            # wait for the next watchdog tick.
            if interrupt_check is not None and interrupt_check():
                logger.info(
                    "Mid-stream interrupt for session %s (iteration %d); "
                    "cancelling stream",
                    session.id,
                    iteration,
                )
                await _close_stream()
                interrupted = True
                break
            if not chunk.choices:
                # Final chunk may carry usage without choices.
                if hasattr(chunk, "usage") and chunk.usage:
                    input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0
                if hasattr(chunk, "model") and chunk.model:
                    model = chunk.model
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason

            if hasattr(chunk, "model") and chunk.model:
                model = chunk.model

            # Usage from final chunk (OpenAI pattern).
            if hasattr(chunk, "usage") and chunk.usage:
                input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0

            if delta is None:
                continue

            # Role (usually arrives in the first chunk only).
            if hasattr(delta, "role") and delta.role:
                role = delta.role

            # Reasoning content delta (DeepSeek, Qwen, Moonshot, GLM-5).
            reasoning_text = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
            )
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
                reasoning_char_count += len(reasoning_text)
                # Emit LLM_DELTA event for reasoning so the frontend can
                # stream reasoning content incrementally (same as text deltas).
                await store.emit_event(
                    session.id,
                    EventType.LLM_DELTA,
                    _stamp_turn_meta(
                        {"reasoning": reasoning_text, "iteration": iteration},
                        iteration=iteration,
                        turn_id=turn_id,
                    ),
                )

            # Text content delta.
            text_delta = getattr(delta, "content", None)
            if text_delta:
                visible_delta = context_scrubber.feed(think_scrubber.feed(text_delta))
                if visible_delta:
                    content_or_tool_emitted = True
                    content_parts.append(visible_delta)
                    # Emit LLM_DELTA event.
                    await store.emit_event(
                        session.id,
                        EventType.LLM_DELTA,
                        _stamp_turn_meta(
                            {"content": visible_delta, "iteration": iteration},
                            iteration=iteration,
                            turn_id=turn_id,
                        ),
                    )

            # Tool call deltas.
            # Ollama-compatible endpoints reuse index 0 for every tool call
            # in a parallel batch, distinguishing them only by id.  Track
            # the last seen id per raw index so we can detect a new tool
            # call starting at the same index and redirect it to a fresh slot.
            tc_deltas = getattr(delta, "tool_calls", None)
            if tc_deltas:
                content_or_tool_emitted = True
                for tc_delta in tc_deltas:
                    raw_idx = tc_delta.index if tc_delta.index is not None else 0
                    delta_id = getattr(tc_delta, "id", None) or ""

                    if raw_idx not in _active_slot_by_idx:
                        _active_slot_by_idx[raw_idx] = raw_idx
                    if (
                        delta_id
                        and raw_idx in _last_id_at_idx
                        and delta_id != _last_id_at_idx[raw_idx]
                    ):
                        new_slot = max(tool_calls_acc, default=-1) + 1
                        _active_slot_by_idx[raw_idx] = new_slot
                    if delta_id:
                        _last_id_at_idx[raw_idx] = delta_id
                    idx = _active_slot_by_idx[raw_idx]

                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": getattr(tc_delta, "id", None) or "",
                            "type": "function",
                            "function": {
                                "name": "",
                                "arguments": "",
                            },
                        }
                    entry = tool_calls_acc[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    fn_delta = getattr(tc_delta, "function", None)
                    if fn_delta:
                        if fn_delta.name:
                            entry["function"]["name"] += fn_delta.name
                        if fn_delta.arguments:
                            entry["function"]["arguments"] += fn_delta.arguments

                    # Streaming tool execution: when a new higher-index slot
                    # appears, all lower-index slots are fully formed.  Fire
                    # the callback so the StreamingToolExecutor can start
                    # executing concurrency-safe tools immediately.
                    if on_tool_call_complete is not None and idx > _highest_known_slot:
                        for prev_slot in sorted(tool_calls_acc):
                            if prev_slot < idx and prev_slot not in _notified_slots:
                                prev_entry = tool_calls_acc[prev_slot]
                                if prev_entry["function"]["name"]:
                                    _notified_slots.add(prev_slot)
                                    on_tool_call_complete(prev_entry)
                        _highest_known_slot = idx

            # Runaway-reasoning: model has emitted > threshold chars of
            # reasoning_content without any visible content or tool call.
            # Cancel; the outer retry will reissue with thinking disabled.
            if (
                not content_or_tool_emitted
                and reasoning_char_count > RUNAWAY_REASONING_CHAR_THRESHOLD
            ):
                logger.warning(
                    "Runaway reasoning: %d chars of reasoning_content with no "
                    "content/tool_call (threshold %d). Cancelling stream for "
                    "session %s (iteration %d).",
                    reasoning_char_count,
                    RUNAWAY_REASONING_CHAR_THRESHOLD,
                    session.id,
                    iteration,
                )
                stop_reason = "runaway_reasoning"
                await _close_stream()
                interrupted = True
                break
    except Exception as exc:
        # The watchdog closed the stream (stale or interrupt) -- the
        # SDK's iterator raises when the underlying response is closed
        # mid-read.  We swallow only the close-induced error; any other
        # exception propagates normally.
        if stop_reason is None:
            partial_tool_names = _partial_tool_names_from_accumulator(
                tool_calls_acc,
                treat_empty_args_as_partial=True,
            )
            if partial_tool_names:
                await _close_stream()
                raise PartialToolCallStreamError(partial_tool_names, exc) from exc
            raise
        interrupted = True
    finally:
        stop_event.set()
        watchdog_task.cancel()
        try:
            await watchdog_task
        except (asyncio.CancelledError, Exception):
            pass

    # Some SDK iterators respond to ``aclose`` by returning cleanly
    # (StopAsyncIteration) rather than raising.  When the watchdog
    # triggered the close, surface it as an interruption regardless.
    if stop_reason is not None:
        interrupted = True

    tail_delta = context_scrubber.feed(think_scrubber.flush()) + context_scrubber.flush()
    if tail_delta:
        content_parts.append(tail_delta)
        await store.emit_event(
            session.id,
            EventType.LLM_DELTA,
            _stamp_turn_meta(
                {"content": tail_delta, "iteration": iteration},
                iteration=iteration,
                turn_id=turn_id,
            ),
        )

    partial_tool_names = _partial_tool_names_from_accumulator(tool_calls_acc)

    # Notify any remaining tool call slots that haven't been reported yet.
    # This covers the last tool call in the batch (no higher index follows)
    # and the case where only a single tool call was generated.
    if on_tool_call_complete is not None:
        for slot in sorted(tool_calls_acc):
            if slot not in _notified_slots:
                entry = tool_calls_acc[slot]
                if entry["function"]["name"]:
                    _notified_slots.add(slot)
                    on_tool_call_complete(entry)

    # Reconstruct the complete assistant message.
    assistant_message = reconstruct_message_from_deltas(
        role=role,
        content_parts=content_parts,
        tool_calls_acc=tool_calls_acc,
    )

    # Attach reasoning content from streaming deltas so the loop can
    # extract it the same way it handles non-streaming responses.
    full_reasoning = "".join(reasoning_parts) if reasoning_parts else None
    if full_reasoning:
        assistant_message["reasoning_content"] = full_reasoning

    usage_data: dict[str, Any] = {
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "finish_reason": "interrupted" if interrupted else (finish_reason or "stop"),
    }
    if interrupted and stop_reason is not None:
        usage_data["stream_error_reason"] = stop_reason
    if partial_tool_names:
        usage_data["partial_tool_call"] = True
        usage_data["partial_tool_names"] = partial_tool_names

    return sanitize_response_message(assistant_message), usage_data


# ---------------------------------------------------------------------------
# Non-streaming LLM call
# ---------------------------------------------------------------------------


async def call_llm_non_streaming(
    *,
    session: Session,
    create_kwargs: dict[str, Any],
    iteration: int,
    llm_client: AsyncOpenAI,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the LLM without streaming. Returns (message_dict, usage_dict)."""
    # Inject prompt cache extra_body for cacheable models.
    final_kwargs = dict(create_kwargs)
    model_id = final_kwargs.get("model", "")
    cache_extra = build_cache_extra_body(model_id)
    if cache_extra is not None:
        existing_extra = final_kwargs.get("extra_body") or {}
        merged = {**existing_extra, **cache_extra}
        final_kwargs["extra_body"] = merged

    response = await llm_client.chat.completions.create(**final_kwargs)

    # Response shape validation.
    if response is None or not getattr(response, "choices", None):
        raise ValueError(
            "Invalid LLM response: response is None or has no choices"
        )

    choice = response.choices[0]
    if not hasattr(choice, "message") or choice.message is None:
        raise ValueError(
            "Invalid LLM response: choices[0].message is missing or None"
        )

    assistant_message_dict = message_to_dict(choice.message)

    usage = response.usage
    input_tokens = usage.prompt_tokens if usage else 0
    output_tokens = usage.completion_tokens if usage else 0

    usage_data: dict[str, Any] = {
        "model": response.model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "finish_reason": choice.finish_reason,
    }

    return sanitize_response_message(assistant_message_dict), usage_data
