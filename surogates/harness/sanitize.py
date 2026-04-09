"""Message sanitization -- prevents encoding crashes and API 400 errors.

Provides pre-call sanitization functions that are applied to the message
list before every LLM API call:

- **Surrogate character sanitization** -- lone UTF-16 surrogates (U+D800-U+DFFF)
  crash ``json.dumps()`` in the OpenAI SDK.
- **Orphaned tool call / tool result pair fixup** -- drops orphaned tool results
  and injects stub results for unmatched tool calls.
- **Budget warning stripping** -- budget warnings are turn-scoped and must be
  removed from replayed messages. 
- **Duplicate tool call deduplication** -- removes duplicate (name, arguments)
  pairs from a tool call batch. 
- **Delegate task capping** -- caps the number of ``delegate_task`` calls per turn.
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Surrogate character sanitization
# ---------------------------------------------------------------------------

# Lone UTF-16 surrogates that crash json.dumps()
_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


def sanitize_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogate code points with U+FFFD."""
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub('\ufffd', text)
    return text


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Walk all string fields in messages and sanitize surrogates.

    Handles both simple string content and list-of-dicts multimodal content
    (e.g. ``[{"type": "text", "text": "..."}, {"type": "image_url", ...}]``).

    Operates in-place for performance but also returns the list for chaining.
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = sanitize_surrogates(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    for key in ("text", "content"):
                        if key in part and isinstance(part[key], str):
                            part[key] = sanitize_surrogates(part[key])
        # Also sanitize tool call arguments
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            if isinstance(fn.get("arguments"), str):
                fn["arguments"] = sanitize_surrogates(fn["arguments"])
    return messages


# ---------------------------------------------------------------------------
# Orphaned tool call / tool result pair fixup
# ---------------------------------------------------------------------------


def sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
    """Fix orphaned tool_call/tool_result pairs that cause API 400 errors.

    - Drops tool results whose call_id has no matching tool_call in any
      prior assistant message
    - Injects stub results for tool_calls that have no matching tool result
    - Validates role is one of: system, user, assistant, tool, function, developer
    """
    # Collect all tool_call IDs from assistant messages
    call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                tc_id = tc.get("id") or tc.get("call_id", "")
                if tc_id:
                    call_ids.add(tc_id)

    # Collect all result IDs from tool messages
    result_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id:
                result_ids.add(tc_id)

    # Drop orphaned tool results and messages with invalid roles
    orphaned_results = result_ids - call_ids
    cleaned: list[dict] = []
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id and tc_id in orphaned_results:
                continue  # orphaned -- drop
        # Validate role
        if msg.get("role") not in ("system", "user", "assistant", "tool", "function", "developer"):
            logger.debug(
                "sanitize_tool_pairs: dropping message with invalid role %r",
                msg.get("role"),
            )
            continue
        cleaned.append(msg)

    if orphaned_results:
        logger.debug(
            "sanitize_tool_pairs: removed %d orphaned tool result(s)",
            len(orphaned_results),
        )

    # Inject stub results for unmatched tool_calls -- placed immediately
    # after their corresponding assistant message.
    # Recalculate result_ids from the cleaned list (some may have been dropped).
    cleaned_result_ids: set[str] = set()
    for msg in cleaned:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id:
                cleaned_result_ids.add(tc_id)

    missing_ids = call_ids - cleaned_result_ids
    if missing_ids:
        patched: list[dict] = []
        for msg in cleaned:
            patched.append(msg)
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls", []):
                    tc_id = tc.get("id") or tc.get("call_id", "")
                    if tc_id in missing_ids:
                        patched.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "[Result unavailable — see context summary above]",
                        })
        cleaned = patched
        logger.debug(
            "sanitize_tool_pairs: added %d stub tool result(s)",
            len(missing_ids),
        )

    return cleaned


# ---------------------------------------------------------------------------
# Budget warning stripping
# ---------------------------------------------------------------------------

_BUDGET_WARNING_RE = re.compile(
    r'\[BUDGET(?:\s+WARNING)?:.*?\]',
    re.DOTALL,
)


def strip_budget_warnings(messages: list[dict]) -> list[dict]:
    """Strip budget warnings from tool results in replayed messages.

    Budget warnings are turn-scoped signals. When messages are replayed
    (e.g., on ``wake()`` after crash recovery), old warnings must be removed
    to prevent confusing the model.

    Handles both plain-text budget warnings and JSON ``_budget_warning`` keys.
    """
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        if "_budget_warning" not in content and "[BUDGET" not in content:
            continue

        # Try JSON first (the common case: _budget_warning key in a dict)
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "_budget_warning" in parsed:
                del parsed["_budget_warning"]
                msg["content"] = json.dumps(parsed, ensure_ascii=False)
                continue
        except (json.JSONDecodeError, TypeError):
            pass

        # Fallback: strip the text pattern from plain-text tool results
        cleaned = _BUDGET_WARNING_RE.sub("", content).strip()
        if cleaned != content:
            msg["content"] = cleaned

    return messages


# ---------------------------------------------------------------------------
# Duplicate tool call deduplication
# ---------------------------------------------------------------------------


def deduplicate_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Remove duplicate (name, arguments) pairs from a tool call batch.

    Only the first occurrence of each unique (name, args) pair is kept.
    """
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        key = (fn.get("name", ""), fn.get("arguments", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(tc)

    if len(unique) < len(tool_calls):
        logger.warning(
            "Deduplicated %d duplicate tool calls", len(tool_calls) - len(unique),
        )
    return unique


# ---------------------------------------------------------------------------
# Delegate task capping
# ---------------------------------------------------------------------------

_MAX_DELEGATE_CALLS: int = 5


def cap_delegate_calls(
    tool_calls: list[dict],
    max_delegates: int = _MAX_DELEGATE_CALLS,
) -> list[dict]:
    """Cap the number of delegate_task calls in a single turn.

    Preserves all non-delegate calls. Truncates excess delegates.
    """
    delegates: list[dict] = []
    others: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        if fn.get("name") == "delegate_task":
            delegates.append(tc)
        else:
            others.append(tc)

    if len(delegates) > max_delegates:
        logger.warning(
            "Capping delegate_task calls from %d to %d",
            len(delegates), max_delegates,
        )
        delegates = delegates[:max_delegates]

    return others + delegates
