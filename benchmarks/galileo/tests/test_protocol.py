"""Preamble construction and tool-call parsing."""
from galbench.dataset import Scenario
from galbench.protocol import (
    build_preamble,
    is_complete,
    parse_tool_calls,
    tool_results_message,
)


def _scenario():
    return Scenario(
        scenario_id="banking-000",
        domain="banking",
        persona={"name": "Margaret"},
        first_message="I need to check my balance.",
        user_goals=("Check balance", "Report lost card"),
        tools=({
            "title": "get_account_balance",
            "description": "Retrieves balance information.",
            "type": "object",
            "properties": {"account_number": {"type": "string"}},
            "required": ["account_number"],
            "response_schema": {"type": "object"},
        },),
    )


def test_preamble_carries_instructions_catalog_and_first_message():
    text = build_preamble(_scenario())
    assert "Banking Assistant" in text
    assert "get_account_balance" in text
    assert "```tool_call" in text
    assert "CONVERSATION_COMPLETE" in text
    assert text.rstrip().endswith("I need to check my balance.")


def test_parse_tool_calls_single_and_multiple():
    text = (
        'Let me check.\n```tool_call\n{"tool_name": "get_account_balance", '
        '"tool_args": {"account_number": "123"}}\n```\n'
        '```tool_call\n{"tool_name": "second_tool"}\n```'
    )
    calls, errors = parse_tool_calls(text)
    assert errors == []
    assert calls == [
        {"tool_name": "get_account_balance",
         "tool_args": {"account_number": "123"}},
        {"tool_name": "second_tool", "tool_args": {}},
    ]


def test_parse_tool_calls_surfaces_malformed_blocks():
    calls, errors = parse_tool_calls(
        "```tool_call\nnot json\n```\n"
        '```tool_call\n{"tool_args": {}}\n```'
    )
    assert calls == []
    assert len(errors) == 2
    assert "invalid JSON" in errors[0]
    assert "missing tool_name" in errors[1]


def test_no_tool_calls_in_plain_reply():
    calls, errors = parse_tool_calls("Your balance is $100. CONVERSATION_COMPLETE")
    assert calls == [] and errors == []
    assert is_complete("done. CONVERSATION_COMPLETE")
    assert not is_complete("still working")


def test_tool_results_message_roundtrips_results():
    msg = tool_results_message([
        {"tool_name": "get_account_balance", "response": {"balance": 5}}
    ])
    assert msg.startswith("TOOL RESULTS:")
    assert '"balance": 5' in msg
    assert "CONVERSATION_COMPLETE" in msg
