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
import contextlib
import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from surogates.harness.agent_resolver import (
    apply_agent_def_to_session,
    resolve_agent_def,
)
from surogates.harness.connection_health import cleanup_dead_connections
from surogates.harness.cost_tracker import SessionCostTracker
from surogates.harness.credentials import CredentialPool
from surogates.harness.error_classify import classify_harness_error
from surogates.harness.expert_routing import (
    build_thinking_extra_body,
    classify_hard_task_async,
    merge_extra_body,
    model_supports_thinking_toggle,
    parse_next_action_complexity,
)
from surogates.harness.llm_call import apply_developer_role, call_llm_with_retry
from surogates.harness.self_discover import (
    SCAFFOLD_CATEGORIES,
    build_scaffold,
    format_scaffold_for_injection,
)
from surogates.harness.message_utils import (
    coerce_message_content,
    make_skipped_tool_result,
    message_to_dict,
)
from surogates.harness.outcomes import (
    DEFAULT_MAX_ITERATIONS,
    OutcomeState,
    apply_evaluation,
    build_evaluator_messages,
    parse_goal_command,
    parse_outcome_evaluation,
    start_outcome,
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
    strip_budget_warnings,
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
    from surogates.session.models import Event, Session, SessionLease
    from surogates.session.store import SessionStore
    from surogates.tools.registry import ToolRegistry
    from surogates.tenant.context import TenantContext

logger = logging.getLogger(__name__)


# Default reasoning-control knobs applied to every main-loop call when the
# model supports the thinking toggle.  Sessions can override via
# ``session.config["thinking_budget"]`` / ``session.config["preserve_thinking"]``.
# ``4096`` keeps reasoning roomy enough for hard tasks while staying well
# under the 16 KB-char runaway threshold in ``llm_call.py``.
# ``preserve_thinking=True`` matches the existing behaviour of replaying
# ``reasoning_content`` across turns for OpenRouter/Moonshot.
DEFAULT_THINKING_BUDGET: int = 4096
DEFAULT_PRESERVE_THINKING: bool = True


async def maybe_inject_browser_pause(
    *,
    session: Any,
    browser_control: "BrowserControlStore | None",
) -> str | None:
    """Return a one-time pause notice while the user holds browser control."""
    if browser_control is None:
        return None

    holder = await browser_control.held_by(str(session.id))
    config = session.config if isinstance(getattr(session, "config", None), dict) else {}
    if not isinstance(getattr(session, "config", None), dict):
        with contextlib.suppress(Exception):
            session.config = config

    if holder is not None:
        if config.get("browser_pause_msg_injected"):
            return None
        config["browser_pause_msg_injected"] = True
        return (
            "The user has taken control of the browser. Wait for them to "
            "finish before continuing; browser_* tool calls will return "
            "paused_by_user until they release control."
        )

    if config.get("browser_pause_msg_injected"):
        config["browser_pause_msg_injected"] = False
    return None


def _initial_system_message(system_prompt: str, browser_pause_notice: str | None) -> dict[str, str]:
    """Build the first API message, folding transient system notices into it."""

    if not browser_pause_notice:
        return {"role": "system", "content": system_prompt}
    return {
        "role": "system",
        "content": f"{system_prompt}\n\n{browser_pause_notice}",
    }


def _view_context_note_from_metadata(metadata: Any) -> str | None:
    """Pure helper: render the view-context note from a metadata dict.

    Returns ``None`` when *metadata* is missing, not a dict, lacks
    ``view_context``, or the inner ``view_context`` payload is malformed
    (not a dict, missing ``kind``/``id``).
    """
    if not isinstance(metadata, dict):
        return None
    view_context = metadata.get("view_context")
    if not isinstance(view_context, dict):
        return None
    kind = view_context.get("kind")
    target_id = view_context.get("id")
    if not kind or not target_id:
        return None
    note = f"The user is currently viewing **{kind}** {target_id}"
    name = view_context.get("name")
    if name:
        note += f" ({name})"
    note += "."
    return note


def _view_context_note(events: list[Any]) -> str | None:
    """Return the view-context note for the most recent user.message event.

    Kept for tests and external callers; the main loop folds the note
    into each user message's content via
    :func:`_view_context_note_from_metadata` during
    :meth:`AgentHarness._rebuild_messages`, so this helper is no longer
    used for ephemeral mid-array insertion.
    """
    for event in reversed(events):
        event_type = event.type
        type_value = event_type.value if hasattr(event_type, "value") else event_type
        if type_value != EventType.USER_MESSAGE.value:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        return _view_context_note_from_metadata(data.get("metadata"))
    return None


def _format_bytes(n: int) -> str:
    """Render byte counts as B / KB / MB / GB with one decimal place."""
    for unit, divisor in (
        ("GB", 1_000_000_000),
        ("MB", 1_000_000),
        ("KB", 1_000),
    ):
        if n >= divisor:
            return f"{n / divisor:.1f} {unit}"
    return f"{n} B"


_ATTACHMENT_SKIP_HINTS: dict[str, str] = {
    "parse_error": (
        "try read_file with pdftotext/pandoc fallbacks"
    ),
    "parse_timeout": (
        "the parser hit its wall-clock cap; try read_file with a narrower offset/limit"
    ),
    "decode_error": (
        "the file is not UTF-8; try read_file which has full BOM detection"
    ),
    "oversize_output": (
        "the parsed content exceeded the inline cap; use read_file with"
        " offset/limit to paginate"
    ),
    "empty_output": (
        "the parser produced no text; the file may be a scan — try the"
        " ocr-and-documents skill"
    ),
    "total_budget_exceeded": (
        "earlier attachments already filled the inline-budget; use read_file"
        " when you actually need this file's content"
    ),
}


def _latest_user_event_text(events: list[Any]) -> str:
    """Return the raw text of the latest ``USER_MESSAGE`` event.

    The raw text is what the user actually typed -- without the
    attachment-note / view-context-note prefixes that
    :meth:`AgentHarness._rebuild_messages` folds into the rebuilt user
    content for the LLM call.

    Slash-command dispatch (``/compress``, ``/clear``, ``/goal``,
    ``/mission``, ``/loop``, ``/<skill>``) inspects this value: looking
    at the rebuilt message instead would push the leading ``/`` off the
    start whenever the message carries a path-only attachment (e.g. a
    PDF too large to inline) and silently disable dispatch.

    Returns ``""`` when no ``USER_MESSAGE`` event exists or the content
    is missing / malformed.
    """
    for event in reversed(events):
        event_type = event.type
        type_value = (
            event_type.value if hasattr(event_type, "value") else event_type
        )
        if type_value != EventType.USER_MESSAGE.value:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        raw = data.get("content", "")
        if isinstance(raw, list):
            raw = next(
                (
                    p["text"] for p in raw
                    if isinstance(p, dict) and p.get("type") == "text"
                ),
                "",
            )
        return (raw or "").strip()
    return ""


def _attachments_note(events: list[Any]) -> str | None:
    """Return a per-turn system note describing path-only attachments.

    Reads ``data.attachments`` on the most recent ``user.message``
    event.  Any attachment whose ``inlined_text`` is already populated
    is omitted from this note (the content lives in the user message
    text via :func:`_render_inlined_attachments`).  Attachments that
    were candidates for inline but skipped get an annotated entry that
    names the ``inline_skip_reason`` so the agent knows why it needs to
    fall back to ``read_file``.

    The lookup is read-only and never raises — malformed payloads
    (e.g. ``attachments`` not a list, items not dicts) yield ``None``
    so the LLM call proceeds unchanged.
    """
    for event in reversed(events):
        event_type = event.type
        type_value = (
            event_type.value if hasattr(event_type, "value") else event_type
        )
        if type_value != EventType.USER_MESSAGE.value:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        return _attachments_note_from_data(data)
    return None


def _attachments_note_from_data(data: Any) -> str | None:
    """Pure helper: render the attachments note from a user.message data dict."""
    if not isinstance(data, dict):
        return None
    attachments = data.get("attachments")
    if not isinstance(attachments, list) or not attachments:
        return None

    lines = [
        "The user attached the following files to this message. They are"
        " available in the workspace and you can read them with your file"
        " tools:",
    ]
    for item in attachments:
        if not isinstance(item, dict):
            continue
        if item.get("inlined_text"):
            # Content already in the user message text via
            # _render_inlined_attachments — don't double-list it here.
            continue
        path = item.get("path")
        filename = item.get("filename")
        if not path or not filename:
            continue
        mime = item.get("mime_type") or "application/octet-stream"
        raw_size = item.get("size")
        if isinstance(raw_size, (int, float)) and raw_size >= 0:
            size_str = _format_bytes(int(raw_size))
        else:
            size_str = "unknown size"
        line = f"- {path} ({mime}, {size_str}) — \"{filename}\""
        skip_reason = item.get("inline_skip_reason")
        if skip_reason:
            hint = _ATTACHMENT_SKIP_HINTS.get(skip_reason, "use read_file")
            line += f" (inline skipped: {skip_reason} — {hint})"
        lines.append(line)

    if len(lines) == 1:
        # All items malformed or all inlined.
        return None
    return "\n".join(lines)


def _render_inlined_attachments(
    content: str,
    attachments: list[Any] | None,
) -> str:
    """Append one fenced block per inlined attachment to ``content``.

    ``attachments`` is the persisted ``data["attachments"]`` payload
    from a ``user.message`` event.  Each item with a non-empty
    ``inlined_text`` field becomes a fenced block at the end of the
    returned string.  Items without ``inlined_text`` (path-only,
    inline-skipped, or unsupported) are ignored here -- the system
    ``_attachments_note`` surface covers them.
    """
    if not attachments:
        return content
    blocks: list[str] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        inlined = item.get("inlined_text")
        if not inlined:
            continue
        kind = item.get("inlined_render_kind") or "text"
        path = item.get("path") or ""
        filename = item.get("filename") or path
        header = f"**Attachment: {filename}**"
        if kind == "markdown":
            subtitle = (
                "*(parsed via liteparse — to re-read or "
                f"paginate, use `read_file(\"{path}\")`)*"
            )
            block = f"---\n{header}\n{subtitle}\n\n{inlined}\n---"
        else:
            block = f"---\n{header}\n\n{inlined}\n---"
        blocks.append(block)
    if not blocks:
        return content
    return content + "\n\n" + "\n\n".join(blocks)


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


def _last_assistant_message_excerpt(
    messages: list[dict[str, Any]],
    limit: int = 500,
) -> str:
    """Return the latest assistant text as a compact summary."""

    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", ""))
                for part in content
                if (
                    isinstance(part, dict)
                    and part.get("type") in {"text", "output_text"}
                )
            )
        text = str(content).strip()
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."
    return ""


def _prior_next_action_complexity(
    messages: list[dict[str, Any]],
) -> str | None:
    """Return the complexity declared by the latest assistant turn, or ``None``.

    Reads the most recent assistant message's full text (not the
    truncated excerpt — the ``<next_action>`` footer can land anywhere
    in a long answer) and parses the ``<next_action complexity="...">``
    block via :func:`parse_next_action_complexity`.

    Returns ``None`` when there is no prior assistant turn (turn 1 of
    a session) OR when the model failed to emit the directive (older
    sessions, prompt drift).  Callers treat ``None`` as "no signal —
    fall through to the classifier" and ``low``/``medium``/``high`` as
    the model's self-reported intent.
    """
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            text = " ".join(
                str(part.get("text", ""))
                for part in content
                if (
                    isinstance(part, dict)
                    and part.get("type") in {"text", "output_text"}
                )
            )
        else:
            text = str(content)
        return parse_next_action_complexity(text)
    return None


def _coerce_modified_to_datetime(raw: Any) -> "datetime | None":
    """Normalize a storage backend's ``modified`` field to ``datetime``.

    LocalBackend returns a POSIX float (``st_mtime``); S3Backend
    returns the boto3 ``LastModified`` ``datetime`` directly. Anything
    else is treated as unparseable and yields ``None`` so the caller
    skips the entry rather than crashing.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _coerce_tool_args(raw: Any) -> dict[str, Any]:
    """Best-effort coercion of a TOOL_CALL ``arguments`` field to a dict.

    Different tool emitters store ``arguments`` either as a JSON string
    (OpenAI convention) or as a pre-parsed dict.  Anything else is
    treated as opaque and yields an empty dict so candidate-artifact
    collection can keep going.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _latest_user_message_text(
    messages: list[dict[str, Any]],
    limit: int = 1000,
) -> str:
    """Extract the latest user message's text content, capped at ``limit``.

    Used by the turn-summary path so the summarizer sees what the user
    was asking for.  Mirrors the content-coercion logic in
    :func:`_last_assistant_message_excerpt` so multimodal user messages
    yield their text parts.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", ""))
                for part in content
                if (
                    isinstance(part, dict)
                    and part.get("type") in {"text", "output_text"}
                )
            )
        text = str(content).strip()
        return text[:limit] if limit and len(text) > limit else text
    return ""


def _seconds_since(value: Any) -> int:
    if not isinstance(value, datetime):
        return 0
    created_at = value
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(
        0,
        int((datetime.now(timezone.utc) - created_at).total_seconds()),
    )


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _should_notify_parent_on_completion(session: Any) -> bool:
    return session.parent_id is not None and session.channel != "scheduled"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default TTL (seconds) for lease acquisition and renewal.
_LEASE_TTL_SECONDS: int = 60

# Renewal cadence.  Time-based (not iteration-based) so a long-running
# iteration (e.g. slow LLM call, streaming fallback) cannot let the lease
# expire and get stolen by another worker.  Must be well under
# ``_LEASE_TTL_SECONDS`` so a single missed tick still leaves the lease alive.
_LEASE_RENEWAL_INTERVAL_SECONDS: float = 20.0

# Upper bound on how long ``wake()`` will wait for fire-and-forget background
# tasks (e.g. title generation) before releasing the session lease.  The drain
# is best-effort: anything still pending after this is cancelled so the worker
# can release the lease promptly.  Slightly longer than the title generator's
# own 30 s timeout to give it room to finish cleanly.
_BACKGROUND_DRAIN_TIMEOUT_SECONDS: float = 35.0

# Retry / resilience constants
_MAX_LENGTH_CONTINUATIONS: int = 3
_MAX_CONSECUTIVE_INVALID_TOOL_CALLS: int = 3
_MAX_EMPTY_RESPONSE_RETRIES: int = 3
_LENGTH_CONTINUATION_PROMPT: str = (
    "[System: Your previous response was truncated by the output "
    "length limit. Continue exactly where you left off. Do not "
    "restart or repeat prior text. Finish the answer directly.]"
)
_EMPTY_RESPONSE_NUDGE: str = (
    "[System: Your previous response was empty. Re-read the user's "
    "request and act now. If the user asked for a visual or rendered "
    "output (SVG, HTML, chart, table, markdown document), invoke "
    "create_artifact — do NOT paste the content as a code fence.]"
)
_USER_ACTION_RESCUE_SYSTEM: str = (
    "You are a strict routing judge for an agent harness. Decide whether "
    "the assistant's draft response is ending the turn while it is "
    "genuinely blocked on user input or user action. Default to "
    "action_kind='none' unless the assistant has clearly stopped a "
    "concrete in-progress task that cannot proceed without specific input "
    "from the user. When in doubt, choose 'none'. "
    "Use action_kind='ask_user_question' ONLY when the assistant has paused a "
    "specific in-progress task and is asking a specific question whose "
    "answer is required to continue. Set 'question' to that concise "
    "question. "
    "Use action_kind='action_required' when the assistant has paused for "
    "the user to perform a UI action it cannot do itself: login, MFA, "
    "OAuth, CAPTCHA, consent screen, file picker, or browser approval. "
    "Set 'instructions' to what the user must do and 'target' to 'browser' "
    "or 'session'. "
    "Use action_kind='none' for: completed work with polite closings ('let "
    "me know if you need anything else', 'feel free to ask'), status "
    "reports, summaries, recaps of what was done, suggestions, optional "
    "follow-ups ('I can also do X if you want'), rhetorical questions, "
    "offers to continue, and any case where the assistant could simply "
    "stop and wait without losing progress. A polite invitation to "
    "continue is NOT a blocker. Asking 'anything else?' is NOT a blocker. "
    "Return only JSON with keys: action_kind string, reason string, "
    "question string, title string, instructions string, context string, "
    "action_type string, target string."
)
_DYNAMIC_LOOP_EXCLUDED_TOOLS: frozenset[str] = frozenset({
    "cron_create",
    "cron_delete",
    "cron_list",
})


class _UserActionRescueDecision(BaseModel):
    action_kind: str = Field(
        default="none",
        description="One of none, ask_user_question, or action_required.",
    )
    needs_ask_user_question: bool = Field(
        default=False,
        description="Whether the assistant draft is blocked on user input.",
    )
    reason: str = Field(
        description="Short machine-readable reason for the routing decision.",
    )
    question: str = Field(
        default="",
        description="Concise question to ask via ask_user_question when blocked.",
    )
    context: str = Field(
        default="",
        description="Short context explaining why the user input is needed.",
    )
    title: str = Field(default="", description="Short inbox item title.")
    instructions: str = Field(
        default="",
        description="Instructions for a user action_required inbox item.",
    )
    action_type: str = Field(
        default="manual",
        description="Machine-readable action type such as browser or approval.",
    )
    target: str = Field(
        default="session",
        description="Where the user should perform the action.",
    )


async def _generate_user_action_rescue_structured(
    *,
    llm_client: Any,
    model: str,
    messages: list[dict[str, str]],
) -> dict[str, Any] | None:
    """Return a typed user-action rescue judge decision when supported."""
    decision = await generate_structured(
        llm_client=llm_client,
        model=model,
        messages=messages,
        output_model=_UserActionRescueDecision,
        max_tokens=300,
        temperature=0,
    )
    return decision.model_dump() if decision is not None else None

# Fenced-block kinds the post-response promoter is willing to turn into
# an artifact when the model emits one in place of a ``create_artifact``
# call.  Keys are the fence language tags; values map to the artifact
# ``kind`` and the spec key that carries the raw body.
_PROMOTABLE_FENCES: dict[str, tuple[str, str]] = {
    "svg": ("svg", "svg"),
    "html": ("html", "html"),
}

# Precompiled regex that matches ``` + language-tag + body + ``` .  The
# (?s) flag lets ``.`` match newlines inside the body.  Only matches
# fences starting at line-begin to avoid misfires on inline backticks.
_FENCE_RE = re.compile(
    r"(?ms)^```([a-zA-Z0-9_-]+)\s*\n(.*?)^```\s*$"
)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_HARNESS_CONTROL_PENDING_EVENT_TYPES = frozenset({
    EventType.HARNESS_RECOVERED.value,
    EventType.HARNESS_WAKE.value,
})


def _actionable_pending_events(events: list[Any], cursor: int) -> list[Any]:
    """Return post-cursor events that should start harness work."""
    pending = []
    for event in events:
        event_type = (
            event.type.value
            if isinstance(event.type, EventType)
            else str(event.type)
        )
        if (
            event.id is not None
            and event.id > cursor
            and event_type not in _HARNESS_CONTROL_PENDING_EVENT_TYPES
        ):
            pending.append(event)
    return pending


def _slash_loop_already_processed(events: list[Any]) -> bool:
    """Return True if the latest ``/loop`` user message has already been answered.

    ``_handle_loop_command`` emits exactly one ``LLM_RESPONSE`` via
    ``_emit_loop_response`` per run, so an ``LLM_RESPONSE`` whose id sits
    after the latest ``USER_MESSAGE`` proves the command has already been
    processed.  Used to skip duplicate schedule creation when the harness
    wakes a second time on the same ``/loop`` message — e.g. when the
    orphan sweeper re-enqueues a finished session.
    """
    latest_user_msg_id: int | None = None
    for event in events:
        event_type = (
            event.type.value
            if isinstance(event.type, EventType)
            else str(event.type)
        )
        if event_type == EventType.USER_MESSAGE.value and event.id is not None:
            if latest_user_msg_id is None or event.id > latest_user_msg_id:
                latest_user_msg_id = event.id
    if latest_user_msg_id is None:
        return False
    for event in events:
        event_type = (
            event.type.value
            if isinstance(event.type, EventType)
            else str(event.type)
        )
        if (
            event_type == EventType.LLM_RESPONSE.value
            and event.id is not None
            and event.id > latest_user_msg_id
        ):
            return True
    return False


def _derive_artifact_name(kind: str, messages: list[dict]) -> str:
    """Pick a human-readable name for an auto-promoted artifact.

    Uses the most recent user message's first line (trimmed to a
    reasonable length) so the artifact header says something like
    "Draw a minimal SVG logo…" instead of a generic "SVG artifact".
    Falls back to a kind-based default when no user message is
    available or the extract is empty.
    """
    fallback = {
        "svg": "SVG artifact",
        "html": "HTML preview",
    }.get(kind, "Artifact")

    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content") or ""
        if not isinstance(content, str):
            continue
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        # Strip surrounding quotes the frontend sometimes inherits from
        # copy-pasted prompts.
        first_line = first_line.strip(' "\'')
        if first_line:
            return first_line[:80]
        break
    return fallback


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


def build_partial_tool_call_recovery_results(tool_calls: list[dict]) -> list[dict]:
    """Build model-visible tool results for truncated tool-call arguments."""
    results: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        tool_name = fn.get("name", "")
        results.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(
                    {
                        "error": (
                            "Partial tool call arguments detected. The provider "
                            "ended the response before the JSON arguments were "
                            "complete. Retry this tool call with complete JSON."
                        ),
                        "tool": tool_name,
                    },
                    ensure_ascii=False,
                ),
            }
        )
    return results


def _configured_vision_model() -> str:
    from surogates.config import load_settings

    return str(getattr(load_settings().llm, "vision_model", "") or "").strip()


def _message_has_image_blocks(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "image_url"
        for part in content
    )


def _messages_have_image_blocks(messages: list[dict]) -> bool:
    return any(_message_has_image_blocks(message) for message in messages)


def _strip_image_blocks_from_message(message: dict[str, Any]) -> None:
    content = message.get("content")
    if not isinstance(content, list):
        return
    text_parts = [
        part
        for part in content
        if not (isinstance(part, dict) and part.get("type") == "image_url")
    ]
    collapsed = _collapse_text_parts(text_parts)
    if collapsed is not None:
        message["content"] = collapsed
    else:
        message["content"] = text_parts


def _strip_image_blocks_from_messages(messages: list[dict]) -> None:
    for message in messages:
        _strip_image_blocks_from_message(message)


def _extract_response_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = message_to_dict(message).get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text.strip())
            elif isinstance(item, str):
                parts.append(item.strip())
        return "\n".join(part for part in parts if part)
    return str(content).strip() if content is not None else ""


def _text_context_for_image_description(content: list[Any]) -> str:
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def _collapse_text_parts(parts: list[dict[str, Any]]) -> str | None:
    if not parts:
        return ""
    if not all(isinstance(part, dict) and part.get("type") == "text" for part in parts):
        return None
    return "\n\n".join(
        str(part.get("text") or "").strip()
        for part in parts
        if str(part.get("text") or "").strip()
    )


async def _describe_image_part(
    *,
    llm_client: Any,
    vision_model: str,
    image_part: dict[str, Any],
    text_context: str,
) -> str:
    prompt = (
        "Describe this image for a text-only language model that will answer "
        "the user's prompt. Include visible text, layout, objects, relevant "
        "details, and uncertainty. Do not answer the user's task directly."
    )
    if text_context:
        prompt = f"{prompt}\n\nUser text around the image:\n{text_context}"
    response = await llm_client.chat.completions.create(
        model=vision_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": dict(image_part.get("image_url") or {}),
                    },
                ],
            }
        ],
        temperature=0,
    )
    return _extract_response_text(response)


async def _replace_image_blocks_with_descriptions(
    messages: list[dict],
    *,
    llm_client: Any,
    vision_model: str,
) -> None:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text_context = _text_context_for_image_description(content)
        replacement_parts: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") != "image_url":
                replacement_parts.append(part)
                continue
            try:
                description = await _describe_image_part(
                    llm_client=llm_client,
                    vision_model=vision_model,
                    image_part=part,
                    text_context=text_context,
                )
            except Exception as exc:
                logger.warning(
                    "Vision preflight failed for non-vision model; stripping image: %s",
                    exc,
                )
                continue
            if description:
                replacement_parts.append({
                    "type": "text",
                    "text": (
                        f"[Image description from {vision_model}]\n"
                        f"{description}"
                    ),
                })
        collapsed = _collapse_text_parts(replacement_parts)
        if collapsed is not None:
            message["content"] = collapsed
        else:
            message["content"] = replacement_parts


async def _prepare_messages_for_model_vision_support(
    messages: list[dict],
    *,
    model_id: str,
    llm_client: Any,
    vision_client: Any | None = None,
    vision_model_override: str = "",
) -> list[dict]:
    from surogates.harness.model_metadata import get_model_info

    model_info = get_model_info(model_id)
    has_images = _messages_have_image_blocks(messages)
    if has_images:
        logger.info(
            "Vision gate: model=%s info=%s supports_vision=%s",
            model_id,
            model_info is not None,
            model_info.supports_vision if model_info else "N/A",
        )
    if model_info is None or model_info.supports_vision:
        return messages

    vision_model = vision_model_override or _configured_vision_model()
    if vision_model:
        await _replace_image_blocks_with_descriptions(
            messages,
            llm_client=vision_client or llm_client,
            vision_model=vision_model,
        )
    else:
        _strip_image_blocks_from_messages(messages)
    return messages


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
        browser_pool: BrowserPool | None = None,
        browser_control: BrowserControlStore | None = None,
        storage: Any | None = None,
        checkpoints_enabled: bool = False,
        saga_enabled: bool = False,
        saga_settings: Any | None = None,
        api_client: Any | None = None,
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
        turn_summarizer: Any | None = None,
        bundle: Any | None = None,
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
        self._browser_pool: BrowserPool | None = browser_pool
        self._browser_control: BrowserControlStore | None = browser_control
        self._storage = storage
        self._api_client = api_client
        self._session_factory = session_factory
        # Per-session Hub-backed bundle shared by every catalogue
        # load inside the harness (sub-agent resolver, prompt
        # builder, future skill staging).  ``None`` for agents
        # whose first publish hasn't landed yet.
        self._bundle: Any | None = bundle

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

        # When set, the thinking gate forces enable_thinking=False for
        # every iteration in the current user turn, overriding the
        # classifier.  Set by the LLM-call retry path when a
        # runaway-reasoning stream was cancelled and re-issued with
        # thinking disabled (the same task would runaway again otherwise).
        # Cleared at the start of each new user turn.
        self._thinking_disabled_for_turn: bool = False

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

    async def _should_abort_before_llm_response(
        self,
        session: Session,
        llm_request_event_id: int,
    ) -> bool:
        """Return True if this iteration must drop its buffered response.

        Detects two races that can otherwise leak a buffered response into
        the wrong turn:

        - An explicit interrupt was raised (lease loss, channel pause,
          new-message-while-busy nudge from the API).
        - A new ``user.message`` was appended after this iteration's own
          ``llm.request`` event — meaning the user moved on to a new turn
          while the stream or end-of-turn judge was still in flight.

        When a stale user message is detected the interrupt flag is also
        set so the cleanup path runs identically for both causes.
        """
        if self._check_interrupt():
            return True
        newer = await self._store.get_events(
            session.id,
            after=llm_request_event_id,
            types=[EventType.USER_MESSAGE],
            limit=1,
        )
        if newer:
            logger.info(
                "Session %s: dropping stale buffered response — user "
                "message %s arrived after llm.request %s",
                session.id,
                newer[0].id,
                llm_request_event_id,
            )
            self.interrupt("stale response — newer user message in log")
            return True
        return False

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
    # Entry point
    # ------------------------------------------------------------------

    async def wake(self, session_id: UUID) -> str | None:
        """Entry point.  Acquire lease, replay events, run loop, release lease."""
        from surogates.trace import new_span

        # Create a child span for this wake cycle (parent was set by the
        # orchestrator or API middleware).
        new_span()

        # Skip thinking-gate + SELF-DISCOVER scaffolding on turns where
        # the user explicitly invoked a slash-skill: the skill body is
        # already the planning structure, so layering an additional
        # classifier-driven scaffold on top is dead weight that adds
        # ~40s of LLM round-trips on cold sessions for no quality
        # gain.  Flag is set below if expand_slash_skill succeeds, and
        # consumed once by ``_run_iteration`` before the first LLM call.
        self._skip_pre_llm_scaffold_for_turn = False

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

        # Bail out if the session was already paused/completed/failed before
        # this wake cycle — prevents re-running a session the user stopped.
        if session.status in ("paused", "completed", "failed"):
            logger.info(
                "Session %s: status is '%s', skipping wake",
                session_id,
                session.status,
            )
            return

        # Resolve the sub-agent type (if any) and hydrate session config
        # with its presets.  Mutation is scoped to this wake cycle only —
        # the DB row is not modified.  The resolved def is also pushed to
        # the prompt builder so the identity section reflects the active
        # agent type.
        active_agent_def = await resolve_agent_def(
            session, self._tenant,
            session_factory=self._session_factory,
            bundle=self._bundle,
        )
        if active_agent_def is not None:
            apply_agent_def_to_session(session, active_agent_def)
        self._prompt.set_agent_def(active_agent_def)

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
            return "lease_held"

        # Start the background lease renewal task alongside the main loop.
        # Cancelled in the ``finally`` block below so the lease renews
        # regardless of how long any single iteration takes.
        renewal_task = asyncio.create_task(
            self._renew_lease_forever(session_id, lease.lease_token),
            name=f"lease-renewal-{session_id}",
        )

        try:
            # 3. Retrieve the harness cursor and the full event history.
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
                if last_user is not None:
                    last_user["content"] = build_deep_research_message(
                        topic=deep_research_topic,
                    )
                # The rewritten directive is the planning structure;
                # skip the SELF-DISCOVER / thinking-gate scaffold.
                self._skip_pre_llm_scaffold_for_turn = True

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
                    session_store=self._store,
                    sandbox_pool=self._sandbox_pool,
                )
                if expansion is not None:
                    expanded_text, skill_name, staged_at, kind = expansion
                    last_user["content"] = expanded_text
                    # The skill body itself is the planning structure;
                    # don't pay for the thinking-gate + SELF-DISCOVER
                    # classifier pair on this turn.
                    self._skip_pre_llm_scaffold_for_turn = True
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
                                    {
                                        "skill": skill_name,
                                        "raw_message": last_user_content,
                                        "staged_at": staged_at,
                                    },
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
            # Notify parent if this is a worker session.
            if session.parent_id is not None:
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
            # Stop the background renewal task before touching the lease.
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

        iteration = 0
        length_continuation_count = 0
        length_continuation_prefix = ""  # accumulated partial response across length retries
        consecutive_invalid_tool_calls = 0
        invalid_json_retries = 0  # API-level retries for malformed tool args
        thinking_prefill_retries = 0  # retries for thinking-only responses
        incomplete_scratchpad_retries = 0  # retries for unclosed REASONING_SCRATCHPAD
        empty_response_retries = 0  # retries for empty LLM responses (no content, no tools, no reasoning)
        content_with_tools_cache = ContentWithToolsCache()
        tool_guardrails = ToolGuardrails(
            ToolGuardrailConfig.from_mapping(
                session.config.get("tool_loop_guardrails")
                if session.config else None
            )
        )

        # Subdirectory hint tracker -- discovers context files as the agent navigates.
        hint_tracker = SubdirectoryHintTracker(
            initial_cwd=session.config.get("workspace_path"),
        )

        # --- Prefilled context injection ---
        # Ephemeral messages injected between system prompt and conversation
        # for few-shot examples or planning context. API-call-time only.
        prefill_messages: list[dict] = session.config.get("prefill_messages") or []

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
        # New user turn clears any prior runaway-thinking suppression.
        # Future turns get thinking back automatically.
        self._thinking_disabled_for_turn = False
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
                )
                return

            # 1. Emit LLM_REQUEST event.
            model_id = self._current_model or session.model or self._default_model
            llm_request_event_id = await self._store.emit_event(
                session.id,
                EventType.LLM_REQUEST,
                {
                    "model": model_id,
                    "iteration": iteration,
                    "turn_id": turn_id,
                    "iteration_index": iteration - 1,
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

            # Skip both scaffolding hops when a slash-skill
            # expansion already supplied planning structure for this
            # turn.  The flag is one-shot — consumed here and reset
            # so subsequent iterations within the same wake cycle go
            # back to the default behaviour (tool-call follow-ups
            # still get the gate so the model can drop thinking on
            # easy assistant turns).
            _skip_scaffold = getattr(
                self, "_skip_pre_llm_scaffold_for_turn", False,
            )
            self._skip_pre_llm_scaffold_for_turn = False
            if not _skip_scaffold:
                # Run both concurrently.  They mutate disjoint
                # parts of create_kwargs (gate → extra_body, self_discover
                # → messages), so gather is safe.  Wall-clock collapses
                # to max(gate_classifier, classifier+scaffold) instead
                # of their sum.
                await asyncio.gather(
                    self._maybe_apply_thinking_gate(
                        create_kwargs, api_messages, session,
                    ),
                    self._maybe_apply_self_discover(
                        create_kwargs, api_messages,
                    ),
                )

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
                    browser_pool=self._browser_pool,
                    browser_control=self._browser_control,
                    storage=self._storage,
                    api_client=self._api_client,
                    session_factory=self._session_factory,
                    llm_client=self._llm,
                    model=model_id,
                    vision_llm_client=self._vision_client,
                    vision_model=self._vision_model,
                    saga=saga,
                    log_policy_allowed=self._log_policy_allowed,
                    tool_guardrails=tool_guardrails,
                    bundle=self._bundle,
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
                self._propagate_runaway_flag(session, usage_data)
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
                        "iteration_index": iteration - 1,
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

            # 4a. Interrupt / staleness guard.  The stream and any judge
            # LLM call above can take several seconds.  If a new
            # user.message has been appended (or the interrupt flag was
            # set) while we were busy, the buffered response belongs to a
            # turn the user has already abandoned — drop it instead of
            # attributing it to the next user message.
            if await self._should_abort_before_llm_response(
                session, llm_request_event_id,
            ):
                await self._abort_iteration_with_pause(session, saga)
                return

            response_data["turn_id"] = turn_id
            response_data["iteration_index"] = iteration - 1
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

                # Text-only iteration: kick off the iteration-summary
                # task before _complete_session so the drain in A8 can
                # await it.
                await self._maybe_summarize_iteration(
                    session_id=session.id,
                    turn_id=turn_id,
                    iteration_index=iteration - 1,
                    reasoning_text=reasoning_text or "",
                    tool_calls=[],
                    started_at=iteration_started_at,
                )

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
                    browser_pool=self._browser_pool,
                    browser_control=self._browser_control,
                    storage=self._storage,
                    api_client=self._api_client,
                    session_factory=self._session_factory,
                    llm_client=self._llm,
                    model=model_id,
                    vision_llm_client=self._vision_client,
                    vision_model=self._vision_model,
                    saga=saga,
                    log_policy_allowed=self._log_policy_allowed,
                    bundle=self._bundle,
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
                iteration_index=iteration - 1,
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
        if session.user_id is None:
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
    ) -> set[str] | None:
        """Keep platform control-plane tools available after filtering."""
        if tool_filter is None:
            return None
        if "ask_user_question" not in self._tools.tool_names:
            return tool_filter
        updated = set(tool_filter)
        updated.add("ask_user_question")
        return updated

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
            return self._ensure_always_available_tools(tool_filter)

        if tool_filter is not None and not explicit_allowed:
            tool_filter = set(tool_filter)
            tool_filter.discard("loop_wait")
            tool_filter.discard("loop_complete")
        return self._ensure_always_available_tools(tool_filter)

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
    # Auto-think gate
    # ------------------------------------------------------------------

    def _propagate_runaway_flag(
        self,
        session: Session,
        usage_data: dict[str, Any] | None,
    ) -> None:
        """Flip the per-turn thinking-disabled flag if the LLM-call layer
        recovered from a runaway-reasoning stream by retrying with
        ``enable_thinking=False``.

        The flag is cleared at the start of every new user turn, so
        future turns get thinking back automatically.
        """
        if not usage_data:
            return
        if not usage_data.get("thinking_disabled_due_to_runaway"):
            return
        if self._thinking_disabled_for_turn:
            return
        self._thinking_disabled_for_turn = True
        logger.info(
            "Runaway-reasoning recovery: disabling thinking for "
            "remainder of user turn (session=%s).",
            session.id,
        )

    async def _maybe_apply_thinking_gate(
        self,
        create_kwargs: dict[str, Any],
        messages: list[dict[str, Any]],
        session: Session | None = None,
    ) -> None:
        """Inject reasoning-control knobs into ``create_kwargs["extra_body"]``.

        Three knobs feed in:

        1. ``enable_thinking`` -- disabled when ``_thinking_disabled_for_turn``
           is set (runaway recovery within the current user turn) or when
           the cached LLM classifier returns ``required=False`` for an
           easy turn.  Otherwise left at the provider default.
        2. ``thinking_budget`` -- read from ``session.config["thinking_budget"]``
           if set; caps reasoning tokens on providers that honor it
           (Qwen3 via DashScope).  Left at the provider default otherwise.
        3. ``preserve_thinking`` -- read from
           ``session.config["preserve_thinking"]``; tells the provider to
           feed prior assistant ``reasoning_content`` back into the input
           on subsequent turns.  Left at the provider default otherwise.

        The runaway flag in (1) clears on the next user turn (see the
        user-turn bookkeeping where ``_user_turn_count`` is incremented),
        so future turns get thinking back automatically.

        Classifier failures (aux unavailable, network error,
        structured-output parse miss) fall through silently -- the
        request just keeps the model default for ``enable_thinking``,
        while ``thinking_budget`` / ``preserve_thinking`` still apply.
        """
        model_id = str(create_kwargs.get("model") or "")
        if not model_supports_thinking_toggle(model_id):
            return
        if not messages:
            return

        session_cfg = session.config if session is not None else {}
        raw_budget = session_cfg.get("thinking_budget", DEFAULT_THINKING_BUDGET)
        thinking_budget: int | None
        if isinstance(raw_budget, (int, float)) and not isinstance(raw_budget, bool):
            thinking_budget = int(raw_budget)
        else:
            thinking_budget = None
        raw_preserve = session_cfg.get(
            "preserve_thinking", DEFAULT_PRESERVE_THINKING,
        )
        preserve_thinking: bool | None = (
            bool(raw_preserve) if isinstance(raw_preserve, bool) else None
        )

        enable_thinking: bool | None = None
        reason: str | None = None
        if self._thinking_disabled_for_turn:
            enable_thinking = False
            reason = "runaway flag set for current user turn"
        else:
            try:
                classification = await classify_hard_task_async(
                    messages,
                    tenant=self._tenant,
                )
            except Exception:
                logger.debug(
                    "Thinking-gate classification failed; leaving model default.",
                    exc_info=True,
                )
                classification = None

            if classification is not None and not classification.required:
                enable_thinking = False
                reason = (
                    f"easy turn (category={classification.category}, "
                    f"reason={classification.reason})"
                )

        if (
            enable_thinking is None
            and thinking_budget is None
            and preserve_thinking is None
        ):
            return

        thinking_extra = build_thinking_extra_body(
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            preserve_thinking=preserve_thinking,
        )
        create_kwargs["extra_body"] = merge_extra_body(
            create_kwargs.get("extra_body"),
            thinking_extra,
        )
        if reason is not None:
            logger.debug("Thinking-gate: disabling reasoning (%s).", reason)

    # ------------------------------------------------------------------
    # SELF-DISCOVER planning preamble
    # ------------------------------------------------------------------

    async def _maybe_apply_self_discover(
        self,
        create_kwargs: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> None:
        """Inject a SELF-DISCOVER scaffold as a synthetic user message.

        Two-tier gate before paying for ``build_scaffold`` (~24s upstream
        LLM call on cold sessions):

        1. If the prior assistant turn emitted a ``<next_action>``
           footer (per ``guidance/next_action`` prompt fragment),
           trust that as the model's self-reported intent for THIS
           turn.  Complexity ``low`` → skip scaffold entirely; ``high``
           → proceed; ``medium`` (or missing) → fall through to the
           classifier.
        2. Classifier gate: ``classify_hard_task_async`` returns a
           ``needs_scaffold`` field (see ``HardTaskJudgment``).  Only
           build the scaffold when the LLM judges it genuinely helpful.

        The scaffold itself is cached per-turn so iterations within a
        user turn reuse it without re-paying the build cost.  Appended
        to a **copy** of the messages list on ``create_kwargs``; the
        persistent conversation log is not mutated -- keeps the prompt
        cache, compressor, and event log untouched.
        """
        if not messages:
            return

        # Tier 1: trust the model's prior next_action declaration.
        prior_complexity = _prior_next_action_complexity(messages)
        if prior_complexity == "low":
            return  # Model said next turn is simple — believe it.

        # Tier 2: classifier-gated build.  Whether we reach here
        # because there was no prior declaration (turn 1, or model
        # forgot to emit one) or because the declaration was
        # ``medium``/``high`` (uncertain enough to warrant a check),
        # the classifier's ``needs_scaffold`` field is the final say.
        try:
            classification = await classify_hard_task_async(
                messages,
                tenant=self._tenant,
            )
        except Exception:
            logger.debug(
                "SELF-DISCOVER classification failed; skipping scaffold.",
                exc_info=True,
            )
            return

        if not classification.needs_scaffold:
            return
        # Defensive: SCAFFOLD_CATEGORIES still gates the category
        # whitelist so a misclassified ``needs_scaffold=true`` paired
        # with an unknown/none category cannot trigger an unscaffolded
        # build.
        if classification.category not in SCAFFOLD_CATEGORIES:
            return

        try:
            scaffold = await build_scaffold(
                messages,
                category=classification.category,
                tenant=self._tenant,
            )
        except Exception:
            logger.debug(
                "SELF-DISCOVER build_scaffold raised; skipping scaffold.",
                exc_info=True,
            )
            return

        if scaffold is None:
            return

        # Merge the scaffold into the latest user message in-place
        # rather than appending it as a separate synthetic user
        # message. With a separate trailing message, every iteration's
        # last user-role content was the scaffold's imperative; the
        # model kept narrating "The user is asking me to continue..."
        # because that's literally what its most recent user message
        # said. Bundling the scaffold into the original request keeps
        # the planning context visible without re-prompting the model
        # on every iteration.
        body = format_scaffold_for_injection(scaffold)
        working = list(create_kwargs["messages"])
        target_idx = -1
        for i in range(len(working) - 1, -1, -1):
            if working[i].get("role") == "user":
                target_idx = i
                break
        if target_idx < 0:
            return
        target = dict(working[target_idx])
        original_content = target.get("content")
        if isinstance(original_content, str):
            target["content"] = f"{original_content}\n\n{body}"
        elif isinstance(original_content, list):
            # Multimodal user message (text blocks + images). Append
            # the scaffold as an extra text block so we don't
            # stringify and lose the image parts.
            target["content"] = list(original_content) + [
                {"type": "text", "text": body},
            ]
        else:
            # Unknown content shape — leave the message alone rather
            # than corrupt it.
            return
        target["_surogate_scaffold_merged"] = True
        working[target_idx] = target
        create_kwargs["messages"] = working
        logger.debug(
            "SELF-DISCOVER: merged scaffold into user msg "
            "(category=%s, modules=%s).",
            classification.category,
            scaffold.relevant_modules,
        )

    # ------------------------------------------------------------------
    # Hidden advisor routing
    # ------------------------------------------------------------------

    async def _maybe_consult_required_advisor(
        self,
        session: Session,
        messages: list[dict],
        all_events: list[Event],
        system_prompt: str = "",
        consulted_categories: set[str] | None = None,
    ) -> bool:
        """Ask the hidden advisor for guidance before hard executor work."""
        consulted_categories = consulted_categories if consulted_categories is not None else set()
        last_user = self._last_user_message(messages)
        if last_user is None:
            return False
        if not self._advisor_available():
            return False

        user_content = str(last_user.get("content") or "")
        classification = await classify_hard_task_async(
            messages,
            tenant=self._tenant,
        )
        if not classification.required or classification.category is None:
            return False

        if (
            classification.category in consulted_categories
            or classification.category in self._advisor_categories_after_latest_user(
                all_events,
            )
        ):
            return False

        result = await self._consult_advisor_for_category(
            session=session,
            messages=messages,
            system_prompt=system_prompt,
            category=classification.category,
            task=user_content,
            reason="early",
            consulted_categories=consulted_categories,
        )
        if not result:
            return False

        messages.append({
            "role": "user",
            "content": self._format_advisor_context(
                category=classification.category,
                content=result,
            ),
        })
        return True

    def _advisor_available(self) -> bool:
        return self._advisor_client is not None and bool(self._advisor_model)

    async def _consult_advisor_for_category(
        self,
        *,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        category: str,
        task: str,
        reason: Literal["early", "final_check"],
        consulted_categories: set[str],
    ) -> str | None:
        if not self._advisor_available():
            return None
        if len(consulted_categories) >= self._advisor_max_calls_per_turn:
            return None
        if category in consulted_categories:
            return None

        consulted_categories.add(category)
        await self._emit_advisor_request(session, reason, category)

        try:
            assert self._advisor_client is not None
            response = await self._advisor_client.chat.completions.create(
                model=self._advisor_model,
                messages=self._build_advisor_messages(
                    messages=messages,
                    system_prompt=system_prompt,
                    category=category,
                    task=task,
                    reason=reason,
                ),
                temperature=0.2,
                max_tokens=self._advisor_max_tokens,
            )
            content = _extract_response_text(response)
            if not content:
                raise RuntimeError("advisor returned empty guidance")
            usage = getattr(response, "usage", None)
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_RESULT,
                {
                    "model": self._advisor_model,
                    "reason": reason,
                    "category": category,
                    "content": content,
                    "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                    "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                },
            )
            return content
        except Exception as exc:
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_FAILURE,
                {
                    "model": self._advisor_model,
                    "reason": reason,
                    "category": category,
                    "error": str(exc),
                },
            )
            logger.debug(
                "Session %s: advisor call failed for %s/%s",
                session.id,
                reason,
                category,
                exc_info=True,
            )
            return None

    def _build_advisor_messages(
        self,
        *,
        messages: list[dict],
        system_prompt: str,
        category: str,
        task: str,
        reason: str,
    ) -> list[dict[str, str]]:
        transcript = self._build_advisor_context(messages)
        prompt = (
            "You are a strategic advisor for an agent harness. The executor "
            "model is cheaper and will continue the task after reading your "
            "guidance. Give concise, high-leverage advice under "
            f"{self._advisor_max_tokens} tokens. Do not solve by writing the "
            "entire final answer unless that is the only useful guidance.\n\n"
            f"Advisor reason: {reason}\n"
            f"Hard-task category: {category}\n\n"
            f"Current task or tool intent:\n{task}\n\n"
            f"Recent transcript:\n{transcript}"
        )
        if system_prompt:
            prompt = f"Executor system prompt:\n{system_prompt[-8000:]}\n\n{prompt}"
        return [{"role": "user", "content": prompt}]

    async def _emit_advisor_request(
        self,
        session: Session,
        reason: str,
        category: str,
    ) -> None:
        try:
            await self._store.emit_event(
                session.id,
                EventType.ADVISOR_REQUEST,
                {
                    "model": self._advisor_model,
                    "reason": reason,
                    "category": category,
                },
            )
        except Exception:
            logger.debug(
                "Session %s: failed to emit advisor request",
                session.id,
                exc_info=True,
            )

    @staticmethod
    def _last_user_message(messages: list[dict]) -> dict | None:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                return msg
        return None

    @staticmethod
    def _advisor_categories_after_latest_user(events: list[Event]) -> set[str]:
        latest_user_event_id = 0
        for event in events:
            if event.type == EventType.USER_MESSAGE.value and event.id is not None:
                latest_user_event_id = max(latest_user_event_id, event.id)
        categories: set[str] = set()
        for event in events:
            if event.id is None or event.id <= latest_user_event_id:
                continue
            if event.type in {
                EventType.ADVISOR_RESULT.value,
                EventType.ADVISOR_FAILURE.value,
            }:
                category = event.data.get("category")
                if category:
                    categories.add(str(category))
        return categories

    @staticmethod
    def _build_advisor_context(messages: list[dict]) -> str:
        fragments: list[str] = []
        for msg in messages[-12:]:
            role = msg.get("role", "unknown")
            content = msg.get("content") or ""
            if isinstance(content, list):
                content = _collapse_text_parts([
                    part
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ])
            if not isinstance(content, str) or not content.strip():
                continue
            fragments.append(f"{role}: {content}")
        return "\n\n".join(fragments)[-16_000:]

    @staticmethod
    def _format_advisor_context(
        *,
        category: str,
        content: str,
    ) -> str:
        return (
            f"[Advisor guidance: {category}]\n"
            f"{content}\n\n"
            "Use this as strategic guidance. Verify with tools where "
            "appropriate and adapt if direct evidence contradicts it."
        )

    # ------------------------------------------------------------------
    # Message reconstruction from event log
    # ------------------------------------------------------------------

    async def _prefetch_memory(self, session_id: UUID) -> str | None:
        """Prefetch user memory and snapshot it for the session.

        The first wake() of a session reads memory from disk; every
        subsequent wake() reuses the cached snapshot byte-identically so
        the memory_context message stays in the provider's prefix cache.
        The snapshot is invalidated alongside the system prompt cache
        (compression / context overflow / explicit reset).

        If a MemoryManager is available, delegates to it and wraps the
        result in a ``<memory-context>`` fence.  Otherwise falls back to
        direct file I/O.
        """
        if session_id in self._memory_snapshot_cache:
            return self._memory_snapshot_cache[session_id]

        snapshot = await self._load_memory_snapshot()
        self._memory_snapshot_cache[session_id] = snapshot
        return snapshot

    async def _load_memory_snapshot(self) -> str | None:
        """Read the current memory context from disk (no caching)."""
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
                f"users/{self._tenant.user_id}/memory",
                "shared/memory",
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
                content = _render_inlined_attachments(
                    content, event.data.get("attachments"),
                )
                # Fold per-user ephemeral notes (view-context, non-inlined
                # attachments) into the user content here so the bytes are
                # determined entirely by the durable event payload.  This
                # keeps the provider's implicit prefix cache stable across
                # turns -- the previous design inserted the notes mid-array
                # before the latest user message, which left them present
                # in turn T's request but absent in turn T+1's prefix.
                note_parts: list[str] = []
                view_note = _view_context_note_from_metadata(
                    event.data.get("metadata"),
                )
                if view_note:
                    note_parts.append(view_note)
                attachments_note = _attachments_note_from_data(event.data)
                if attachments_note:
                    note_parts.append(attachments_note)
                if note_parts:
                    notes_block = "\n\n".join(note_parts)
                    content = (
                        f"{notes_block}\n\n{content}" if content else notes_block
                    )
                images = event.data.get("images")
                if images:
                    logger.info(
                        "User message has %d image(s), first mime: %s",
                        len(images),
                        images[0].get("mime_type", "?"),
                    )
                if images:
                    blocks: list[dict] = [{"type": "text", "text": content}]
                    for img in images:
                        data_url = img["data"]
                        if not data_url.startswith("data:"):
                            mime = img.get("mime_type", "image/png")
                            data_url = f"data:{mime};base64,{data_url}"
                        blocks.append({
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "auto"},
                        })
                    user_msg = {"role": "user", "content": blocks}
                    from surogates.harness.image_shrink import shrink_image_parts_in_messages
                    shrink_image_parts_in_messages([user_msg])
                    messages.append(user_msg)
                else:
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

            elif etype == EventType.ADVISOR_RESULT.value and event.data.get("content"):
                messages.append({
                    "role": "user",
                    "content": self._format_advisor_context(
                        category=event.data.get("category", "advisor"),
                        content=str(event.data.get("content") or ""),
                    ),
                })

            elif etype == EventType.CONTEXT_COMPACT.value:
                compacted = event.data.get("compacted_messages")
                if compacted is not None:
                    messages = list(compacted)

            # Worker coordination events — injected as synthetic user
            # messages so the coordinator LLM sees worker results.
            elif etype == EventType.WORKER_COMPLETE.value:
                worker_id = event.data.get("worker_id", "?")
                result = event.data.get("result", "")
                messages.append({
                    "role": "user",
                    "content": f"[Worker {worker_id} completed]\n{result}",
                })

            elif etype == EventType.WORKER_FAILED.value:
                worker_id = event.data.get("worker_id", "?")
                error = event.data.get("error", "unknown error")
                messages.append({
                    "role": "user",
                    "content": f"[Worker {worker_id} failed: {error}]",
                })

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
        self._memory_snapshot_cache.pop(session.id, None)

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

    async def _maybe_summarize_iteration(
        self,
        *,
        session_id: UUID,
        turn_id: str,
        iteration_index: int,
        reasoning_text: str,
        tool_calls: list[dict[str, Any]],
        started_at: str,
        tool_results: list[dict[str, Any]] | None = None,
    ) -> None:
        """Fire-and-forget per-iteration summarization.

        Spawns a background task that calls the summarizer and emits an
        ``ITERATION_SUMMARY`` event when it resolves. Tracked in
        ``_pending_iteration_summary_tasks`` so :meth:`_complete_session`
        can drain it before emitting ``TURN_SUMMARY``. No-op when the
        harness has no summarizer or the iteration produced nothing
        worth summarizing.
        """
        if self._turn_summarizer is None:
            return
        if not reasoning_text and not tool_calls:
            return

        # Snapshot only summaries that have already resolved for earlier
        # iterations of this turn. Later summaries may still be pending;
        # awaiting them here would defeat the fire-and-forget design and
        # could deadlock the loop.
        prior_summaries = [
            self._completed_iteration_summaries[idx]
            for idx in sorted(self._completed_iteration_summaries)
            if idx < iteration_index
        ]
        tool_call_ids = [
            str(tc.get("id") or "") for tc in tool_calls
        ]

        async def _run() -> None:
            summary = await self._turn_summarizer.summarize_iteration(
                iteration_id=f"{turn_id}:{iteration_index}",
                reasoning=reasoning_text,
                tool_calls=tool_calls,
                prior_iteration_summaries=prior_summaries,
                tool_results=tool_results,
            )
            if summary is None:
                return
            self._completed_iteration_summaries[iteration_index] = summary
            try:
                await self._store.emit_event(
                    session_id,
                    EventType.ITERATION_SUMMARY,
                    {
                        "turn_id": turn_id,
                        "iteration_index": iteration_index,
                        "summary": summary,
                        "tool_call_ids": tool_call_ids,
                        "started_at": started_at,
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to emit ITERATION_SUMMARY for %s iter %d",
                    session_id, iteration_index, exc_info=True,
                )

        task = asyncio.create_task(
            _run(), name=f"iteration-summary-{turn_id}-{iteration_index}",
        )
        # Track in two places: the per-turn dict keyed by
        # iteration_index lets _drain_and_emit_turn_summary await the
        # right tasks before generating the recap; _background_tasks
        # lets wake()'s finally drain anything still in flight when the
        # turn ends abnormally (cancellation, crash).
        self._pending_iteration_summary_tasks[iteration_index] = task
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(
            lambda _t: self._pending_iteration_summary_tasks.pop(
                iteration_index, None,
            ),
        )

    async def _request_final_summary(
        self,
        session: Session,
        messages: list[dict],
        system_prompt: str,
        lease: SessionLease,
        *,
        cost_tracker: SessionCostTracker | None = None,
        turn_id: str | None = None,
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

            await self._maybe_apply_thinking_gate(
                create_kwargs, api_messages, session,
            )
            await self._maybe_apply_self_discover(create_kwargs, api_messages)

            assistant_message, usage_data = await call_llm_with_retry(
                session=session,
                create_kwargs=create_kwargs,
                iteration=self._budget.used + 1,
                turn_id=turn_id,
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
            self._propagate_runaway_flag(session, usage_data)

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
                final_payload["iteration_index"] = max(self._budget.used - 1, 0)
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

    # ------------------------------------------------------------------
    # /compress command handler
    # ------------------------------------------------------------------

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

    def _outcome_settings(self) -> Any:
        try:
            from surogates.config import load_settings

            return load_settings().outcomes
        except Exception:
            logger.debug("Failed to load outcome settings", exc_info=True)
            return SimpleNamespace(
                max_iterations=DEFAULT_MAX_ITERATIONS,
                max_parse_failures=3,
            )

    async def _session_has_active_mission(self, session_id: UUID) -> bool:
        """True iff the session has an active or paused mission row.

        Used by ``_handle_goal_command`` to enforce mutual exclusion: only
        one evaluator loop per session is allowed, so a /mission already
        in flight blocks /goal creation (and vice versa).
        """
        if self._session_factory is None:
            return False
        try:
            from surogates.missions.store import MissionStore

            store = MissionStore(self._session_factory)
            return (await store.get_active_for_session(session_id)) is not None
        except Exception:
            logger.debug(
                "Mission active-check failed for session %s; treating as no mission",
                session_id, exc_info=True,
            )
            return False

    async def _mission_has_pending_work(self, session_id: UUID) -> bool:
        """True iff the session's mission is in a non-terminal status.

        The session is owned by the mission's lifecycle: while the
        mission is ``active`` or ``paused`` it can still produce work
        (more tasks to spawn, an evaluator retry after a parse failure,
        a ``/mission resume`` after a manual pause). Completing the
        session here would set ``status=completed``, every subsequent
        wake would bail at the status guard in ``process_wake_cycle``,
        and the mission could never progress.

        Only when the mission reaches a terminal status (``satisfied``,
        ``blocked``, ``failed``, ``cancelled``, ``max_iterations_reached``)
        does ``apply_verdict`` clear ``active_mission_id`` from the
        session config; ``get_active_for_session`` then returns ``None``,
        this method returns ``False``, and the session completes
        normally on the next no-tool-call response.

        Returns ``False`` (allow completion) on any failure path so a
        bug in the mission layer can't strand sessions forever.
        """
        if self._session_factory is None:
            return False
        try:
            from surogates.missions.store import MissionStore

            store = MissionStore(self._session_factory)
            active = await store.get_active_for_session(session_id)
            return active is not None
        except Exception:
            logger.debug(
                "Mission pending-work check failed for session %s; "
                "falling back to completing session",
                session_id, exc_info=True,
            )
            return False

    async def _handle_mission_command(
        self,
        session: Session,
        content: str,
        lease: SessionLease,
    ) -> None:
        """Dispatch ``/mission ...`` to the matching handler.

        Mirrors :meth:`_handle_goal_command`: parses args, calls into
        :mod:`surogates.missions.commands`, then emits an LLM_RESPONSE
        carrying the operator-visible message and advances the harness
        cursor so the same wake does not re-process the command.
        """
        from surogates.missions.commands import (
            MissionCommandParseError,
            MissionHandlerResult,
            handle_mission_cancel,
            handle_mission_create,
            handle_mission_pause,
            handle_mission_resume,
            handle_mission_status,
            parse_mission_command,
        )
        from surogates.missions.store import MissionStore

        # ``result`` is the inner handler's return when a branch invokes
        # one; the post-cursor kickoff emit reads ``result.kickoff_content``.
        # Branches that short-circuit on a precondition (missing Redis,
        # missing principal, unparseable command, invalid action) leave
        # this None and skip the kickoff emit.
        result: MissionHandlerResult | None = None

        args = content[len("/mission"):].strip()
        try:
            command = parse_mission_command(args)
        except MissionCommandParseError as exc:
            message = f"/mission parse error: {exc}"
        else:
            if self._session_factory is None:
                message = (
                    "/mission requires a configured session factory; "
                    "this looks like a harness initialization bug."
                )
            else:
                mission_store = MissionStore(self._session_factory)
                redis_client = self._redis
                if command.action == "create":
                    principal_user_id = self._tenant.user_id
                    principal_sa_id = self._tenant.service_account_id
                    if redis_client is None:
                        message = (
                            "/mission create cannot run without a Redis "
                            "connection (the coordinator must be enqueued "
                            "after kickoff)."
                        )
                    elif principal_user_id is None and principal_sa_id is None:
                        # Anonymous-channel sessions have neither a user nor
                        # a service-account principal — the session itself is
                        # the principal.  Missions need a durable owner that
                        # outlives the session, so reject these explicitly.
                        message = (
                            "/mission requires a user or service-account "
                            "session — anonymous channel sessions cannot "
                            "own missions."
                        )
                    else:
                        result = await handle_mission_create(
                            description=command.description or "",
                            rubric=command.rubric or "",
                            session_id=session.id,
                            user_id=principal_user_id,
                            service_account_id=principal_sa_id,
                            org_id=self._tenant.org_id,
                            agent_id=session.agent_id,
                            session_store=self._store,
                            session_factory=self._session_factory,
                            mission_store=mission_store,
                        )
                        message = result.message or result.error
                        if result.ok and result.mission_id is not None:
                            # Propagate the config write back to the
                            # in-memory session so the rest of this wake
                            # sees ``coordinator=True`` + the orchestrator
                            # skill preload. Without this, the kickoff
                            # message gets processed by the same wake
                            # against a stale config and tools gated on
                            # ``coordinator`` (``spawn_task`` &c) get
                            # filtered out as worker-excluded.
                            cfg = dict(session.config or {})
                            cfg["active_mission_id"] = str(result.mission_id)
                            cfg["coordinator"] = True
                            # Strip implementation tools so the LLM has to
                            # delegate via spawn_task/delegate_task instead of
                            # "fixing it quickly" itself.  See
                            # COORDINATOR_IMPLEMENTATION_TOOLS for the set.
                            cfg["strict_coordinator"] = True
                            preloaded = list(cfg.get("preloaded_skills") or [])
                            if "subagent-task-orchestrator" not in preloaded:
                                preloaded.append("subagent-task-orchestrator")
                            cfg["preloaded_skills"] = preloaded
                            session.config = cfg
                elif command.action == "status":
                    result = await handle_mission_status(
                        session_id=session.id, mission_store=mission_store,
                    )
                    message = result.message
                elif command.action == "pause":
                    result = await handle_mission_pause(
                        session_id=session.id,
                        reason=command.reason,
                        session_store=self._store,
                        mission_store=mission_store,
                    )
                    message = result.message or result.error
                elif command.action == "resume":
                    if redis_client is None:
                        message = (
                            "/mission resume cannot wake the coordinator "
                            "without a Redis connection."
                        )
                    else:
                        result = await handle_mission_resume(
                            session_id=session.id,
                            org_id=str(session.org_id),
                            agent_id=session.agent_id,
                            session_store=self._store,
                            mission_store=mission_store,
                            redis=redis_client,
                        )
                        message = result.message or result.error
                elif command.action == "cancel":
                    if redis_client is None:
                        message = (
                            "/mission cancel cannot cascade interrupts "
                            "without a Redis connection."
                        )
                    else:
                        result = await handle_mission_cancel(
                            session_id=session.id,
                            reason=command.reason,
                            cascade_to_workers=command.cascade_to_workers,
                            session_store=self._store,
                            session_factory=self._session_factory,
                            mission_store=mission_store,
                            redis=redis_client,
                        )
                        message = result.message or result.error
                        if result.ok:
                            # Mirror the DB ``clear_session_config_key``
                            # call in the in-memory session so subsequent
                            # iterations of this wake (and the next /goal
                            # mutual-exclusion check) see no active
                            # mission.
                            cfg = dict(session.config or {})
                            cfg.pop("active_mission_id", None)
                            session.config = cfg
                else:
                    message = (
                        "Usage: /mission <description>\\n\\nRubric:\\n<criterion>"
                        " | /mission status | /mission pause [reason]"
                        " | /mission resume | /mission cancel [--cascade] [reason]"
                    )

        response_event_id = await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": message}},
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=response_event_id,
            lease_token=lease.lease_token,
        )

        # /mission create defers its synthetic kickoff message until after
        # the slash response's cursor advance — otherwise the cursor races
        # past the kickoff's event id and the next wake bails with
        # "no actionable pending events".  Mirrors the /goal flow above.
        if (
            result is not None
            and result.ok
            and result.kickoff_content is not None
        ):
            await self._store.emit_event(
                session.id, EventType.USER_MESSAGE,
                {
                    "content": result.kickoff_content,
                    "synthetic": "mission_kickoff",
                },
            )
            if redis_client is not None:
                try:
                    from surogates.config import enqueue_session

                    await enqueue_session(
                        redis_client,
                        org_id=str(session.org_id),
                        agent_id=session.agent_id,
                        session_id=session.id,
                    )
                except Exception:
                    logger.debug(
                        "Failed to enqueue mission kickoff", exc_info=True,
                    )

    async def _handle_goal_command(
        self,
        session: Session,
        content: str,
        lease: SessionLease,
    ) -> None:
        args = content[len("/goal") :].strip()
        command = parse_goal_command(args)
        current = OutcomeState.from_config((session.config or {}).get("outcome"))

        outcome_kickoff_needed = False

        if command.action == "status":
            message = self._format_outcome_status(current)
        elif command.action == "set":
            # Reject setting a new outcome while one is active — a continuation
            # kickoff for the prior outcome may be pending in the event log,
            # and overwriting session.config["outcome"] would orphan it.
            if current is not None and current.status == "active":
                message = (
                    f"Outcome already active ({current.iteration}/"
                    f"{current.max_iterations}): {current.description}. "
                    "Use /goal pause or /goal clear before setting a new outcome."
                )
            elif await self._session_has_active_mission(session.id):
                # Mutual exclusion: only one evaluator loop per session.
                # /mission already runs an evaluator — adding a /goal would
                # produce two competing judges on the same chat.
                message = (
                    "This session has an active /mission. Cancel or pause it "
                    "before setting a /goal (only one evaluator loop per "
                    "session is allowed)."
                )
            else:
                message = await self._define_goal_outcome(session, command)
                outcome_kickoff_needed = True
        elif command.action == "pause":
            message = await self._pause_goal_outcome(session, current)
        elif command.action == "resume":
            message = await self._resume_goal_outcome(session, current)
        elif command.action == "clear":
            message = await self._clear_goal_outcome(session, current)
        else:
            message = "Usage: /goal <outcome>, /goal status, /goal pause, /goal resume, /goal clear."

        response_event_id = await self._store.emit_event(
            session.id,
            EventType.LLM_RESPONSE,
            {"message": {"role": "assistant", "content": message}},
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=response_event_id,
            lease_token=lease.lease_token,
        )

        if outcome_kickoff_needed:
            outcome = OutcomeState.from_config((session.config or {}).get("outcome"))
            if outcome is None:
                return
            outcome_id = outcome.id if outcome else None
            kickoff_id = await self._store.emit_synthetic_user_message(
                session.id,
                content=outcome.description,
                synthetic="outcome_kickoff",
                metadata={"outcome_id": outcome_id},
            )
            logger.debug(
                "Session %s: emitted outcome kickoff user message %s",
                session.id,
                kickoff_id,
            )
            if self._redis is not None:
                try:
                    from surogates.config import enqueue_session

                    await enqueue_session(
                        self._redis,
                        org_id=str(session.org_id),
                        agent_id=session.agent_id,
                        session_id=session.id,
                    )
                except Exception:
                    logger.debug("Failed to enqueue outcome kickoff", exc_info=True)

    async def _define_goal_outcome(self, session: Session, command: Any) -> str:
        settings = self._outcome_settings()
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            state = start_outcome(
                command.text,
                rubric=command.rubric,
                max_iterations=getattr(
                    settings,
                    "max_iterations",
                    DEFAULT_MAX_ITERATIONS,
                ),
                now_iso=now_iso,
            )
        except ValueError:
            return "Usage: /goal <outcome>. Example: /goal Fix all failing tests."

        await self._store.update_session_config_key(
            session.id,
            "outcome",
            state.to_config(),
        )
        session.config = {**(session.config or {}), "outcome": state.to_config()}
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_DEFINED,
            {
                "outcome_id": state.id,
                "description": state.description,
                "rubric": state.rubric,
                "max_iterations": state.max_iterations,
            },
        )
        return f"Outcome defined ({state.max_iterations} iterations): {state.description}"

    async def _pause_goal_outcome(
        self,
        session: Session,
        current: OutcomeState | None,
    ) -> str:
        if current is None or current.status not in {"active", "paused"}:
            return "No active outcome. Set one with /goal <text>."
        current.status = "paused"
        current.paused_reason = "user-paused"
        current.updated_at = datetime.now(timezone.utc).isoformat()
        await self._store.update_session_config_key(
            session.id,
            "outcome",
            current.to_config(),
        )
        session.config = {**(session.config or {}), "outcome": current.to_config()}
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_PAUSED,
            {"outcome_id": current.id, "reason": current.paused_reason},
        )
        return f"Outcome paused: {current.description}"

    async def _resume_goal_outcome(
        self,
        session: Session,
        current: OutcomeState | None,
    ) -> str:
        if current is None or current.status not in {"paused", "max_iterations_reached"}:
            return "No paused outcome to resume."
        current.status = "active"
        current.paused_reason = None
        current.updated_at = datetime.now(timezone.utc).isoformat()
        await self._store.update_session_config_key(
            session.id,
            "outcome",
            current.to_config(),
        )
        session.config = {**(session.config or {}), "outcome": current.to_config()}
        return f"Outcome resumed: {current.description}"

    async def _clear_goal_outcome(
        self,
        session: Session,
        current: OutcomeState | None,
    ) -> str:
        await self._store.clear_session_config_key(session.id, "outcome")
        session.config = {**(session.config or {})}
        session.config.pop("outcome", None)
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_CLEARED,
            {"outcome_id": current.id if current else None},
        )
        return "Outcome cleared." if current else "No active outcome."

    def _format_outcome_status(self, state: OutcomeState | None) -> str:
        if state is None:
            return "No active outcome. Set one with /goal <text>."
        lines = [
            (
                f"Outcome ({state.status}, {state.iteration}/"
                f"{state.max_iterations} iterations): {state.description}"
            ),
        ]
        if state.last_explanation:
            lines.append(f"Last evaluation: {state.last_explanation}")
        if state.paused_reason:
            lines.append(f"Paused reason: {state.paused_reason}")
        return "\n".join(lines)

    async def _maybe_run_mission_evaluator_for_session(
        self,
        *,
        session: Session,
        latest_response: str,
        model: str,
    ) -> None:
        """Bind self's session_factory / store / LLM and dispatch to the
        module-level :func:`_maybe_run_mission_evaluator`.

        Kept as an instance method so it can pull the configured eval
        model and LLM client; the actual logic (trigger detection,
        prompt building, verdict handling) lives on the module-level
        helper so tests can drive it with a stubbed judge without
        constructing a full harness.
        """
        if self._session_factory is None:
            return
        from surogates.missions.store import MissionStore

        settings = self._outcome_settings()
        eval_model = getattr(settings, "evaluator_model", "") or model
        judge = _build_mission_judge(
            llm_client=self._llm, eval_model=eval_model,
        )
        await _maybe_run_mission_evaluator(
            session_id=session.id,
            coordinator_last_response=latest_response,
            session_store=self._store,
            session_factory=self._session_factory,
            mission_store=MissionStore(self._session_factory),
            judge=judge,
        )

    async def _evaluate_outcome(
        self,
        *,
        state: OutcomeState,
        latest_response: str,
        model: str,
    ) -> Any:
        settings = self._outcome_settings()
        eval_model = getattr(settings, "evaluator_model", "") or model
        messages = build_evaluator_messages(
            state,
            latest_response,
            response_max_chars=getattr(
                settings, "evaluator_response_max_chars", 16384,
            ),
        )
        try:
            response = await self._llm.chat.completions.create(
                model=eval_model,
                messages=messages,
                temperature=0,
                max_tokens=500,
            )
            raw = self._extract_chat_message_content(response)
        except Exception as exc:
            logger.warning(
                "Outcome evaluator failed for %s: %s",
                state.id,
                exc,
            )
            raw = json.dumps({
                "result": "needs_revision",
                "explanation": f"evaluator error: {type(exc).__name__}",
                "feedback": "Continue working toward the outcome.",
            })
        return parse_outcome_evaluation(raw)

    async def _maybe_continue_outcome(
        self,
        session: Session,
        lease: SessionLease,
        *,
        latest_response: str,
        response_event_id: int,
        model: str,
    ) -> bool:
        state = OutcomeState.from_config((session.config or {}).get("outcome"))
        if state is None or state.status != "active":
            return False

        start_event_id = await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_START,
            {
                "outcome_id": state.id,
                "iteration": state.iteration,
                "response_event_id": response_event_id,
            },
        )
        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_ONGOING,
            {"outcome_id": state.id, "iteration": state.iteration},
        )
        evaluation = await self._evaluate_outcome(
            state=state,
            latest_response=latest_response,
            model=model,
        )
        settings = self._outcome_settings()
        decision = apply_evaluation(
            state,
            evaluation,
            now_iso=datetime.now(timezone.utc).isoformat(),
            max_parse_failures=getattr(settings, "max_parse_failures", 3),
        )
        await self._store.update_session_config_key(
            session.id,
            "outcome",
            state.to_config(),
        )
        session.config = {**(session.config or {}), "outcome": state.to_config()}

        await self._store.emit_event(
            session.id,
            EventType.OUTCOME_EVALUATION_END,
            {
                "outcome_id": state.id,
                "outcome_evaluation_start_id": start_event_id,
                "iteration": state.iteration,
                "result": decision.result,
                "explanation": evaluation.explanation,
                "feedback": evaluation.feedback,
                "parse_failed": evaluation.parse_failed,
            },
        )

        status_event_id: int | None = None
        if decision.message:
            status_event_id = await self._store.emit_event(
                session.id,
                EventType.LLM_RESPONSE,
                {"message": {"role": "assistant", "content": decision.message}},
            )

        if not decision.should_continue or not decision.continuation_prompt:
            return False

        marker_event_id = await self._store.emit_event(
            session.id,
            EventType.OUTCOME_CONTINUATION,
            {
                "outcome_id": state.id,
                "iteration": state.iteration,
                "status_event_id": status_event_id,
            },
        )
        continuation_event_id = await self._store.emit_synthetic_user_message(
            session.id,
            content=decision.continuation_prompt,
            synthetic="outcome_continuation",
            metadata={"outcome_id": state.id},
        )
        await self._store.advance_harness_cursor(
            session.id,
            through_event_id=marker_event_id,
            lease_token=lease.lease_token,
        )
        logger.debug(
            "Session %s: outcome continuation user message %s queued",
            session.id,
            continuation_event_id,
        )
        if self._redis is not None:
            try:
                from surogates.config import enqueue_session

                await enqueue_session(
                    self._redis,
                    org_id=str(session.org_id),
                    agent_id=session.agent_id,
                    session_id=session.id,
                )
            except Exception:
                logger.debug("Failed to enqueue outcome continuation", exc_info=True)
        return True

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
        messages = [m for m in messages if not (
            m.get("role") == "user" and (m.get("content") or "").strip() == "/compress"
        )]

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

    async def _promote_fenced_artifacts(
        self,
        session: Session,
        assistant_content: str,
        messages: list[dict],
    ) -> None:
        """Auto-create an artifact when the LLM emits a render-worthy
        fenced block instead of calling ``create_artifact``.

        Some smaller models (``gpt-5.4-mini`` observed) prefer a
        one-token ` ```svg ` fence over a multi-token tool call with an
        escaped SVG payload, even when the system prompt explicitly
        forbids it.  Rather than leave the user staring at raw source,
        we parse the final assistant content for known render-capable
        fences and promote the first one into an artifact via the API.

        Only fires when:
        - an API client is wired (``self._api_client``),
        - the content contains at least one promotable fence (svg/html),
        - the fence body parses as non-empty.

        At most ONE artifact is created per response, matching the
        guidance's one-artifact-per-response rule.  Failures are logged
        but swallowed — a failed auto-promotion must not derail the
        turn.
        """
        if self._api_client is None or not assistant_content:
            return

        match = _FENCE_RE.search(assistant_content)
        while match is not None:
            lang = match.group(1).lower()
            mapping = _PROMOTABLE_FENCES.get(lang)
            if mapping is None:
                match = _FENCE_RE.search(assistant_content, match.end())
                continue
            body = match.group(2).strip()
            if not body:
                match = _FENCE_RE.search(assistant_content, match.end())
                continue
            kind, spec_key = mapping
            name = _derive_artifact_name(kind, messages)
            try:
                await self._api_client.create_artifact(
                    name=name, kind=kind, spec={spec_key: body},
                )
                logger.info(
                    "Session %s: promoted ```%s fence to %s artifact",
                    session.id, lang, kind,
                )
            except Exception:
                logger.warning(
                    "Session %s: failed to auto-promote ```%s fence",
                    session.id, lang, exc_info=True,
                )
            return  # one artifact per response

    # ------------------------------------------------------------------
    # Session completion
    # ------------------------------------------------------------------

    async def _end_turn(
        self,
        session: Session,
        lease: SessionLease,
        *,
        through_event_id: int,
    ) -> None:
        """End the current turn of a primary session.

        Advances the harness cursor to ``through_event_id`` so a future wake()
        replays from the right point, and returns.  The session stays in its
        current status (typically 'active') so the user can send a follow-up.
        The sandbox pod, memory manager, and cost tracker are deliberately
        left alive — they belong to the session, not the turn.  The lease is
        released by the outer wake() finally block.
        """
        try:
            await self._store.advance_harness_cursor(
                session.id, through_event_id, lease.lease_token,
            )
        except Exception:
            logger.warning(
                "Failed to advance cursor at end of turn for %s",
                session.id,
            )

    async def _drain_background_tasks(self, session_id: UUID) -> None:
        """Wait for fire-and-forget background tasks to finish before lease release.

        Bounded by ``_BACKGROUND_DRAIN_TIMEOUT_SECONDS`` so a hung task can't
        delay lease release indefinitely.  Anything still pending after the
        timeout is cancelled; exceptions are swallowed because these tasks are
        best-effort by design.

        Tasks are dropped from ``self._background_tasks`` here instead of
        relying on the per-task ``done_callback`` to run later — the callback
        is scheduled separately on the loop and may not have fired by the time
        the caller inspects the set.
        """
        if not self._background_tasks:
            return
        pending = list(self._background_tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=_BACKGROUND_DRAIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            still_pending = [task for task in pending if not task.done()]
            logger.warning(
                "Background drain timed out for session %s; cancelling %d task(s)",
                session_id,
                len(still_pending),
            )
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)
        finally:
            for task in pending:
                self._background_tasks.discard(task)

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

    async def _maybe_emit_progress_checkin(
        self,
        session: Session,
        messages: list[dict],
        *,
        iteration_count: int,
        last_tool: str | None = None,
    ) -> None:
        """Emit an inbox progress check-in when the configured interval elapses."""

        interval = (session.config or {}).get("inbox_checkin_interval_seconds")
        if not interval:
            return
        try:
            interval_seconds = int(interval)
        except (TypeError, ValueError):
            return
        if interval_seconds <= 0:
            return

        latest = await self._store.last_event_at(
            session.id,
            EventType.INBOX_PROGRESS_CHECKIN,
        )
        created_at = session.created_at
        reference = latest or created_at
        if not isinstance(reference, datetime):
            return

        now = datetime.now(timezone.utc)
        if (now - _as_aware_utc(reference)).total_seconds() < interval_seconds:
            return

        await self._store.emit_event(
            session.id,
            EventType.INBOX_PROGRESS_CHECKIN,
            {
                "progress_summary": _last_assistant_message_excerpt(messages),
                "iterations": iteration_count,
                "last_tool": last_tool or "",
                "elapsed_seconds": _seconds_since(created_at),
            },
        )

    async def _drain_and_emit_turn_summary(
        self,
        *,
        session_id: UUID,
        turn_id: str,
        user_message: str,
    ) -> None:
        """Drain pending iteration summaries, then emit TURN_SUMMARY.

        Soft 10s cap on the drain so a hung iteration-summary task
        can't stall session completion. Same 10s cap on the turn
        summary call. Any failure is logged and swallowed — the SDK
        falls back to the per-iteration view when TURN_SUMMARY is
        missing.
        """
        if self._turn_summarizer is None:
            return

        pending = list(self._pending_iteration_summary_tasks.values())
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "iteration summary drain timed out for turn %s", turn_id,
                )

        # Read back the resolved iteration summaries in order so the
        # turn summarizer sees the same recap thread the SDK will
        # render. We re-query the event log because some iteration
        # tasks may have failed silently (returned None).
        try:
            iter_events = await self._store.get_events(
                session_id,
                types=[EventType.ITERATION_SUMMARY],
            )
        except Exception:
            logger.warning(
                "Failed to read iteration summaries for turn %s; "
                "summarizing without them.",
                turn_id,
                exc_info=True,
            )
            iter_events = []
        ordered = sorted(
            (
                e for e in iter_events
                if (getattr(e, "data", None) or {}).get("turn_id") == turn_id
            ),
            key=lambda e: (getattr(e, "data", None) or {}).get(
                "iteration_index", 0,
            ),
        )
        iteration_summaries = [
            str((getattr(e, "data", None) or {}).get("summary") or "")
            for e in ordered
        ]
        candidate_artifacts = await self._collect_candidate_artifacts(
            session_id=session_id, turn_id=turn_id,
        )

        try:
            result = await asyncio.wait_for(
                self._turn_summarizer.summarize_turn(
                    turn_id=turn_id,
                    user_message=user_message,
                    iteration_summaries=iteration_summaries,
                    candidate_artifacts=candidate_artifacts,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.warning("turn summary call timed out for %s", turn_id)
            return
        except Exception:
            logger.warning(
                "turn summary call failed for %s", turn_id, exc_info=True,
            )
            return
        if result is None:
            return

        try:
            await self._store.emit_event(
                session_id,
                EventType.TURN_SUMMARY,
                {
                    "turn_id": turn_id,
                    "recap": result.recap,
                    "artifacts": [
                        {"kind": a.kind, "label": a.label, "ref": a.ref}
                        for a in result.artifacts
                    ],
                },
            )
        except Exception:
            logger.warning(
                "Failed to emit TURN_SUMMARY for %s", turn_id, exc_info=True,
            )

    async def _collect_candidate_artifacts(
        self,
        *,
        session_id: UUID,
        turn_id: str,
    ) -> list[Any]:
        """Pull notable tool calls and artifacts emitted during this turn.

        Returns a list of ``TurnArtifact`` instances from
        :mod:`surogates.harness.turn_summarizer`. The summarizer
        curates this list further; this method's job is to surface
        every plausibly-relevant event so the LLM can pick.

        Invariant: this method MUST only be called at the end of the
        queried turn (i.e. from ``_drain_and_emit_turn_summary`` inside
        ``_complete_session``). Once we see the first event bearing
        ``turn_id``, every following event is treated as "in this
        turn" — TOOL_CALL events don't themselves carry ``turn_id``,
        so we rely on chronological adjacency to LLM events that do.
        Calling this method before the current turn ends, or for a
        turn that's not the LAST in the log, would incorrectly
        attribute later turns' tool calls to this one.
        """
        from surogates.harness.turn_summarizer import TurnArtifact

        out: list[TurnArtifact] = []
        try:
            # Scoped to the event types we actually inspect — keeps the
            # query cheap on long-running sessions with deep event logs.
            events = await self._store.get_events(
                session_id,
                types=[EventType.TOOL_CALL, EventType.ARTIFACT_CREATED,
                       EventType.LLM_REQUEST, EventType.LLM_RESPONSE],
            )
        except Exception:
            logger.debug(
                "Failed to read events for candidate artifacts on %s",
                session_id, exc_info=True,
            )
            return out

        in_turn = False
        terminal_commands: list[str] = []
        for evt in events:
            data = evt.data or {}
            if data.get("turn_id") == turn_id:
                in_turn = True
            if not in_turn:
                continue

            etype_str = evt.type.value if hasattr(evt.type, "value") else evt.type

            if etype_str == EventType.TOOL_CALL.value:
                # Tool-call payloads carry ``name`` and ``arguments`` per
                # the harness's TOOL_CALL emit contract; ``arguments``
                # is JSON-encoded for some tools, a dict for others.
                name = str(data.get("name") or "")
                raw_args = data.get("arguments")
                args = _coerce_tool_args(raw_args)
                tc_id = str(data.get("tool_call_id") or data.get("id") or "")

                if name in {"write_file", "patch"}:
                    path = (
                        args.get("path")
                        or args.get("file_path")
                        or args.get("name")
                        or ""
                    )
                    if isinstance(path, str) and path:
                        out.append(
                            TurnArtifact(kind="file", label=path, ref=path),
                        )
                elif name == "create_artifact":
                    label = args.get("name") or args.get("path") or ""
                    if isinstance(label, str) and label:
                        out.append(
                            TurnArtifact(
                                kind="artifact", label=label, ref=label,
                            ),
                        )
                elif name in {"web_extract", "web_crawl"}:
                    url = args.get("url") or ""
                    if isinstance(url, str) and url:
                        out.append(
                            TurnArtifact(kind="url", label=url, ref=url),
                        )
                elif name == "terminal":
                    cmd = args.get("command") or ""
                    if isinstance(cmd, str) and cmd and tc_id:
                        terminal_commands.append(cmd)
                        out.append(
                            TurnArtifact(
                                kind="command",
                                label=cmd[:80],
                                ref=tc_id,
                            ),
                        )
            elif etype_str == EventType.ARTIFACT_CREATED.value:
                artifact_id = str(
                    data.get("artifact_id") or data.get("id") or "",
                )
                name = str(data.get("name") or artifact_id or "")
                if artifact_id and name:
                    out.append(
                        TurnArtifact(
                            kind="artifact", label=name, ref=artifact_id,
                        ),
                    )

        # Workspace mtime scan — surfaces files created indirectly
        # (terminal scripts, execute_code) that don't show up in the
        # tool-call stream. Deduped against the paths already added
        # via write_file/patch so the same file isn't listed twice.
        try:
            workspace_candidates = await self._scan_workspace_for_new_files(
                session_id=session_id,
                already_seen_paths={
                    a.ref for a in out if a.kind == "file"
                },
            )
        except Exception:
            logger.debug(
                "Workspace mtime scan failed for %s",
                session_id, exc_info=True,
            )
            workspace_candidates = []
        out.extend(workspace_candidates)

        # Flag intermediate scripts: a file the agent wrote and then
        # ran via terminal is almost always scaffolding (e.g. a python
        # script used to generate the real deliverable), not a final
        # artifact the user wanted. Annotate so the summarizer LLM can
        # filter them out — we don't drop here because the user
        # occasionally does ask for code, and the LLM gets to make
        # that call against the user message.
        annotated: list[TurnArtifact] = []
        for art in out:
            if art.kind != "file":
                annotated.append(art)
                continue
            executed = any(art.ref in cmd for cmd in terminal_commands)
            if executed:
                meta = dict(art.meta or {})
                meta["executed_by_terminal"] = True
                annotated.append(TurnArtifact(
                    kind=art.kind,
                    label=art.label,
                    ref=art.ref,
                    meta=meta,
                ))
            else:
                annotated.append(art)
        return annotated

    async def _scan_workspace_for_new_files(
        self,
        *,
        session_id: UUID,
        already_seen_paths: set[str],
    ) -> list[Any]:
        """Return file candidates for workspace objects modified during
        the current turn (mtime >= ``self._turn_started_at``).

        Skips entries already surfaced via tool-call inspection
        (``already_seen_paths``) to avoid duplicates. Uses ``list_entries``
        so mtime/size come from the bulk list response — no per-key HEAD
        round trips.
        """
        from surogates.harness.turn_summarizer import TurnArtifact
        from surogates.storage.tenant import prefixed_session_workspace_prefix

        storage = self._storage
        if storage is None or self._turn_started_at is None:
            return []

        try:
            session = await self._store.get_session(session_id)
        except Exception:
            return []
        bucket = (session.config or {}).get("storage_bucket")
        if not bucket:
            return []
        root_id = (
            (session.config or {}).get("sandbox_root_session_id")
            or str(session.id)
        )
        prefix = prefixed_session_workspace_prefix(session.config, str(root_id))

        try:
            entries = await storage.list_entries(bucket, prefix=prefix)
        except Exception:
            logger.debug(
                "Workspace list_entries failed for bucket %r prefix %r",
                bucket, prefix, exc_info=True,
            )
            return []

        out: list[TurnArtifact] = []
        turn_start = self._turn_started_at
        for entry in entries:
            key = entry["key"]
            rel = key[len(prefix):] if key.startswith(prefix) else key
            if not rel or rel in already_seen_paths:
                continue
            modified = _coerce_modified_to_datetime(entry.get("modified"))
            if modified is None or modified < turn_start:
                continue
            out.append(
                TurnArtifact(kind="file", label=rel, ref=rel),
            )
        return out

    async def _complete_session(
        self,
        session: Session,
        messages: list[dict],
        lease: SessionLease,
        *,
        reason: str,
        through_event_id: int | None = None,
        cost_tracker: SessionCostTracker | None = None,
        turn_id: str | None = None,
        user_message: str | None = None,
    ) -> None:
        """Emit SESSION_COMPLETE and advance the cursor.

        When ``turn_id`` is supplied AND the completion reason represents
        a successful turn end (``stop``/``done``/``complete``/``completed``),
        drains any in-flight iteration-summary tasks and emits a
        ``TURN_SUMMARY`` event before ``SESSION_COMPLETE`` so the SDK
        sees the recap in the same event stream as the closing message.
        """
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

        # Emit TURN_SUMMARY (if applicable) BEFORE SESSION_COMPLETE so
        # late-arriving SSE subscribers see them in event-id order.
        if (
            turn_id is not None
            and self._turn_summarizer is not None
            and reason in {"stop", "done", "complete", "completed"}
        ):
            try:
                await self._drain_and_emit_turn_summary(
                    session_id=session.id,
                    turn_id=turn_id,
                    user_message=user_message
                    if user_message is not None
                    else _latest_user_message_text(messages),
                )
            except Exception:
                logger.exception(
                    "Turn summary drain failed for %s", session.id,
                )

        complete_data: dict[str, Any] = {
            "reason": reason,
            "worker_id": self._worker_id,
        }
        if cost_tracker is not None:
            complete_data["cost_summary"] = cost_tracker.summary()

        await self._store.emit_event(
            session.id,
            EventType.SESSION_COMPLETE,
            complete_data,
        )
        inbox_event_id = await self._store.emit_event(
            session.id,
            EventType.INBOX_TASK_COMPLETE,
            {
                "outcome": (
                    "success"
                    if reason in {"stop", "done", "complete", "completed"}
                    else reason
                ),
                "summary": _last_assistant_message_excerpt(messages),
                "duration_seconds": _seconds_since(session.created_at),
                "session_title": session.title or "Task complete",
                "error": None,
            },
        )
        try:
            await self._store.update_session_status(session.id, "completed")
        except Exception:
            logger.warning(
                "Failed to update session status to completed for %s",
                session.id,
                exc_info=True,
            )

        # Notify parent session if this is a worker (child) session.
        # Scheduled loop runs use parent_id for traceability in the session
        # tree, but should not wake the parent as if they were sub-agent work.
        if _should_notify_parent_on_completion(session):
            from surogates.harness.worker_notify import notify_parent_on_completion
            try:
                await notify_parent_on_completion(
                    session_store=self._store,
                    worker_session_id=session.id,
                    parent_session_id=session.parent_id,
                    org_id=str(session.org_id),
                    agent_id=session.agent_id,
                    redis=self._redis,
                    task_id=getattr(session, "task_id", None),
                    session_factory=self._session_factory,
                )
            except Exception:
                logger.warning(
                    "Failed to notify parent %s of worker %s completion",
                    session.parent_id, session.id,
                    exc_info=True,
                )

        await self._finalize_dynamic_loop_if_needed(session)

        # Advance cursor to the latest event.
        cursor_target = (
            through_event_id if through_event_id is not None else inbox_event_id
        )
        try:
            await self._store.advance_harness_cursor(
                session.id, cursor_target, lease.lease_token,
            )
        except Exception:
            logger.warning(
                "Failed to advance cursor after session completion for %s",
                session.id,
            )

    async def _finalize_dynamic_loop_if_needed(self, session: Session) -> None:
        if not session.config.get("scheduled_dynamic_loop"):
            return
        schedule_id_raw = session.config.get("scheduled_session_id")
        if not schedule_id_raw:
            return
        # Either the user or the service account that minted the schedule
        # may own the row.  Anonymous-channel sessions never reach here
        # (they cannot create schedules), but defensive check anyway.
        if self._tenant.user_id is None and self._tenant.service_account_id is None:
            return

        from surogates.scheduled.schedule import DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS
        from surogates.scheduled.store import ScheduledSessionStore

        try:
            schedule_id = UUID(str(schedule_id_raw))
        except ValueError:
            logger.warning("Invalid dynamic loop id in session config: %s", schedule_id_raw)
            return

        store = ScheduledSessionStore(self._session_factory)
        try:
            schedule = await store.get(schedule_id)
        except KeyError:
            return
        if schedule.next_run_at is not None or schedule.last_session_id != session.id:
            return

        await store.mark_dynamic_run_finished(
            schedule_id=schedule_id,
            org_id=self._tenant.org_id,
            user_id=self._tenant.user_id,
            service_account_id=self._tenant.service_account_id,
            agent_id=session.agent_id,
            session_id=session.id,
            delay_seconds=DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
            reason="The agent did not call loop_wait; using the fallback delay.",
        )


# ---------------------------------------------------------------------------
# Mission evaluator hook
#
# Called after every no-tool-call assistant response. The hook itself is
# cheap when no mission is active (one SELECT on `missions` for the
# session); a real evaluation only fires when ``should_evaluate``
# returns ``should=True``. See:
#   docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md
# ---------------------------------------------------------------------------


class MissionJudgeParseError(ValueError):
    """Raised when the mission judge returns non-JSON or malformed JSON.

    The harness hook records a parse-failure verdict on the mission row
    and emits a parse-failed evaluation.end event instead of treating
    the malformed response as a regular needs_revision verdict; three
    consecutive parse failures pause the mission per the
    :class:`~surogates.missions.store.MissionStore` contract.
    """


async def _maybe_run_mission_evaluator(
    *,
    session_id: UUID,
    coordinator_last_response: str | None,
    session_store: Any,
    session_factory: Any,
    mission_store: Any,
    judge: Any,
) -> None:
    """Run the mission evaluator iff the session has an active mission
    and a trigger condition fires.

    ``judge`` is an async callable ``(system_prompt, user_prompt) -> dict``
    that returns the parsed verdict JSON. Tests inject a stub; production
    wires it via :func:`_build_mission_judge` below.
    """
    from surogates.missions.evaluator import (
        apply_verdict,
        build_evaluator_prompt,
        evaluator_system_prompt,
        should_evaluate,
    )

    active = await mission_store.get_active_for_session(session_id)
    if active is None or active.status != "active":
        return

    decision = await should_evaluate(
        mission_id=active.id,
        coordinator_last_response=coordinator_last_response,
        session_factory=session_factory,
        mission_store=mission_store,
    )
    if not decision.should:
        return

    await session_store.emit_event(
        session_id, EventType.MISSION_EVALUATION_START,
        {
            "mission_id": str(active.id),
            "iteration": active.iteration,
            "trigger": decision.trigger,
        },
    )

    user_prompt = await build_evaluator_prompt(
        mission_id=active.id,
        coordinator_last_response=coordinator_last_response,
        session_factory=session_factory,
        mission_store=mission_store,
    )
    try:
        verdict = await judge(evaluator_system_prompt(), user_prompt)
    except MissionJudgeParseError as exc:
        failures = await mission_store.record_parse_failure(active.id)
        await session_store.emit_event(
            session_id, EventType.MISSION_EVALUATION_END,
            {
                "mission_id": str(active.id),
                "iteration": active.iteration,
                "trigger": decision.trigger,
                "result": "needs_revision",
                "explanation": "judge parse failure",
                "feedback": str(exc)[:500],
                "parse_failed": True,
                "parse_failures": failures,
            },
        )
        return
    except Exception as exc:
        # Transport-level failure (provider outage, rate limit, timeout).
        # Do NOT synthesize a needs_revision verdict — that would burn
        # one of the mission's max_iterations on something that wasn't a
        # real evaluator turn. Emit a transport-failed evaluation.end
        # event so the dashboard can surface the outage, and return.
        # The next no-tool-call response triggers another attempt; the
        # rate-limit guard prevents tight retries.
        logger.warning(
            "Mission %s evaluator judge call failed (transport): %s",
            active.id, exc,
        )
        await session_store.emit_event(
            session_id, EventType.MISSION_EVALUATION_END,
            {
                "mission_id": str(active.id),
                "iteration": active.iteration,
                "trigger": decision.trigger,
                "result": "transport_failed",
                "explanation": "judge call failed (transport)",
                "feedback": str(exc)[:500],
                "transport_failed": True,
            },
        )
        return

    await apply_verdict(
        mission_id=active.id,
        verdict=verdict,
        coordinator_session_id=session_id,
        session_store=session_store,
        mission_store=mission_store,
        trigger=decision.trigger,
    )


def _parse_judge_json(raw: str) -> dict[str, Any]:
    """Tolerant JSON extraction for the mission judge.

    Mirrors :meth:`AgentHarness._parse_json_object`: strips Markdown
    fences, falls back to the first ``{...}`` block in prose, and
    raises ``ValueError`` on empty / non-object payloads so the caller
    can surface the error as a ``MissionJudgeParseError``.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    if not text:
        raise ValueError("empty payload")
    # Some reasoning models prefix the JSON with their thought process.
    # Find the first balanced ``{ ... }`` block if the payload isn't
    # already a JSON object.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"judge returned non-object JSON: {type(parsed).__name__}")
    return parsed


class _MissionVerdict(BaseModel):
    """Structured shape the judge must return.

    Used both for ``outlines``-backed constrained generation (preferred,
    via :func:`generate_structured`) and for tolerant fallback parsing
    when outlines isn't installed or fails to coerce the model's output.
    Keeping the schema in one place means the prompt's documented JSON
    shape and the parser's expected shape stay in lockstep.
    """

    result: Literal["satisfied", "needs_revision", "blocked", "failed"]
    explanation: str = ""
    feedback: str = ""


def _build_mission_judge(*, llm_client: Any, eval_model: str) -> Any:
    """Return an async ``(system, user) -> dict`` judge bound to ``llm_client``.

    Prefers ``outlines``-backed structured generation against the
    :class:`_MissionVerdict` schema so the LLM cannot emit malformed
    JSON or omit required fields. Falls back to a free-form chat
    completion with tolerant JSON extraction when structured generation
    is unavailable (no outlines, provider doesn't support it) — the
    fallback also reads ``reasoning_content`` for reasoning-mode models
    (GLM, DeepSeek) that leave ``content`` empty.

    A parse / coercion failure raises :class:`MissionJudgeParseError`
    so :func:`_maybe_run_mission_evaluator` can distinguish parse vs.
    transport failure and record the right counter.
    """

    async def judge(system_prompt: str, user_prompt: str) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Preferred path: constrain the LLM to the verdict schema.
        # Returns None when outlines isn't available, the provider
        # isn't supported, or coercion fails — fall through to the
        # tolerant free-form parser in that case.
        try:
            verdict = await generate_structured(
                llm_client=llm_client,
                model=eval_model,
                messages=messages,
                output_model=_MissionVerdict,
                max_tokens=600,
                temperature=0,
            )
        except Exception as exc:
            logger.debug(
                "Mission judge structured generation raised %r; "
                "falling back to free-form JSON parsing",
                exc,
            )
            verdict = None
        if verdict is not None:
            return verdict.model_dump()

        # Fallback: free-form completion + tolerant parser.
        resp = await llm_client.chat.completions.create(
            model=eval_model,
            messages=messages,
            temperature=0.0,
            max_tokens=600,
        )
        try:
            message = resp.choices[0].message
        except (AttributeError, IndexError) as exc:
            raise MissionJudgeParseError(
                f"judge returned an unexpected shape: {exc}",
            ) from exc
        if isinstance(message, dict):
            raw = (
                message.get("content")
                or message.get("reasoning_content")
                or message.get("reasoning")
                or ""
            )
        else:
            raw = (
                getattr(message, "content", None)
                or getattr(message, "reasoning_content", None)
                or getattr(message, "reasoning", None)
                or ""
            )
        if not raw or not str(raw).strip():
            raise MissionJudgeParseError("judge returned empty content")
        try:
            parsed = _parse_judge_json(str(raw))
            # Validate against the verdict schema so the caller always
            # sees the documented shape (or a parse error, never a
            # silently-malformed dict).
            return _MissionVerdict.model_validate(parsed).model_dump()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MissionJudgeParseError(str(exc)) from exc

    return judge
