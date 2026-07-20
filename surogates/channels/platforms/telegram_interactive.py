"""Inline-keyboard helpers for Telegram ``ask_user_question`` prompts.

Telegram has no modal surface, so the interaction model differs from the
Slack Block Kit flow:

- A single question with choices renders as an ``inline_keyboard`` — one
  button per choice.  ``callback_data`` is capped at 64 bytes by Telegram,
  so a button carries only ``si:<session_id>:<question>:<choice>``
  (≤ 48 bytes); the question text and answer are re-resolved from the
  durable pending-input record when the button is tapped.
- Free-text questions (and multi-question prompts) render as text; the
  visitor answers by simply replying, which the inbound pipeline resolves
  against the same durable record.
"""

from __future__ import annotations

import html

__all__ = [
    "CALLBACK_PREFIX",
    "build_input_prompt",
    "parse_callback_data",
    "build_answered_text",
    "resolve_text_answer",
]

CALLBACK_PREFIX = "si"

_FREE_TEXT_HINT = "Reply to this message to answer in your own words."


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def build_input_prompt(
    *,
    session_id: str,
    questions: list[dict],
    context: str = "",
) -> tuple[str, str, dict | None]:
    """Render an input prompt as ``(html_text, plain_text, reply_markup)``.

    ``reply_markup`` is an ``inline_keyboard`` dict when the prompt is a
    single choice-question, else ``None``.
    """
    lines_html: list[str] = ["❓ <b>I need your input</b>"]
    lines_plain: list[str] = ["❓ I need your input"]
    if context.strip():
        lines_html.append(_esc(context.strip()))
        lines_plain.append(context.strip())

    for index, question in enumerate(questions):
        prompt = question.get("prompt") or f"Question {index + 1}"
        prefix = f"{index + 1}. " if len(questions) > 1 else ""
        lines_html.append(f"{prefix}<b>{_esc(prompt)}</b>")
        lines_plain.append(f"{prefix}{prompt}")
        choices = question.get("choices") or []
        if choices and len(questions) > 1:
            # Buttons only work for the single-question form; list the
            # options inline for multi-question prompts.
            for choice in choices:
                label = choice.get("label") or ""
                lines_html.append(f"  • {_esc(label)}")
                lines_plain.append(f"  • {label}")

    reply_markup: dict | None = None
    single = questions[0] if len(questions) == 1 else None
    choices = (single or {}).get("choices") or []
    if single is not None and choices:
        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": (choice.get("label") or f"Choice {ci + 1}")[:64],
                        "callback_data": f"{CALLBACK_PREFIX}:{session_id}:0:{ci}",
                    }
                ]
                for ci, choice in enumerate(choices)
            ]
        }
        if single.get("allow_other", True):
            lines_html.append(_esc(_FREE_TEXT_HINT))
            lines_plain.append(_FREE_TEXT_HINT)
    else:
        lines_html.append(_esc(_FREE_TEXT_HINT))
        lines_plain.append(_FREE_TEXT_HINT)

    return "\n".join(lines_html), "\n".join(lines_plain), reply_markup


def parse_callback_data(data: str) -> tuple[str, int, int] | None:
    """Parse ``si:<session_id>:<question>:<choice>`` callback data.

    Returns ``(session_id, question_index, choice_index)`` or ``None`` for
    anything that is not a well-formed input callback.
    """
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX:
        return None
    session_id, q_raw, c_raw = parts[1], parts[2], parts[3]
    if not session_id:
        return None
    try:
        return session_id, int(q_raw), int(c_raw)
    except ValueError:
        return None


def build_answered_text(responses: list[dict]) -> str:
    """Render the resolved prompt (HTML) that replaces the button message."""
    lines = ["✅ <b>Answered</b>"]
    for response in responses:
        question = (response.get("question") or "").strip()
        answer = (response.get("answer") or "").strip()
        if question:
            lines.append(f"<b>{_esc(question)}</b>: {_esc(answer)}")
        else:
            lines.append(_esc(answer))
    return "\n".join(lines)


def resolve_text_answer(questions: list[dict], text: str) -> list[dict]:
    """Map a free-text reply onto the pending questions.

    A reply that exactly matches a choice label (case-insensitive) selects
    that choice; anything else is recorded as an "other" answer.  Multi-
    question prompts get the whole reply attached to each question — the
    agent asked them as one message and receives the answer the same way.
    """
    stripped = (text or "").strip()
    responses: list[dict] = []
    for index, question in enumerate(questions):
        prompt = question.get("prompt") or f"Question {index + 1}"
        matched = None
        for choice in question.get("choices") or []:
            label = (choice.get("label") or "").strip()
            if label and label.lower() == stripped.lower():
                matched = label
                break
        responses.append(
            {
                "question": prompt,
                "answer": matched or stripped,
                "is_other": matched is None,
            }
        )
    return responses
