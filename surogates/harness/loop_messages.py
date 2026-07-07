"""Message and session-note helpers for the harness loop."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from surogates.session.events import EventType

if TYPE_CHECKING:
    from surogates.browser.control import BrowserControlStore

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

    The Platform Copilot attaches a ``view_context`` describing the Studio
    page the user is looking at. The current shape carries ``page`` plus a
    ``docPath`` (the product-doc that explains that screen) and an optional
    ``resource``; older Studio builds send a flat ``{kind, id, name}`` — both
    are rendered so a version skew never drops the note.

    Returns ``None`` when *metadata* is missing, not a dict, lacks
    ``view_context``, or the payload is malformed.
    """
    if not isinstance(metadata, dict):
        return None
    view_context = metadata.get("view_context")
    if not isinstance(view_context, dict):
        return None

    # Current shape: a page descriptor with the doc that explains it.
    if view_context.get("page"):
        return _page_view_context_note(view_context)

    # Backward-compatible flat resource shape.
    kind = view_context.get("kind")
    target_id = view_context.get("id")
    if not kind or not target_id:
        return None
    note = f"The user is currently viewing **{kind}** {target_id}"
    name = view_context.get("name")
    if name:
        note += f" ({name})"
    return note + "."


def _page_view_context_note(view_context: dict[str, Any]) -> str:
    """Render the page-descriptor form of a ``view_context`` payload."""
    note = f"The user is currently on the **{view_context.get('page')}** page"
    mode = view_context.get("mode")
    if mode:
        note += f" ({mode} mode)"
    tab = view_context.get("tab")
    if tab:
        note += f' on the "{tab}" tab'
    note += "."

    doc_path = view_context.get("docPath")
    if doc_path:
        note += (
            " To explain this page, its settings, or concepts, read its "
            f'documentation: call read_doc("{doc_path}").'
        )

    resource = view_context.get("resource")
    if isinstance(resource, dict) and resource.get("kind") and resource.get("id"):
        name = resource.get("name")
        named = f' "{name}"' if name else ""
        note += f" They are viewing {resource['kind']}{named} (id {resource['id']})."
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


def _latest_user_event_data(events: list[Any]) -> dict | None:
    """Return the ``data`` payload of the latest ``USER_MESSAGE`` event.

    Used by the slash-skill / ``/deep-research`` rewrite paths to refold the
    current turn's attachment note, inlined content, and image blocks onto
    the rewritten message body via
    :func:`surogates.harness.loop_context_replay.build_user_message_dict`.

    Returns ``None`` when no ``USER_MESSAGE`` event exists or its payload is
    not a dict.
    """
    for event in reversed(events):
        event_type = event.type
        type_value = (
            event_type.value if hasattr(event_type, "value") else event_type
        )
        if type_value != EventType.USER_MESSAGE.value:
            continue
        return event.data if isinstance(event.data, dict) else None
    return None


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


def _is_scheduled_run(session: Any) -> bool:
    """True when *session* is a scheduled/loop run.

    A run is identified by ``channel == "scheduled"`` or a
    ``scheduled_session_id`` config marker (the latter guards the case where a
    run's channel drifts from ``"scheduled"``). Both the loop-result surfacing
    and the parent-notify suppression key off this single predicate so they can
    never disagree.
    """
    if session.channel == "scheduled":
        return True
    return bool((getattr(session, "config", None) or {}).get("scheduled_session_id"))


def _should_notify_parent_on_completion(session: Any) -> bool:
    return session.parent_id is not None and not _is_scheduled_run(session)
