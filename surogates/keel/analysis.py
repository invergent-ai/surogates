"""The model call behind keel-link's analysis routes.

Keel asks the runtime to group an agent's raised hands by reason and to draft
a passage of policy from agreeing answers.  Both are one structured call to
the agent's own model: its summary slot, else its main slot, keyed at org
scope from the vault and reached at the slot's ``base_url`` -- for a platform
agent that is the management plane's proxy, so the agent's project is billed
there.  Nothing is stored: a call is a request and an answer.

A log line here names the agent, a count or an error's class, never what a
hand or the model said: hands are case content.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel

from surogates.harness.model_metadata import estimate_call_cost
from surogates.harness.session_llm import resolve_org_llm
from surogates.harness.structured_output import StructuredCall, generate_structured_with_usage
from surogates.runtime.context import LLMEndpoint
from surogates.runtime.resolver import build_agent_runtime_context

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

#: How long the model work of one request may take, both attempts together.
#: Keel's link waits longer, so a slow model is answered before Keel gives up.
#: Read at call time: tests patch it.
ANALYSIS_MODEL_SECONDS = 22.0

#: The longest reason and answer gist a label may hold, in characters.
REASON_CHARS = 120
GIST_CHARS = 120

#: How much of one answer reaches the model.
ANSWER_PROMPT_CHARS = 2000

#: What a call that reached no model used.
NO_USAGE: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": "0.000000"}


class AnalysisError(Exception):
    """A request the model could not serve: the route answers its status and code."""

    status: int
    code: str
    message: str

    def __init__(self) -> None:
        super().__init__(self.code)


class ModelUnavailable(AnalysisError):
    status = 503
    code = "unavailable"
    message = "The agent's model cannot be used: it names none, or the vault holds no key for it."


class ModelFailed(AnalysisError):
    status = 502
    code = "model-failed"
    message = "The model gave no usable answer."


class ModelTimedOut(AnalysisError):
    status = 504
    code = "model-timeout"
    message = "The model did not finish in time."


@dataclass(frozen=True, slots=True)
class Analysis(Generic[T]):
    """The object a call produced, ``None`` when no completion held a usable one, and what the call used."""

    value: T | None
    usage: dict[str, Any]


def analysis_endpoint(agent: dict[str, Any]) -> LLMEndpoint:
    """The endpoint an agent's analysis runs on: its summary slot, else its main slot.

    There is no session here, so no tier swap: the slot is the one the
    runtime config names.  A config that does not project, or names
    neither slot, raises :class:`ModelUnavailable`.
    """
    try:
        context = build_agent_runtime_context(agent)
    except (KeyError, TypeError, ValueError):
        raise ModelUnavailable from None
    endpoint = context.llm_summary or context.llm_main
    if endpoint is None:
        raise ModelUnavailable
    return endpoint


def usage_answer(call: StructuredCall[Any], model: str) -> dict[str, Any]:
    """What the call used, as Keel is told: tokens, and the cost as a six-place decimal string.

    The cost is an estimate against the model that served the call, ``null``
    when no price is known for it.  A call that received no completion used
    nothing.
    """
    if call.completions == 0:
        return dict(NO_USAGE)
    cost, priced = estimate_call_cost(
        model, call.served_by, call.input_tokens, call.output_tokens,
        cache_read_tokens=call.cache_read_tokens,
    )
    return {
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "cost_usd": None if priced is None else f"{cost:.6f}",
    }


def one_line(value: object) -> str | None:
    """A model's string read on one line: white space runs become one space, the ends are stripped and one
    trailing full stop goes.  ``None`` for anything but a string, and for an empty result."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if text.endswith("."):
        text = text[:-1].rstrip()
    return text or None


async def run_analysis(
    agent: dict[str, Any],
    *,
    vault: Any,
    org_id: UUID,
    messages: list[dict[str, str]],
    output_model: type[T],
    max_tokens: int,
) -> Analysis[T]:
    """One structured call as the agent, bounded at :data:`ANALYSIS_MODEL_SECONDS` for both its attempts.

    No vault, a vault that raises or holds no key, and an agent that names
    no model raise :class:`ModelUnavailable`; a call that received no
    completion raises :class:`ModelFailed`; the bound raises
    :class:`ModelTimedOut`.  Completions that held no valid object are not
    an error: the answer's value is ``None`` and its usage says what they
    cost.  The client is closed whatever happens.
    """
    agent_id = str(agent.get("agent_id"))
    endpoint = analysis_endpoint(agent)
    if vault is None:
        logger.warning("keel-link: agent %s: no credential vault to read the model's key from", agent_id)
        raise ModelUnavailable
    try:
        slot = await resolve_org_llm(
            endpoint, vault=vault, org_id=org_id, timeout=ANALYSIS_MODEL_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 -- a vault can fail in any way
        logger.warning(
            "keel-link: agent %s: the model's key could not be read (%s)", agent_id, type(exc).__name__,
        )
        raise ModelUnavailable from None
    try:
        try:
            async with asyncio.timeout(ANALYSIS_MODEL_SECONDS):
                call = await generate_structured_with_usage(
                    llm_client=slot.client,
                    model=slot.model,
                    messages=messages,
                    output_model=output_model,
                    max_tokens=max_tokens,
                    temperature=0,
                )
        except TimeoutError:
            logger.warning("keel-link: agent %s: the model did not finish in time", agent_id)
            raise ModelTimedOut from None
    finally:
        await slot.client.close()
    if call.completions == 0:
        logger.warning("keel-link: agent %s: the model sent no completion", agent_id)
        raise ModelFailed
    return Analysis(call.value, usage_answer(call, slot.model))
