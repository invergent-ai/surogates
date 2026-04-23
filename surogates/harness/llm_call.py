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

from surogates.harness.message_utils import (
    message_to_dict,
    reconstruct_message_from_deltas,
)
from surogates.harness.model_metadata import (
    get_next_probe_tier,
    parse_context_limit_from_error,
)
from surogates.harness.prompt_cache import build_cache_extra_body, is_cacheable_model
from surogates.harness.provider import APIMode, detect_api_mode
from surogates.harness.resilience import extract_api_error_context, summarize_api_error
from surogates.harness.retry import jittered_backoff
from surogates.harness.sanitize import sanitize_messages, sanitize_tool_pairs
from surogates.session.events import EventType

if TYPE_CHECKING:
    from openai import AsyncOpenAI

    from surogates.session.models import Session
    from surogates.session.store import SessionStore

logger = logging.getLogger(__name__)

# Retry constants
MAX_LLM_RETRIES: int = 3

# Stale stream detection timeout (seconds).  If no real streaming chunk
# arrives within this window the stream is considered stale and will be
# cancelled.  Configurable via ``SUROGATES_STREAM_STALE_TIMEOUT`` env var.
STREAM_STALE_TIMEOUT: float = float(
    os.environ.get("SUROGATES_STREAM_STALE_TIMEOUT", "180.0")
)

# Polling interval (seconds) used to wake up between chunk reads so the
# stale-stream and interrupt checks run even when the upstream provider
# stops sending bytes entirely.  Short enough that user-initiated stops
# feel responsive; long enough that it doesn't burn CPU on a healthy
# stream.
STREAM_CHUNK_POLL_INTERVAL: float = 1.0

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

    # Pre-call sanitization: clean surrogates and fix orphaned tool pairs.
    if "messages" in create_kwargs:
        sanitize_messages(create_kwargs["messages"])
        create_kwargs["messages"] = sanitize_tool_pairs(create_kwargs["messages"])

    for attempt in range(1, MAX_LLM_RETRIES + 1):
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
                    on_tool_call_complete=on_tool_call_complete,
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

            # Empty response -- likely provider issue.  Try fallback
            # immediately instead of burning through retries.
            if result is None or not assistant_message:
                if activate_fallback():
                    current_model = get_current_model()
                    if current_model:
                        create_kwargs["model"] = current_model
                    continue
                raise ValueError("LLM returned empty response")

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

            # ── Thinking block signature recovery ──────────────────
            # Anthropic signs thinking blocks against the full turn
            # content.  Any upstream mutation (context compression,
            # session truncation, message merging) invalidates the
            # signature -> HTTP 400.  Recovery: strip reasoning_details
            # from all messages so the next retry sends no thinking
            # blocks at all.  One-shot -- don't retry infinitely.
            if (
                status_code == 400
                and not thinking_sig_retry_attempted
                and "signature" in error_msg
                and "thinking" in error_msg
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
                # ── Anthropic Sonnet long-context tier gate ──────────
                # Anthropic returns HTTP 429 "Extra usage is required
                # for long context requests" when a Claude Max (or
                # similar) subscription doesn't include the 1M-context
                # tier.  This is NOT a transient rate limit -- retrying
                # or switching credentials won't help.  Reduce context
                # and compress.
                _is_long_context_tier_error = (
                    "extra usage" in error_msg
                    and "long context" in error_msg
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

            # ── 413 Payload too large ──────────────────────────────
            is_payload_too_large = (
                status_code == 413
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
            is_context_length_error = any(
                phrase in error_msg for phrase in _CONTEXT_PHRASES
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
        )
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
    # whether the stream has gone silent past ``STREAM_STALE_TIMEOUT``
    # or whether the caller asked us to interrupt.  Uses a plain
    # ``asyncio.Event`` to signal the main ``async for`` that it should
    # stop -- closing the response forces the iterator to raise, which
    # we catch outside.  We deliberately don't wrap ``__anext__`` in
    # ``asyncio.wait_for``: cancelling an SDK's in-flight read can
    # leave the underlying stream in a weird state, whereas closing
    # the response is the SDK-sanctioned way to abort.
    stop_reason: str | None = None
    stop_event = asyncio.Event()

    async def _watchdog() -> None:
        nonlocal stop_reason
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=STREAM_CHUNK_POLL_INTERVAL,
                )
                return  # stop_event fired -- main loop already done
            except asyncio.TimeoutError:
                pass

            now = time.monotonic()
            if (now - last_chunk_time) > STREAM_STALE_TIMEOUT:
                logger.warning(
                    "Stream stale for %.0fs (threshold %.0fs) — no chunks "
                    "received. Cancelling stream for session %s (iteration %d).",
                    now - last_chunk_time,
                    STREAM_STALE_TIMEOUT,
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

    watchdog_task = asyncio.create_task(
        _watchdog(), name=f"llm-stream-watchdog-{session.id}",
    )

    try:
        async for chunk in response:
            last_chunk_time = time.monotonic()
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

            # Reasoning content delta (DeepSeek, Qwen, Moonshot, etc.)
            reasoning_text = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
            )
            if reasoning_text:
                reasoning_parts.append(reasoning_text)
                # Emit LLM_DELTA event for reasoning so the frontend can
                # stream reasoning content incrementally (same as text deltas).
                await store.emit_event(
                    session.id,
                    EventType.LLM_DELTA,
                    {"reasoning": reasoning_text, "iteration": iteration},
                )

            # Text content delta.
            text_delta = getattr(delta, "content", None)
            if text_delta:
                content_parts.append(text_delta)
                # Emit LLM_DELTA event.
                await store.emit_event(
                    session.id,
                    EventType.LLM_DELTA,
                    {"content": text_delta, "iteration": iteration},
                )

            # Tool call deltas.
            # Ollama-compatible endpoints reuse index 0 for every tool call
            # in a parallel batch, distinguishing them only by id.  Track
            # the last seen id per raw index so we can detect a new tool
            # call starting at the same index and redirect it to a fresh slot.
            tc_deltas = getattr(delta, "tool_calls", None)
            if tc_deltas:
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
    except Exception:
        # The watchdog closed the stream (stale or interrupt) -- the
        # SDK's iterator raises when the underlying response is closed
        # mid-read.  We swallow only the close-induced error; any other
        # exception propagates normally.
        if stop_reason is None:
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

    return assistant_message, usage_data


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

    return assistant_message_dict, usage_data
