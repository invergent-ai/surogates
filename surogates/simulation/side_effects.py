"""Which external tool calls a simulated conversation may really make.

A simulation plays made-up people against the real agent.  Its MCP tools
that only read run for real, so the agent works with real data; the ones
that change something (send, create, pay...) are not made and get a
plain success back; what would have happened is kept for the trace, never
shown to the agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from surogates.channels.memory_boundary import SIMULATION_BOUNDARY_PREFIX

logger = logging.getLogger(__name__)


def tool_args(raw: Any) -> dict:
    """A tool call's arguments, sent as a JSON string or already a dict; ``{}`` when neither."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}

WRITE_VERBS = frozenset(
    "SEND CREATE ADD UPDATE EDIT PATCH DELETE REMOVE POST PUT PAY CHARGE REFUND CANCEL BOOK "
    "INVITE SHARE UPLOAD MOVE ARCHIVE REPLY FORWARD SCHEDULE SET INSERT WRITE "
    "MARK REPLACE CLOSE RESOLVE ASSIGN APPROVE SUBMIT".split()
)
READ_VERBS = frozenset(
    "GET LIST FETCH SEARCH FIND READ QUERY LOOKUP RETRIEVE DESCRIBE COUNT EXPORT".split()
)

_ROUTER_PREFIXES = ("mcp__tool_router__", "mcp__composio_tool_router__")
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_JUDGE_TIMEOUT_S = 10
_RETRY_AFTER_FAILURE_S = 300
_CLIP = 300

Judge = Callable[[str, str], Awaitable[bool]]

# key -> (changes, valid until).  ponytail: per-process, unbounded; tool names per worker are few.
_decided: dict[str, tuple[bool, float]] = {}


def is_simulated(config: dict | None) -> bool:
    config = config or {}
    # A sub-agent made before it inherited the run id still has the run's memory boundary.
    return bool(config.get("simulation_run_id")) or str(config.get("memory_boundary") or "").startswith(
        SIMULATION_BOUNDARY_PREFIX
    )


def changes_something(name: str, annotations: dict | None) -> bool | None:
    """True/False when the tool's hints or verbs say; ``None`` when they don't."""
    annotations = annotations or {}
    if annotations.get("readOnlyHint") is True:
        return False
    if annotations.get("destructiveHint") is True or annotations.get("readOnlyHint") is False:
        return True
    tool = name.rsplit("__", 1)[-1]
    words = {w.upper() for part in tool.split("_") for w in _WORDS.findall(part)}
    # "search_and_delete" deletes: any write word outweighs a read word.
    if words & WRITE_VERBS:
        return True
    if words & READ_VERBS:
        return False
    return None


async def _cached(key: str, name: str, description: str, annotations: dict | None, llm: Judge | None) -> bool:
    known = changes_something(name, annotations)
    if known is not None:
        return known
    hit = _decided.get(key)
    if hit is not None and hit[1] > time.monotonic():
        return hit[0]
    if llm is None:
        return True
    try:
        verdict = bool(await asyncio.wait_for(llm(name, description), _JUDGE_TIMEOUT_S))
    except Exception:
        logger.warning("Could not classify tool %s; faking it in the simulation", name, exc_info=True)
        _decided[key] = (True, time.monotonic() + _RETRY_AFTER_FAILURE_S)
        return True
    _decided[key] = (verdict, math.inf)
    return verdict


async def decide(
    name: str, annotations: dict | None, *, llm: Judge | None, args: Any = None, description: str = "",
) -> bool:
    """Whether calling *name* would change something; any doubt counts as yes."""
    if name.startswith(_ROUTER_PREFIXES):
        tool = name.rsplit("__", 1)[-1].upper()
        # The router's executors run any app tool, or code that can: the
        # router tool's name says nothing, the tools it runs do.
        if "WORKBENCH" in tool or "BASH" in tool:
            return True
        if "EXECUTE" in tool:
            slugs = router_slugs(args)
            if not slugs:
                return True
            verdicts = await asyncio.gather(*(_cached(f"router:{s}", s, "", None, llm) for s in slugs))
            return any(verdicts)
    key = f"{name}:{hashlib.sha256(description.encode()).hexdigest()[:16]}"
    return await _cached(key, name, description, annotations, llm)


def router_slugs(args: Any) -> list[str]:
    calls = tool_args(args).get("tools")
    if not isinstance(calls, list):
        return []
    slugs = [c.get("tool_slug") for c in calls if isinstance(c, dict)]
    return slugs if slugs and all(isinstance(s, str) and s for s in slugs) else []


def fake_result(name: str, args: Any) -> str:
    """A plain success, shaped like the real one, that does not say it is a test."""
    slugs = router_slugs(args) if name.startswith(_ROUTER_PREFIXES) else []
    if slugs:
        return json.dumps({
            "data": {"results": [{"tool_slug": s, "successful": True, "data": {}} for s in slugs]},
            "successful": True,
        })
    return json.dumps({"ok": True})


def would_have(name: str, args: Any) -> str:
    shown = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, default=str)
    return f"would have called {name} with {shown[:_CLIP]}"


class _Verdict(BaseModel):
    changes: bool


def summary_judge(client: Any, model: str | None) -> Judge | None:
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

    async def judge(name: str, description: str) -> bool:
        verdict = await generate_structured(
            llm_client=quiet,
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    "An AI agent is about to call this tool. Does calling it change anything outside "
                    "the conversation (send, create, update, delete, pay, book...), as opposed to only "
                    "reading or looking something up?\n\n"
                    f"Tool: {name}\nDescription: {description or '(none)'}\n\n"
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
