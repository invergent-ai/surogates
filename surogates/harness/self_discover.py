"""SELF-DISCOVER planning preamble.

Synthesises a task-specific reasoning scaffold *before* the agent
starts solving.  The scaffold is built in a single auxiliary-LLM call
with structured output: the engine picks 3-7 reasoning primitives
from [[reasoning_modules]] that fit the task, then produces a free-
form JSON ``structure`` whose field names the LLM chooses to match
the task -- exactly as DeepThink does, but in one call instead of
three thanks to Outlines.  The envelope still pins ``pitfalls`` and
``relevant_modules`` so we always know which heuristics fired.

The scaffold is injected into the loop's ``create_kwargs.messages``
as an ephemeral synthetic ``user``-role message right before the
next assistant turn -- it does **not** become part of the stored
conversation, so the prompt cache, compressor, and event log stay
clean.

Failures (aux unavailable, network error, structured-output parse
miss) return ``None`` and the loop proceeds without a scaffold,
matching the previous behaviour.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, Field

from surogates.config import load_settings
from surogates.harness.auxiliary_client import build_summary_auxiliary_llm
from surogates.harness.expert_routing import _build_classifier_payload
from surogates.harness.reasoning_modules import REASONING_MODULES, render_module_library
from surogates.harness.structured_output import generate_structured

logger = logging.getLogger(__name__)


# Categories that get a scaffold.  ``terminal`` is excluded -- shell
# work is mechanical and a planning preamble is overkill.  ``none`` is
# implicitly excluded because the classifier already gates thinking
# off and the loop never asks for a scaffold on easy turns.
SCAFFOLD_CATEGORIES: frozenset[str] = frozenset({
    "coding",
    "debugging",
    "data_reasoning",
    "planning",
    "math",
    "problem_solving",
})


class ReasoningScaffold(BaseModel):
    """The structured-output envelope for a SELF-DISCOVER scaffold.

    ``structure`` is intentionally free-form ``dict[str, Any]`` so the
    LLM can pick field names that fit the task (e.g. ``proof_strategy``
    for math, ``reproduce_then_isolate`` for debugging) -- the
    DeepThink semantics.  ``relevant_modules`` and ``pitfalls`` stay
    in the envelope so they're always present and predictable.
    """

    relevant_modules: list[str] = Field(
        description=(
            "Names of 3-7 reasoning modules from the provided library "
            "that this task benefits from. Use the exact module names."
        ),
        max_length=7,
    )
    structure: dict[str, Any] = Field(
        description=(
            "A free-form JSON object whose field names you choose to fit "
            "this task.  Each field is a concrete reasoning step keyed "
            "by a short snake_case name that names what to do (e.g. "
            "'identify_unknowns', 'set_up_equation', 'verify').  Steps "
            "may nest sub-objects to express ordered substeps.  The "
            "structure is followed top-to-bottom when solving."
        ),
    )
    pitfalls: list[str] = Field(
        description=(
            "Common mistakes on this kind of task to actively avoid. "
            "Each pitfall is one short, concrete sentence."
        ),
        max_length=5,
    )


_SCAFFOLD_SYSTEM_PROMPT = """\
You are a planning assistant.  Given a user's task, you produce a *reasoning scaffold* the main assistant will follow when answering.

You do NOT solve the task.  You design how to think about it.

Your output has three parts:
1. ``relevant_modules``: 3-7 module names picked from the library below.  Pick the heuristics that are most useful for THIS task.
2. ``structure``: a JSON object whose field names YOU choose to operationalize the picked modules into concrete reasoning steps for this exact task.  Field names are snake_case, describe what to do, and are ordered (the main assistant follows them top-to-bottom).  Nest sub-objects for sub-steps when natural.  Examples:
     - Math: ``{"identify_unknowns": "...", "set_up_equation": "...", "solve": {"step_1": "...", "step_2": "..."}, "verify_units": "..."}``
     - Debugging: ``{"reproduce": "...", "isolate": "...", "hypothesize": [...], "test_hypothesis": "...", "confirm_fix": "..."}``
     - Coding: ``{"understand_inputs_and_outputs": "...", "edge_cases": [...], "draft_implementation": "...", "test_against_edge_cases": "..."}``
3. ``pitfalls``: 1-5 short, concrete mistakes that are commonly made on this kind of task.

Be specific to the task at hand -- generic scaffolds are useless.  When the user task is anaphoric ("yes do it", "now the same for the API"), use the earlier turns to figure out what the actual task is.
"""


_SCAFFOLD_CACHE_SIZE = 64


class _ScaffoldCache:
    """Tiny LRU keyed on the classifier's cache key so scaffolds stay
    stable across all LLM iterations within a single user turn.
    """

    def __init__(self, max_entries: int = _SCAFFOLD_CACHE_SIZE) -> None:
        self._max = max_entries
        self._store: OrderedDict[str, ReasoningScaffold] = OrderedDict()

    def get(self, key: str) -> ReasoningScaffold | None:
        value = self._store.get(key)
        if value is not None:
            self._store.move_to_end(key)
        return value

    def put(self, key: str, value: ReasoningScaffold) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        while len(self._store) > self._max:
            self._store.popitem(last=False)


_scaffold_cache = _ScaffoldCache()


async def build_scaffold(
    messages: list[dict[str, Any]],
    *,
    category: str,
    tenant: Any | None = None,
) -> ReasoningScaffold | None:
    """Synthesize a reasoning scaffold for the latest user turn.

    Returns ``None`` when the auxiliary client is unavailable, the
    structured-output call fails, or Outlines is missing -- the loop
    then proceeds without a scaffold (same as previous behaviour).
    """
    if category not in SCAFFOLD_CATEGORIES:
        return None
    if not messages:
        return None

    latest_user, transcript, cache_key = _build_classifier_payload(messages)
    if not latest_user:
        return None

    cached = _scaffold_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        aux = build_summary_auxiliary_llm(load_settings(), tenant)
    except Exception:
        logger.debug(
            "Auxiliary client unavailable for SELF-DISCOVER; "
            "skipping scaffold.",
            exc_info=True,
        )
        return None
    if aux is None:
        return None

    user_prompt = (
        f"Task category: {category}\n\n"
        f"Conversation (build a scaffold for the LAST user message):\n\n"
        f"{transcript}\n\n"
        f"Reasoning modules library:\n{render_module_library()}"
    )

    try:
        scaffold = await generate_structured(
            llm_client=aux.client,
            model=aux.model,
            messages=[
                {"role": "system", "content": _SCAFFOLD_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            output_model=ReasoningScaffold,
            max_tokens=1500,
            temperature=0,
        )
    except Exception:
        logger.debug(
            "SELF-DISCOVER structured generation raised; skipping scaffold.",
            exc_info=True,
        )
        return None

    if scaffold is None:
        return None

    # Drop modules the LLM hallucinated (not in our library).  We keep
    # the rest -- a mostly-good scaffold is still useful.
    valid_modules = [m for m in scaffold.relevant_modules if m in REASONING_MODULES]
    if valid_modules != scaffold.relevant_modules:
        scaffold = scaffold.model_copy(update={"relevant_modules": valid_modules})

    _scaffold_cache.put(cache_key, scaffold)
    return scaffold


def format_scaffold_for_injection(scaffold: ReasoningScaffold) -> str:
    """Render a scaffold block for in-place merge into the user message.

    Wrapped in ``<reasoning_scaffold>...</reasoning_scaffold>`` so the
    assistant can recognise it as harness-supplied planning context
    (not user-authored prose) and so we can detect double-injection if
    the block ever leaks back into the persisted log.

    No trailing imperative: the user's request already states what to
    do. Stacking "Now produce the answer..." at the end of every
    iteration's last user-role message caused the model to narrate
    "The user is asking me to continue..." on every iteration.
    """
    modules_line = ", ".join(scaffold.relevant_modules) or "(none)"
    structure_json = json.dumps(scaffold.structure, indent=2)
    pitfalls_lines = (
        "\n".join(f"  - {p}" for p in scaffold.pitfalls)
        if scaffold.pitfalls
        else "  (none identified)"
    )
    return (
        "<reasoning_scaffold>\n"
        "Planning context for this task. Following the structure below "
        "tends to produce more reliable answers on this kind of work.\n\n"
        f"**Heuristics in play:** {modules_line}\n\n"
        f"**Structure:**\n```json\n{structure_json}\n```\n\n"
        f"**Pitfalls to avoid:**\n{pitfalls_lines}\n"
        "</reasoning_scaffold>"
    )
