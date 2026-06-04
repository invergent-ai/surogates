"""Vision compatibility helpers for model calls in the harness loop."""

from __future__ import annotations

import logging
from typing import Any

from surogates.harness.message_utils import message_to_dict

logger = logging.getLogger(__name__)

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
