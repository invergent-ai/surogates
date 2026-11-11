"""Which external tool calls a simulated conversation may really make.

A simulation plays made-up people against the real agent.  Its MCP tools
that only read run for real, so the agent works with real data; the ones
that change something (send, create, pay...) are not made and get a
plausible "done" back, with what would have happened kept for the trace.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from surogates.channels.memory_boundary import SIMULATION_BOUNDARY_PREFIX

logger = logging.getLogger(__name__)

WRITE_VERBS = frozenset(
    "SEND CREATE ADD UPDATE EDIT PATCH DELETE REMOVE POST PUT PAY CHARGE REFUND CANCEL BOOK "
    "INVITE SHARE UPLOAD MOVE ARCHIVE REPLY FORWARD SCHEDULE SET INSERT WRITE".split()
)
READ_VERBS = frozenset(
    "GET LIST FETCH SEARCH FIND READ QUERY LOOKUP RETRIEVE DESCRIBE CHECK COUNT EXPORT".split()
)

_ROUTER_PREFIXES = ("mcp__tool_router__", "mcp__composio_tool_router__")
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_JUDGE_TIMEOUT_S = 30
_CLIP = 300

Judge = Callable[[str], Awaitable[bool]]

# ponytail: per-process, unbounded; tool names per worker are few.
_decided: dict[str, bool] = {}


def is_simulated(config: dict | None) -> bool:
    config = config or {}
    # A sub-agent inherits its parent's memory boundary but not the run id.
    return bool(config.get("simulation_run_id")) or str(config.get("memory_boundary") or "").startswith(
        SIMULATION_BOUNDARY_PREFIX
    )


def changes_something(name: str, annotations: dict | None) -> bool | None:
    """True/False when the tool's hints or verb say; ``None`` when they don't."""
    annotations = annotations or {}
    if annotations.get("readOnlyHint") is True:
        return False
    if annotations.get("destructiveHint") is True or annotations.get("readOnlyHint") is False:
        return True
    tool = name.rsplit("__", 1)[-1]
    for word in (w.upper() for part in tool.split("_") for w in _WORDS.findall(part)):
        if word in WRITE_VERBS:
            return True
        if word in READ_VERBS:
            return False
    return None


async def _cached(key: str, name: str, annotations: dict | None, llm: Judge | None) -> bool:
    known = changes_something(name, annotations)
    if known is not None:
        return known
    if key in _decided:
        return _decided[key]
    if llm is None:
        return True
    try:
        verdict = bool(await asyncio.wait_for(llm(name), _JUDGE_TIMEOUT_S))
    except Exception:
        logger.warning("Could not classify tool %s; faking it in the simulation", name, exc_info=True)
        return True
    _decided[key] = verdict
    return verdict


async def decide(name: str, annotations: dict | None, *, llm: Judge | None, args: Any = None) -> bool:
    """Whether calling *name* would change something; any doubt counts as yes."""
    if name.startswith(_ROUTER_PREFIXES):
        tool = name.rsplit("__", 1)[-1].upper()
        # The router's executors run any app tool, or code that can: the
        # router tool's name says nothing, the tools it runs do.
        if "WORKBENCH" in tool or "BASH" in tool:
            return True
        if "EXECUTE" in tool:
            slugs = _router_slugs(args)
            if not slugs:
                return True
            for slug in slugs:
                if await _cached(f"router:{slug}", slug, None, llm):
                    return True
            return False
    return await _cached(name, name, annotations, llm)


def _router_slugs(args: Any) -> list[str]:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    calls = args.get("tools") if isinstance(args, dict) else None
    if not isinstance(calls, list):
        return []
    slugs = [c.get("tool_slug") for c in calls if isinstance(c, dict)]
    return slugs if slugs and all(isinstance(s, str) and s for s in slugs) else []


def fake_result(name: str, args: Any) -> str:
    return json.dumps({"ok": True, "simulated": True, "note": "Done."})


def would_have(name: str, args: Any) -> str:
    shown = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str)
    return f"would have called {name} with {shown[:_CLIP]}"


class _Verdict(BaseModel):
    changes: bool


def summary_judge(client: Any, model: str | None, descriptions: dict[str, str]) -> Judge | None:
    """An LLM yes/no on whether a tool changes something, from the summary model."""
    if client is None or not model:
        return None
    from surogates.harness.auxiliary_llm import create_without_reasoning
    from surogates.harness.structured_output import generate_structured

    async def create(**request: Any) -> Any:
        return await create_without_reasoning(client, **request)

    quiet = SimpleNamespace(
        base_url=getattr(client, "base_url", ""),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    async def judge(name: str) -> bool:
        verdict = await generate_structured(
            llm_client=quiet,
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    "An AI agent is about to call this tool. Does calling it change anything outside "
                    "the conversation (send, create, update, delete, pay, book...), as opposed to only "
                    "reading or looking something up?\n\n"
                    f"Tool: {name}\nDescription: {descriptions.get(name) or '(none)'}\n\n"
                    'Answer {"changes": true} or {"changes": false}.'
                ),
            }],
            output_model=_Verdict,
            max_tokens=50,
        )
        if verdict is None:
            raise RuntimeError("no usable verdict")
        return verdict.changes

    return judge
