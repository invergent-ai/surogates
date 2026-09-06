"""The text tool-call protocol between the orchestrator and the agent.

Upstream binds the domain tools natively into its own agent loop. A prod
harness session cannot be handed synthetic tool schemas, so the
orchestrator speaks a strict text protocol instead: the first message
carries upstream's agent system prompt (verbatim, including the
CONVERSATION_COMPLETE convention) plus the tool catalog and the calling
format; the agent emits fenced ``tool_call`` blocks and receives TOOL
RESULTS messages back. A documented scaffold difference -- it measures
the harness chat loop and the model's tool selection, not the harness's
native tool router.
"""
from __future__ import annotations

import json
import re
from typing import Any

from galbench.dataset import Scenario

# Verbatim from v2/evaluate/config.py (rungalileo/agent-leaderboard).
DOMAIN_SPECIFIC_INSTRUCTIONS = {
    "banking": """You are a Banking Assistant helping customers with banking needs.
Your job is to use available tools to check balances, process transfers, manage accounts, and handle transactions.
Complete banking operations directly rather than just providing guidance.
You can accomplish most banking tasks using the tools provided.""",
    "healthcare": """You are a Healthcare Assistant helping patients manage healthcare needs.
Your job is to use available tools to access patient records, schedule appointments, and provide health information.
Complete healthcare actions directly rather than just providing guidance.
You can accomplish most healthcare tasks using the tools provided.""",
    "investment": """You are an Investment Assistant helping customers with investment needs.
Your job is to use available tools to manage portfolios, research investment options, execute trades, and track performance.
Complete investment operations directly rather than just providing guidance.
For transactions: collect required information, then execute using tools.
You can accomplish most investment tasks using the tools provided.""",
    "telecom": """You are a Telecommunications Assistant helping customers with service needs.
Your job is to use available tools to troubleshoot connection issues, change plans, and manage account services.
Solve problems directly using tools, especially for frustrated customers.
For technical issues: gather specific details, then use diagnostic tools to identify and resolve problems.
You can accomplish most telecom tasks using the tools provided.""",
    "insurance": """You are an Insurance Assistant helping clients manage insurance needs.
Your job is to use available tools to check policies, process claims, and update coverage.
Execute insurance-related tasks directly rather than just explaining processes.
For claims: gather incident details, verify coverage, then submit and track using tools.
You can accomplish most insurance tasks using the tools provided.""",
}

# Adapted from upstream's AGENT_SYSTEM_PROMPT: identical rules, plus the
# explicit calling format that replaces native tool binding.
PREAMBLE_TEMPLATE = """{domain_instructions}

Important:
- You have access to a set of tools that you can use to help the user. \
Use tools whenever you can to complete the task. Use multiple tools in \
sequence when needed to complete a request.
- Ask clarifying questions for ambiguous requests before using the tools.
- Make sure to get the *required* information to call the tool as per \
the tool's parameters and constraints.
- For unsupported requests, respond with a brief explanation on why you \
cannot help the user.
- Do not assume or make up things you don't know explicitly.
- Do not give any generic advice or do things you are not asked to do.
- If you do not know the answer, say you do not know.
- When you have completed all the user's requests and there is nothing \
more to do, end your response with 'CONVERSATION_COMPLETE'.

TOOLS AVAILABLE TO YOU (call them, do not describe them):
{tool_catalog}

HOW TO CALL A TOOL: emit one fenced block per call, exactly like this,
then stop and wait for the results before continuing:

```tool_call
{{"tool_name": "<name>", "tool_args": {{"<param>": <value>}}}}
```

You may emit several tool_call blocks in one reply; they run in order.
Never invent tool results -- wait for the TOOL RESULTS message. Do not
use any tools other than the ones listed above.

USER: {first_message}"""

_TOOL_CALL_RE = re.compile(
    r"```tool_call\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
)
COMPLETE_MARKER = "CONVERSATION_COMPLETE"


def tool_catalog(scenario: Scenario) -> str:
    catalog = []
    for tool in scenario.tools:
        catalog.append({
            "name": tool["title"],
            "description": tool["description"],
            "parameters": {
                "type": tool.get("type", "object"),
                "properties": tool["properties"],
                "required": list(tool["required"]),
            },
        })
    return json.dumps(catalog, ensure_ascii=False, indent=1)


def build_preamble(scenario: Scenario) -> str:
    return PREAMBLE_TEMPLATE.format(
        domain_instructions=DOMAIN_SPECIFIC_INSTRUCTIONS[scenario.domain],
        tool_catalog=tool_catalog(scenario),
        first_message=scenario.first_message.strip(),
    )


def parse_tool_calls(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract tool_call blocks. Returns (calls, malformed-block errors).

    A malformed block is surfaced back to the agent as an error result
    rather than silently dropped -- upstream's native binding would have
    rejected it too, and the judge should see the miss.
    """
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    for block in _TOOL_CALL_RE.findall(text or ""):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError as exc:
            errors.append(f"invalid JSON in tool_call block: {exc}")
            continue
        if not isinstance(data, dict) or not data.get("tool_name"):
            errors.append("tool_call block missing tool_name")
            continue
        args = data.get("tool_args")
        calls.append({
            "tool_name": str(data["tool_name"]),
            "tool_args": args if isinstance(args, dict) else {},
        })
    return calls, errors


def is_complete(text: str) -> bool:
    return COMPLETE_MARKER in (text or "")


def tool_results_message(results: list[dict[str, Any]]) -> str:
    lines = ["TOOL RESULTS:"]
    for r in results:
        lines.append(json.dumps(r, ensure_ascii=False, default=str))
    lines.append(
        "\nContinue helping the user based on these results. Remember to "
        f"end with {COMPLETE_MARKER} once every request is done."
    )
    return "\n".join(lines)
