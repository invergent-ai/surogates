from __future__ import annotations

"""Tool result persistence -- preserves large outputs instead of truncating.

Defense against context-window overflow operates at three levels:

1. **Per-tool output cap** (inside each tool): Tools like search_files
   pre-truncate their own output before returning. This is the first line
   of defense and the only one the tool author controls.

2. **Per-result persistence** (maybe_persist_tool_result): After a tool
   returns, if its output exceeds the tool's registered threshold
   (config.resolve_threshold), the full output is written out and the
   in-context content is replaced with a preview + file path reference,
   which the model can read_file later.  The write has to target whichever
   filesystem read_file runs on: the session workspace via the sandbox
   when tools execute in a pod, this process's /tmp when they do not.

3. **Per-turn aggregate budget** (enforce_turn_budget): After all tool
   results in a single assistant turn are collected, if the total exceeds
   turn_budget (200K), the largest non-persisted results are spilled to
   disk until the aggregate is under budget. This catches cases where many
   medium-sized results combine to overflow context.
"""

import json
import logging
import os
import uuid
from typing import Any, Awaitable, Callable

from surogates.tools.utils.budget_config import (
    DEFAULT_PREVIEW_SIZE_CHARS,
    BudgetConfig,
    DEFAULT_BUDGET,
)

logger = logging.getLogger(__name__)
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

# Where a spilled result lands when the harness process is also the one
# that will read it back (no sandbox pool).
STORAGE_DIR = "/tmp/surogates-results"

# Where it lands when tools execute in a sandbox pod.  ``read_file`` runs
# there, not here, and refuses any path outside the session workspace --
# so the spill has to be workspace-relative and written through the same
# sandbox, or the model is handed a path it cannot open.
WORKSPACE_STORAGE_DIR = ".surogates-results"

_BUDGET_TOOL_NAME = "__budget_enforcement__"

# (path, content) -> wrote successfully
ResultWriter = Callable[[str, str], Awaitable[bool]]


def make_sandbox_writer(sandbox_pool: Any, sandbox_owner: str) -> ResultWriter:
    """Return a writer that spills through *sandbox_pool* into the workspace."""

    async def _write(file_path: str, content: str) -> bool:
        try:
            output = await sandbox_pool.execute(
                sandbox_owner,
                "write_file",
                json.dumps({"path": file_path, "content": content}),
            )
        except Exception as exc:
            logger.warning("Sandbox spill write failed for %s: %s", file_path, exc)
            return False
        # The sandbox reports tool errors in-band as ``{"error": ...}``
        # rather than raising, so a write can "succeed" and still be a
        # refusal (protected path, blind overwrite).
        try:
            payload = json.loads(output)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            logger.warning(
                "Sandbox spill write rejected for %s: %s",
                file_path, str(payload["error"])[:200],
            )
            return False
        return True

    return _write


def generate_preview(content: str, max_chars: int = DEFAULT_PREVIEW_SIZE_CHARS) -> tuple[str, bool]:
    """Truncate at last newline within max_chars. Returns (preview, has_more)."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl + 1]
    return truncated, True


def _write_to_disk(content: str, file_path: str) -> bool:
    """Write content to a local file path. Returns True on success.

    Only correct when tools execute in this process.  When they run in a
    sandbox pod the spill must go through :func:`make_sandbox_writer`
    instead -- a file written here is invisible there.
    """
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return True
    except OSError as exc:
        logger.warning("Disk write failed for %s: %s", file_path, exc)
        return False


def _build_persisted_message(
    preview: str,
    has_more: bool,
    original_size: int,
    file_path: str,
) -> str:
    """Build the <persisted-output> replacement block."""
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"This tool result was too large ({original_size:,} characters, {size_str}).\n"
    msg += f"Full output saved to: {file_path}\n"
    msg += "Use the read_file tool with offset and limit to access specific sections of this output.\n\n"
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


async def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    config: BudgetConfig = DEFAULT_BUDGET,
    threshold: int | float | None = None,
    writer: ResultWriter | None = None,
) -> str:
    """Layer 2: persist oversized result, return preview + path.

    The spill has to land wherever ``read_file`` will run.  With a
    *writer* (sandboxed tools) that means a workspace-relative path
    written through the sandbox; without one, the harness process reads
    its own filesystem.  Falls back to inline truncation if the write
    fails.

    Args:
        content: Raw tool result string.
        tool_name: Name of the tool (used for threshold lookup).
        tool_use_id: Unique ID for this tool call (used as filename).
        config: BudgetConfig controlling thresholds and preview size.
        threshold: Explicit override; takes precedence over config resolution.
        writer: Sandbox writer from :func:`make_sandbox_writer`, when tools
            execute in a sandbox pod.

    Returns:
        Original content if small, or <persisted-output> replacement.
    """
    effective_threshold = threshold if threshold is not None else config.resolve_threshold(tool_name)

    if effective_threshold == float("inf"):
        return content

    if len(content) <= effective_threshold:
        return content

    preview, has_more = generate_preview(content, max_chars=config.preview_size)

    if writer is None:
        file_path = f"{STORAGE_DIR}/{tool_use_id}.txt"
        persisted = _write_to_disk(content, file_path)
    else:
        file_path = f"{WORKSPACE_STORAGE_DIR}/{tool_use_id}.txt"
        persisted = await writer(file_path, content)

    if persisted:
        logger.info(
            "Persisted large tool result: %s (%s, %d chars -> %s)",
            tool_name, tool_use_id, len(content), file_path,
        )
        return _build_persisted_message(preview, has_more, len(content), file_path)

    logger.info(
        "Inline-truncating large tool result: %s (%d chars, disk write failed)",
        tool_name, len(content),
    )
    return (
        f"{preview}\n\n"
        f"[Truncated: tool response was {len(content):,} chars. "
        f"Full output could not be saved to disk.]"
    )


async def enforce_turn_budget(
    tool_messages: list[dict],
    config: BudgetConfig = DEFAULT_BUDGET,
    writer: ResultWriter | None = None,
) -> list[dict]:
    """Layer 3: enforce aggregate budget across all tool results in a turn.

    If total chars exceed budget, persist the largest non-persisted results
    first (via disk write) until under budget. Already-persisted results
    are skipped.

    Mutates the list in-place and returns it.
    """
    candidates = []
    total_size = 0
    for i, msg in enumerate(tool_messages):
        content = msg.get("content", "")
        size = len(content)
        total_size += size
        if PERSISTED_OUTPUT_TAG not in content:
            candidates.append((i, size))

    if total_size <= config.turn_budget:
        return tool_messages

    candidates.sort(key=lambda x: x[1], reverse=True)

    for idx, size in candidates:
        if total_size <= config.turn_budget:
            break
        msg = tool_messages[idx]
        content = msg["content"]
        # The fallback id must stay unique across turns: the sandbox
        # ``write_file`` refuses to overwrite a file it has not read.
        tool_use_id = msg.get(
            "tool_call_id", f"budget_{idx}_{uuid.uuid4().hex[:8]}",
        )

        replacement = await maybe_persist_tool_result(
            content=content,
            tool_name=_BUDGET_TOOL_NAME,
            tool_use_id=tool_use_id,
            config=config,
            threshold=0,
            writer=writer,
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            tool_messages[idx]["content"] = replacement
            logger.info(
                "Budget enforcement: persisted tool result %s (%d chars)",
                tool_use_id, size,
            )

    return tool_messages
