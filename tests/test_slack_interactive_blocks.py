import json

from surogates.channels.platforms.slack_interactive import (
    ANSWER_ACTION_ID,
    MODAL_CALLBACK_ID,
    ModalErrors,
    ModalSubmission,
    build_input_prompt_blocks,
    build_question_modal,
    parse_modal_submission,
)


Q_CHOICE = {
    "prompt": "Which color?",
    "choices": [{"label": "blue"}, {"label": "red"}],
    "allow_other": True,
}
Q_FREE = {"prompt": "Anything else?", "allow_other": True}


def test_prompt_blocks_have_answer_button_with_ids():
    text, blocks = build_input_prompt_blocks(
        session_id="s1",
        tool_call_id="tc1",
        questions=[Q_CHOICE],
        context="ctx",
    )

    assert "input" in text.lower()
    button = blocks[-1]["elements"][0]
    assert button["action_id"] == ANSWER_ACTION_ID
    assert json.loads(button["value"]) == {"session_id": "s1", "tool_call_id": "tc1"}


def test_modal_has_deterministic_ids_and_metadata():
    view = build_question_modal(
        session_id="s1",
        tool_call_id="tc1",
        questions=[Q_CHOICE, Q_FREE],
    )

    assert view["type"] == "modal"
    assert view["callback_id"] == MODAL_CALLBACK_ID
    assert json.loads(view["private_metadata"]) == {"session_id": "s1", "tool_call_id": "tc1"}
    block_ids = [b["block_id"] for b in view["blocks"]]
    assert block_ids == ["q0_choice", "q0_other", "q1_other"]

    select = view["blocks"][0]["element"]
    assert select["action_id"] == "q0_choice"
    assert [o["value"] for o in select["options"]] == ["0", "1", "__other__"]
    assert view["blocks"][1]["optional"] is True
    assert view["blocks"][2].get("optional") is not True


def test_parse_choice_answer_uses_original_question_and_label():
    view = {
        "private_metadata": json.dumps({"session_id": "s1", "tool_call_id": "tc1"}),
        "state": {"values": {"q0_choice": {"q0_choice": {
            "type": "static_select",
            "selected_option": {"text": {"text": "blu"}, "value": "0"},
        }}}},
    }

    parsed = parse_modal_submission(view, [Q_CHOICE])

    assert isinstance(parsed, ModalSubmission)
    assert parsed.session_id == "s1"
    assert parsed.tool_call_id == "tc1"
    assert parsed.responses == [
        {"question": "Which color?", "answer": "blue", "is_other": False},
    ]


def test_parse_other_requires_text_when_other_selected():
    view = {
        "private_metadata": json.dumps({"session_id": "s1", "tool_call_id": "tc1"}),
        "state": {"values": {
            "q0_choice": {"q0_choice": {
                "type": "static_select",
                "selected_option": {"text": {"text": "Other"}, "value": "__other__"},
            }},
            "q0_other": {"q0_other": {"type": "plain_text_input", "value": ""}},
        }},
    }

    parsed = parse_modal_submission(view, [Q_CHOICE])

    assert isinstance(parsed, ModalErrors)
    assert parsed.to_response() == {
        "response_action": "errors",
        "errors": {"q0_other": "Enter an answer for Other."},
    }


def test_parse_free_text_answer():
    view = {
        "private_metadata": json.dumps({"session_id": "s1", "tool_call_id": "tc1"}),
        "state": {"values": {"q0_other": {"q0_other": {
            "type": "plain_text_input",
            "value": "use a tunnel",
        }}}},
    }

    parsed = parse_modal_submission(view, [Q_FREE])

    assert isinstance(parsed, ModalSubmission)
    assert parsed.responses == [
        {"question": "Anything else?", "answer": "use a tunnel", "is_other": True},
    ]
