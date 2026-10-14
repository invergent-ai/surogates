"""Pure Slack Block Kit helpers for channel ask_user_question prompts."""

from __future__ import annotations

import json
from dataclasses import dataclass


ANSWER_ACTION_ID = "surogates_input_answer"
#: A choice tapped right in the message: ``surogates_input_choice_<index>``.
CHOICE_ACTION_PREFIX = "surogates_input_choice_"
#: At most this many choices, each this short, are buttons in the message; more, or longer, open the form.
INLINE_CHOICES, INLINE_LABEL_CHARS = 5, 40
MODAL_CALLBACK_ID = "surogates_input_modal"
OTHER_VALUE = "__other__"


@dataclass(frozen=True)
class ModalSubmission:
    session_id: str
    tool_call_id: str
    responses: list[dict]


@dataclass(frozen=True)
class ModalErrors:
    errors: dict[str, str]

    def to_response(self) -> dict:
        return {"response_action": "errors", "errors": self.errors}


def _plain(text: str, *, limit: int = 75) -> dict:
    return {"type": "plain_text", "text": (text or "")[:limit]}


def _mrkdwn(text: str, *, limit: int = 2900) -> dict:
    return {"type": "mrkdwn", "text": (text or "")[:limit]}


def inline_choices(questions: list[dict]) -> list[str] | None:
    """The labels to show as buttons in the message itself: one question with a few short choices
    (a confirmation, yes/no), so one tap answers it, as WhatsApp's reply buttons and Telegram's inline
    keyboard do; a typed reply instead reaches the agent as the person's message. ``None``: the question
    needs the form."""
    if len(questions) != 1:
        return None
    labels = [str(c.get("label") or "").strip() for c in questions[0].get("choices") or []]
    if not 1 <= len(labels) <= INLINE_CHOICES or not all(0 < len(x) <= INLINE_LABEL_CHARS for x in labels):
        return None
    return labels


def build_input_prompt_blocks(
    *,
    session_id: str,
    tool_call_id: str,
    questions: list[dict],
    context: str = "",
) -> tuple[str, list[dict]]:
    ref = {"session_id": str(session_id), "tool_call_id": str(tool_call_id)}
    labels = inline_choices(questions)
    if labels is not None:
        prompt = str(questions[0].get("prompt") or "").strip()
        body = "\n".join(p for p in ((context or "").strip(), prompt) if p) or "Choose one"
        buttons = [
            {"type": "button", "text": _plain(label), "action_id": f"{CHOICE_ACTION_PREFIX}{i}",
             "value": json.dumps({**ref, "choice": i}, separators=(",", ":")),
             **({"style": "primary"} if i == 0 else {})}
            for i, label in enumerate(labels)
        ]
        return prompt or body, [
            {"type": "section", "text": _mrkdwn(body)},
            {"type": "actions", "elements": buttons},
        ]
    first_prompt = (questions[0].get("prompt") if questions else "") or "I need your input"
    summary = (context or "").strip() or first_prompt
    text = "I need your input to continue."
    blocks = [
        {
            "type": "section",
            "text": _mrkdwn(f"*I need your input*\n{summary}"),
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": _plain("Answer"),
                    "style": "primary",
                    "action_id": ANSWER_ACTION_ID,
                    "value": json.dumps(ref, separators=(",", ":")),
                },
            ],
        },
    ]
    return text, blocks


def build_answered_blocks(responses: list[dict]) -> tuple[str, list[dict]]:
    """Render a resolved input prompt: the submitted answer(s), no button.

    Replaces the original Answer-button message via ``chat.update`` once the
    user submits the modal, so the thread shows what was answered instead of a
    now-dead button.  ``responses`` are the ``parse_modal_submission`` entries
    (each ``{"question", "answer", "is_other"}``).
    """
    blocks: list[dict] = [
        {"type": "section", "text": _mrkdwn("✅ *Answered*")},
    ]
    summary: list[str] = []
    for response in responses:
        question = (response.get("question") or "").strip()
        answer = (response.get("answer") or "").strip()
        body = f"*{question}*\n> {answer}" if question else f"> {answer}"
        blocks.append({"type": "section", "text": _mrkdwn(body)})
        summary.append(f"{question}: {answer}" if question else answer)
    text = "Answered — " + "; ".join(summary) if summary else "Answered"
    return text, blocks


def build_question_modal(
    *,
    session_id: str,
    tool_call_id: str,
    questions: list[dict],
    channel: str = "",
    message_ts: str = "",
) -> dict:
    blocks: list[dict] = []
    for index, question in enumerate(questions):
        prompt = question.get("prompt") or f"Question {index + 1}"
        choices = question.get("choices") or []
        allow_other = bool(question.get("allow_other", True))
        if choices:
            options = [
                {
                    "text": _plain(choice.get("label") or f"Choice {choice_index + 1}"),
                    "value": str(choice_index),
                }
                for choice_index, choice in enumerate(choices)
            ]
            if allow_other:
                options.append({"text": _plain("Other"), "value": OTHER_VALUE})
            blocks.append(
                {
                    "type": "input",
                    "block_id": f"q{index}_choice",
                    "label": _plain(prompt, limit=150),
                    "element": {
                        "type": "static_select",
                        "action_id": f"q{index}_choice",
                        "options": options,
                    },
                },
            )
            if allow_other:
                blocks.append(
                    {
                        "type": "input",
                        "block_id": f"q{index}_other",
                        "optional": True,
                        "label": _plain("Other (if selected above)", limit=150),
                        "element": {
                            "type": "plain_text_input",
                            "action_id": f"q{index}_other",
                        },
                    },
                )
        else:
            blocks.append(
                {
                    "type": "input",
                    "block_id": f"q{index}_other",
                    "label": _plain(prompt, limit=150),
                    "element": {
                        "type": "plain_text_input",
                        "action_id": f"q{index}_other",
                    },
                },
            )
    return {
        "type": "modal",
        "callback_id": MODAL_CALLBACK_ID,
        "private_metadata": json.dumps(
            {
                "session_id": str(session_id),
                "tool_call_id": str(tool_call_id),
                "channel": str(channel),
                "message_ts": str(message_ts),
            },
            separators=(",", ":"),
        ),
        "title": _plain("Answer", limit=24),
        "submit": _plain("Submit", limit=24),
        "close": _plain("Cancel", limit=24),
        "blocks": blocks,
    }


def parse_choice(action: dict, questions: list[dict]) -> tuple[str, str, list[dict]] | None:
    """A tapped choice button as ``(session_id, tool_call_id, responses)``; ``None`` when it names no choice
    of this question."""
    if not str(action.get("action_id") or "").startswith(CHOICE_ACTION_PREFIX):
        return None
    try:
        ref = json.loads(action.get("value") or "{}")
        choice = (questions[0].get("choices") or [])[int(ref["choice"])]
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    prompt = questions[0].get("prompt") or "Question 1"
    return (str(ref.get("session_id") or ""), str(ref.get("tool_call_id") or ""),
            [{"question": prompt, "answer": choice.get("label") or "", "is_other": False}])


def _value(values: dict, block_id: str) -> dict:
    return values.get(block_id, {}).get(block_id, {}) or {}


def parse_modal_submission(view: dict, questions: list[dict]) -> ModalSubmission | ModalErrors:
    meta = json.loads(view.get("private_metadata") or "{}")
    values = view.get("state", {}).get("values", {}) or {}
    errors: dict[str, str] = {}
    responses: list[dict] = []

    for index, question in enumerate(questions):
        prompt = question.get("prompt") or f"Question {index + 1}"
        choices = question.get("choices") or []
        choice_value = _value(values, f"q{index}_choice")
        other_value = (_value(values, f"q{index}_other").get("value") or "").strip()
        selected = choice_value.get("selected_option") or {}
        selected_value = selected.get("value")

        if choices and selected_value not in (None, OTHER_VALUE):
            try:
                choice = choices[int(selected_value)]
            except (TypeError, ValueError, IndexError):
                errors[f"q{index}_choice"] = "Choose one of the listed options."
                continue
            responses.append(
                {
                    "question": prompt,
                    "answer": choice.get("label") or "",
                    "is_other": False,
                },
            )
            continue

        if choices and selected_value is None:
            errors[f"q{index}_choice"] = "Choose an option."
            continue

        if not other_value:
            errors[f"q{index}_other"] = "Enter an answer for Other." if choices else "Enter an answer."
            continue

        # Typed answer.  Only "other" when there was a menu to depart
        # from — an open-ended question has no options to deviate from.
        responses.append(
            {"question": prompt, "answer": other_value, "is_other": bool(choices)},
        )

    if errors:
        return ModalErrors(errors)
    return ModalSubmission(
        session_id=str(meta.get("session_id") or ""),
        tool_call_id=str(meta.get("tool_call_id") or ""),
        responses=responses,
    )
