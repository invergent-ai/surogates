"""Pure Slack Block Kit helpers for channel ask_user_question prompts."""

from __future__ import annotations

import json
from dataclasses import dataclass


ANSWER_ACTION_ID = "surogates_input_answer"
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


def build_input_prompt_blocks(
    *,
    session_id: str,
    tool_call_id: str,
    questions: list[dict],
    context: str = "",
) -> tuple[str, list[dict]]:
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
                    "value": json.dumps(
                        {"session_id": str(session_id), "tool_call_id": str(tool_call_id)},
                        separators=(",", ":"),
                    ),
                },
            ],
        },
    ]
    return text, blocks


def build_question_modal(
    *,
    session_id: str,
    tool_call_id: str,
    questions: list[dict],
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
            {"session_id": str(session_id), "tool_call_id": str(tool_call_id)},
            separators=(",", ":"),
        ),
        "title": _plain("Answer", limit=24),
        "submit": _plain("Submit", limit=24),
        "close": _plain("Cancel", limit=24),
        "blocks": blocks,
    }


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

        responses.append({"question": prompt, "answer": other_value, "is_other": True})

    if errors:
        return ModalErrors(errors)
    return ModalSubmission(
        session_id=str(meta.get("session_id") or ""),
        tool_call_id=str(meta.get("tool_call_id") or ""),
        responses=responses,
    )
