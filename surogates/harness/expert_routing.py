"""Hard-task classification + thinking-toggle helpers.

This module provides the deterministic regex classifier
(``classify_hard_task``) and its LLM-based equivalent
(``classify_hard_task_async``) plus the chat-template ``enable_thinking``
toggle helpers. The classifier drives the hidden advisor preflight; the
thinking-toggle helpers are used by the LLM-call layer's
runaway-reasoning recovery.

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


def build_thinking_extra_body(
    *,
    enable_thinking: bool | None = None,
) -> dict[str, Any]:
    """Return ``extra_body`` payload for the reasoning on/off toggle.

    Emits ``enable_thinking`` at the top level of ``extra_body`` so
    DashScope-compatible providers (Qwen3-Max) see it, plus a duplicate
    ``chat_template_kwargs.enable_thinking`` for vLLM-style providers
    (GLM via DeepInfra) that read the chat-template form.  Unknown
    fields are silently dropped by providers that don't recognise them,
    so dual emission costs nothing.

    ``enable_thinking`` ``False`` suppresses reasoning; ``True`` forces
    it on; ``None`` leaves the provider default in place (no field
    emitted).
    """
    body: dict[str, Any] = {}
    if enable_thinking is not None:
        body["enable_thinking"] = bool(enable_thinking)
        body["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}
    return body


def merge_extra_body(
    existing: dict[str, Any] | None,
    addition: dict[str, Any],
) -> dict[str, Any]:
    """Shallow-merge *addition* into *existing*, deep-merging known nested keys.

    ``chat_template_kwargs`` is itself a dict that other code paths may
    populate (e.g. provider-specific routing flags), so we merge it
    rather than overwriting.  Other keys in *addition* take precedence
    over *existing* at the top level.
    """
    merged: dict[str, Any] = dict(existing or {})
    for key, value in addition.items():
        if (
            key == "chat_template_kwargs"
            and isinstance(value, dict)
            and isinstance(merged.get(key), dict)
        ):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged
