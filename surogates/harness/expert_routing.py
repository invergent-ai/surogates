"""Hard-task classification + thinking-toggle helpers.

This module provides the deterministic regex classifier
(``classify_hard_task``) and its LLM-based equivalent
(``classify_hard_task_async``) plus ``model_supports_thinking_toggle``.
The classifier drives the hidden advisor preflight; the toggle check
sizes the LLM-call layer's stale-stream timeout for reasoning models.

Auto-routing to experts based on this classifier was removed when the
expert mechanism was rebuilt as a voluntary-consultation feature
(see ``docs/superpowers/specs/2026-05-23-expert-mechanism-resurrection-design.md``).
The selection helpers that used to live here (``select_expert_for_task``,
``classify_tool_calls``, ``load_skills_for_expert_routing``) are gone;
``consult_expert`` and the ``/<expert>`` slash command are the only
entry points to an expert.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

HARD_TASK_CATEGORIES: tuple[str, ...] = (
    "debugging",
    "terminal",
    "coding",
    "math",
    "problem_solving",
    "data_reasoning",
    "planning",
)


# Models that honor ``chat_template_kwargs.enable_thinking`` -- the vLLM
# convention used by Qwen3, GLM-4.5/4.6/5.1, and QwQ.  Verified against
# GLM 5.1 via DeepInfra: setting ``enable_thinking=False`` on a trivial
# prompt cuts completion tokens by ~96% (225 -> 8).  ``surogate`` is
# the proxy sentinel that gets rewritten to the live upstream model.
_THINKING_TOGGLE_MODEL_TOKENS: tuple[str, ...] = (
    "surogate",
    "glm-4",
    "glm-5",
    "qwen3",
    "qwen-3",
    "qwq",
)


def model_supports_thinking_toggle(model_id: str | None) -> bool:
    """Whether *model_id* accepts ``chat_template_kwargs.enable_thinking``.

    Conservative allowlist: only return ``True`` for models known to
    honor the vLLM chat-template-kwargs passthrough.  Other providers
    silently drop the field (verified for Z.AI native ``thinking.type``
    and OpenAI-shaped ``reasoning.effort`` on DeepInfra), so claiming
    "supported" for the wrong model is harmless -- but we'd rather
    avoid wasted bytes on requests where the gate can't fire.
    """
    if not model_id:
        return False
    lower = model_id.lower()
    return any(token in lower for token in _THINKING_TOGGLE_MODEL_TOKENS)
