"""Message serialization helpers for the agent harness.

Provides utilities for converting OpenAI ChatCompletionMessage objects to
plain dicts, reconstructing complete messages from streaming deltas, and
creating synthetic tool results for skipped (interrupted) calls.
"""

from __future__ import annotations

from typing import Any


def message_to_dict(message: Any) -> dict:
    """Convert an OpenAI ChatCompletionMessage to a plain dict.

    Handles both Pydantic-v1 style ``.dict()`` and Pydantic-v2 style
    ``.model_dump()``, falling back to manual extraction for maximum
    compatibility across openai SDK versions.

    Also preserves provider-specific fields that are critical for multi-turn
    reasoning continuity and provider compatibility:

    - ``reasoning_details`` -- opaque per-turn reasoning state from
      OpenRouter/Anthropic/OpenAI that must be passed back on subsequent turns.
    - ``reasoning_content`` -- structured reasoning from DeepSeek/Qwen/Moonshot.
    - ``extra_content`` -- Gemini thought_signature on tool calls.
    """
    # Try the modern SDK approach first.
    if hasattr(message, "model_dump"):
        d = message.model_dump(exclude_none=True)
    elif hasattr(message, "dict"):
        d = message.dict(exclude_none=True)
    else:
        # Manual fallback.
        d: dict[str, Any] = {"role": getattr(message, "role", "assistant")}
        content = getattr(message, "content", None)
        if content is not None:
            d["content"] = content
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            d["tool_calls"] = _serialise_tool_calls(tool_calls)
        _copy_reasoning_fields(message, d)
        return d

    # Ensure tool_calls are serialisable plain dicts.
    if "tool_calls" in d and d["tool_calls"]:
        d["tool_calls"] = _serialise_tool_calls(d["tool_calls"])

    # Preserve reasoning fields from the SDK object if not already in the dict.
    _copy_reasoning_fields(message, d)

    return d


def _serialise_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    """Serialise tool calls to plain dicts, preserving extra_content.

    ``extra_content`` (e.g. Gemini thought_signature) must be sent back on
    subsequent API calls.  Without this, Gemini 3 thinking models reject
    the request with a 400 error.
    """
    serialised_tcs: list[dict[str, Any]] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            serialised_tcs.append(tc)
        elif hasattr(tc, "model_dump"):
            tc_dict = tc.model_dump(exclude_none=True)
            # Preserve extra_content that model_dump may have excluded.
            extra = getattr(tc, "extra_content", None)
            if extra is not None and "extra_content" not in tc_dict:
                if hasattr(extra, "model_dump"):
                    extra = extra.model_dump()
                tc_dict["extra_content"] = extra
            serialised_tcs.append(tc_dict)
        else:
            tc_dict: dict[str, Any] = {
                "id": getattr(tc, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(tc.function, "name", ""),
                    "arguments": getattr(tc.function, "arguments", ""),
                },
            }
            extra = getattr(tc, "extra_content", None)
            if extra is not None:
                if hasattr(extra, "model_dump"):
                    extra = extra.model_dump()
                tc_dict["extra_content"] = extra
            serialised_tcs.append(tc_dict)
    return serialised_tcs


def _copy_reasoning_fields(message: Any, d: dict[str, Any]) -> None:
    """Copy reasoning-related fields from the SDK message object into *d*.

    These fields are critical for multi-turn reasoning continuity:

    - ``reasoning_details`` -- opaque per-turn reasoning state from
      OpenRouter/Anthropic/OpenAI (includes signature, encrypted_content).
    - ``reasoning_content`` -- structured reasoning from DeepSeek/Qwen/Moonshot.
    """
    # reasoning_details (OpenRouter, Anthropic, OpenAI)
    if "reasoning_details" not in d:
        raw_details = getattr(message, "reasoning_details", None)
        if raw_details:
            preserved = []
            for detail in raw_details:
                if isinstance(detail, dict):
                    preserved.append(detail)
                elif hasattr(detail, "model_dump"):
                    preserved.append(detail.model_dump())
                elif hasattr(detail, "__dict__"):
                    preserved.append(detail.__dict__)
            if preserved:
                d["reasoning_details"] = preserved

    # reasoning_content (DeepSeek, Qwen, Moonshot, Novita)
    if "reasoning_content" not in d:
        rc = getattr(message, "reasoning_content", None)
        if rc and isinstance(rc, str):
            d["reasoning_content"] = rc


def reconstruct_message_from_deltas(
    *,
    role: str,
    content_parts: list[str],
    tool_calls_acc: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Build a complete assistant message dict from accumulated stream deltas.

    This mirrors the shape produced by :func:`message_to_dict` so that
    downstream code (event storage, replay) can treat streaming and
    non-streaming responses identically.
    """
    message: dict[str, Any] = {"role": role}

    full_content = "".join(content_parts)
    if full_content:
        message["content"] = full_content

    if tool_calls_acc:
        # Sort by index to preserve the original order.
        sorted_tcs: list[dict[str, Any]] = [
            tool_calls_acc[idx]
            for idx in sorted(tool_calls_acc.keys())
        ]
        message["tool_calls"] = sorted_tcs

    return message


def make_skipped_tool_result(
    tc: dict[str, Any],
    reason: str = "skipped due to interrupt",
) -> dict:
    """Return a synthetic tool result for a tool that did not execute.

    *reason* is included in the result content so the LLM understands
    why the tool produced no output.  Common reasons:

    - ``"skipped due to interrupt"`` (default) — the harness was interrupted.
    - ``"cancelled (sibling error)"`` — a sibling tool's error triggered
      abort of concurrent executions.
    """
    tool_call_id = tc.get("id", "")
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": f"[{reason}]",
    }


# ---------------------------------------------------------------------------
# Final response extraction (shared by delegate_task and worker_notify)
# ---------------------------------------------------------------------------


def extract_final_response(
    events: list[Any],
    fallback: str = "(no response produced)",
) -> str:
    """Extract the last LLM response content from a list of session events.

    Scans *events* in reverse for the last ``LLM_RESPONSE`` event and
    returns its ``message.content``.  Returns *fallback* if no response
    is found.

    Used by :mod:`~surogates.tools.builtin.delegate` and
    :mod:`~surogates.harness.worker_notify` to retrieve a child
    session's final output.
    """
    from surogates.session.events import EventType

    for event in reversed(events):
        if event.type == EventType.LLM_RESPONSE.value:
            message = event.data.get("message", {})
            content = message.get("content")
            if content:
                return str(content)

    return fallback


# ---------------------------------------------------------------------------
# Content type coercion
# ---------------------------------------------------------------------------


def coerce_message_content(message: dict[str, Any]) -> dict[str, Any]:
    """Normalize message content to a string.

    Some local backends (llama-server, LM Studio, Ollama with certain
    models) return ``content`` as a ``dict`` or ``list`` instead of a
    ``str``.  This normalizes to a string so the rest of the harness
    can process it uniformly.

    Handles:
    - ``list`` of dicts with ``"text"`` keys → joined text
    - ``dict`` with a ``"text"`` key → extracted text
    - Other non-string types → ``json.dumps``
    - ``None`` → ``None`` (left as-is)
    """
    content = message.get("content")
    if content is None or isinstance(content, str):
        return message

    import json

    if isinstance(content, list):
        # Multimodal content: [{"type": "text", "text": "..."}, ...]
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text") or part.get("content") or ""
                if text:
                    text_parts.append(str(text))
            elif isinstance(part, str):
                text_parts.append(part)
        message["content"] = "\n".join(text_parts) if text_parts else ""
    elif isinstance(content, dict):
        text = content.get("text") or content.get("content") or ""
        if text:
            message["content"] = str(text)
        else:
            message["content"] = json.dumps(content, ensure_ascii=False)
    else:
        message["content"] = str(content)

    return message
