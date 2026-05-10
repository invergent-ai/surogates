"""Automatic short session title generation."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_TITLE_PROMPT = (
    "Generate a short, descriptive title (3-7 words) for a conversation "
    "that starts with the following exchange. Capture the main topic or "
    "intent. Return ONLY the title text, no quotes, no prefix, and no "
    "punctuation at the end."
)

_MAX_TITLE_CHARS = 80


def clean_generated_title(raw_title: str | None) -> str | None:
    """Normalize an LLM-generated session title."""
    title = (raw_title or "").strip()
    if not title:
        return None

    title = title.strip("\"'` ")
    if title.lower().startswith("title:"):
        title = title[6:].strip()
    title = title.rstrip(" \t\r\n.。!?！؟")

    if len(title) > _MAX_TITLE_CHARS:
        title = title[: _MAX_TITLE_CHARS - 3].rstrip() + "..."
    return title or None


async def generate_session_title(
    *,
    llm_client: Any,
    model: str,
    user_message: str,
    assistant_response: str,
    timeout: float = 30.0,
) -> str | None:
    """Generate a short title from the first user/assistant exchange."""
    user_snippet = (user_message or "")[:500]
    assistant_snippet = (assistant_response or "")[:500]
    if not user_snippet or not assistant_snippet:
        return None

    messages = [
        {"role": "system", "content": _TITLE_PROMPT},
        {
            "role": "user",
            "content": (
                f"User: {user_snippet}\n\n"
                f"Assistant: {assistant_snippet}"
            ),
        },
    ]
    kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": 32,
        "temperature": 0.3,
        "timeout": timeout,
        "stream": False,
    }
    extra_body = _title_extra_body(model)
    if extra_body is not None:
        kwargs["extra_body"] = extra_body

    try:
        response = await llm_client.chat.completions.create(
            **kwargs,
        )
        content = response.choices[0].message.content
        return clean_generated_title(content)
    except Exception as exc:
        if _looks_like_optional_param_rejection(exc):
            try:
                retry_kwargs = {
                    key: value
                    for key, value in kwargs.items()
                    if key not in {"max_tokens", "temperature", "extra_body"}
                }
                response = await llm_client.chat.completions.create(
                    **retry_kwargs,
                )
                content = response.choices[0].message.content
                return clean_generated_title(content)
            except Exception as retry_exc:
                logger.warning(
                    "Session title generation failed after compatibility retry: %s",
                    retry_exc,
                )
                logger.debug(
                    "Session title generation retry traceback",
                    exc_info=True,
                )
                return None
        logger.warning("Session title generation failed: %s", exc)
        logger.debug("Session title generation traceback", exc_info=True)
        return None


def _looks_like_optional_param_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "max_tokens" in text
        or "temperature" in text
        or "extra_body" in text
        or "chat_template_kwargs" in text
        or "unsupported parameter" in text
        or "unexpected keyword" in text
        or "unrecognized request argument" in text
    )


def _title_extra_body(model: str) -> dict[str, Any] | None:
    if model.lower() == "surogate":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return None


async def maybe_generate_session_title(
    *,
    store: Any,
    llm_client: Any,
    session: Any,
    messages: list[dict[str, Any]],
    assistant_message: dict[str, Any],
    model: str,
) -> str | None:
    """Generate and persist a title when this is an early untitled exchange."""
    if getattr(session, "title", None):
        return None

    assistant_content = _content_as_text(assistant_message.get("content", ""))
    if not assistant_content.strip():
        return None

    user_messages = [
        _content_as_text(message.get("content", ""))
        for message in messages
        if message.get("role") == "user"
    ]
    if not user_messages or len(user_messages) > 2:
        return None

    title = await generate_session_title(
        llm_client=llm_client,
        model=model,
        user_message=user_messages[-1],
        assistant_response=assistant_content,
    )
    if not title:
        return None

    updated = await store.update_session_title_if_empty(
        _session_id(session),
        title,
    )
    return title if updated else None


def _content_as_text(content: Any) -> str:
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


def _session_id(session: Any) -> UUID:
    session_id = getattr(session, "id")
    return session_id if isinstance(session_id, UUID) else UUID(str(session_id))
