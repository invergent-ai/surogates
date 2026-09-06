"""One scenario end-to-end through fake harness + fake simulators."""
import json

from galbench.client import Event
from galbench.dataset import Scenario
from galbench.runner import run_scenario


def _scenario():
    return Scenario(
        scenario_id="banking-000",
        domain="banking",
        persona={"name": "Margaret", "tone": "formal"},
        first_message="Please check my balance.",
        user_goals=("Check balance",),
        tools=({
            "title": "get_account_balance",
            "description": "Retrieves balance information.",
            "type": "object",
            "properties": {"account_number": {"type": "string"}},
            "required": ["account_number"],
            "response_schema": {"type": "object",
                                "properties": {"balance": {"type": "number"}}},
        },),
    )


class ScriptedClient:
    """Harness whose agent follows a fixed script of replies."""

    def __init__(self, replies: list[str]):
        self.replies = replies
        self.sent: list[str] = []
        self._event_id = 0

    async def create_session(self):
        return "sess-1"

    async def send_message(self, session_id, content):
        self.sent.append(content)
        return 1

    async def stream_events(self, session_id, after=0):
        if self.replies:
            reply = self.replies.pop(0)
            self._event_id += 10
            ev = Event(self._event_id, "llm.response",
                       {"message": {"content": reply}})
            if ev.id > after:
                yield ev

    async def get_session_status(self, session_id):
        return "completed"


async def fake_chat(messages):
    prompt = messages[-1]["content"]
    if "tool simulator" in prompt:
        return json.dumps({"balance": 1234.56})
    return "Thanks, that is everything."


async def test_tool_call_round_then_completion():
    client = ScriptedClient([
        'Checking now.\n```tool_call\n{"tool_name": "get_account_balance", '
        '"tool_args": {"account_number": "99"}}\n```',
        "Your balance is $1,234.56. CONVERSATION_COMPLETE",
    ])
    result = await run_scenario(client, fake_chat, _scenario())

    assert result.error is None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].result == {"balance": 1234.56}
    assert result.tool_calls[0].known_tool is True
    # preamble, then the TOOL RESULTS message
    assert len(client.sent) == 2
    assert client.sent[1].startswith("TOOL RESULTS:")
    assert result.completed_marker is True
    assert result.user_turns == 0
    roles = [t["role"] for t in result.transcript]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_user_simulator_turn_when_no_tools():
    client = ScriptedClient([
        "Could you give me your account number?",
        "Thanks. CONVERSATION_COMPLETE",
    ])
    result = await run_scenario(client, fake_chat, _scenario())
    assert result.user_turns == 1
    assert result.completed_marker is True
    # The simulated user's reply was sent to the session.
    assert client.sent[-1] == "Thanks, that is everything."


async def test_unknown_tool_gets_error_result():
    client = ScriptedClient([
        '```tool_call\n{"tool_name": "not_a_tool", "tool_args": {}}\n```',
        "Sorry about that. CONVERSATION_COMPLETE",
    ])
    result = await run_scenario(client, fake_chat, _scenario())
    assert result.tool_calls[0].known_tool is False
    assert "Unknown tool" in result.tool_calls[0].result["error"]


async def test_harness_failure_recorded_not_raised():
    class Exploding(ScriptedClient):
        async def create_session(self):
            raise RuntimeError("harness down")

    result = await run_scenario(Exploding([]), fake_chat, _scenario())
    assert result.terminal_status == "error"
    assert "harness down" in result.error
