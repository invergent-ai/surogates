"""The built-in ``advisor`` expert.

The advisor is a stronger model the executor consults at decision points:
before committing to an approach, when stuck on a recurring error, and
before declaring a task done. It is not a separate mechanism -- it is an
expert with no tools and a single iteration, so it reaches the executor
through ``consult_expert`` like any other.

It needs no endpoint of its own. ``expert_model`` is the Pro tier
sentinel, and the proxy serves the Pro upstream for any request whose
body asks for it, whichever route it arrives on. That is what lets a
platform capability be expressed as an ordinary skill definition rather
than a bespoke client wired through its own runtime-config slot.

Ordering matters where this is rendered: tenant-authored experts are
listed first and the advisor last, because a domain specialist beats a
generalist on its own subject. The advisor is the fallback when nothing
else fits.
"""

from __future__ import annotations

from surogates.harness.loop_vision import _collapse_text_parts
from surogates.tools.loader import (
    EXPERT_STATUS_ACTIVE,
    SKILL_SOURCE_PLATFORM,
    SkillDef,
)

#: Reserved name. A tenant-authored expert may not shadow it -- the merge
#: appends the built-in last so it wins on collision.
ADVISOR_EXPERT_NAME = "advisor"

#: The Pro tier sentinel. The proxy rewrites it to the live upstream
#: model name and bills at the Pro multiplier.
ADVISOR_MODEL_SENTINEL = "surogate-pro"

#: One consult is one completion -- the advisor reads and answers, it
#: does not work the problem with tools.
_ADVISOR_MAX_ITERATIONS = 1

# "Carry the work" wording, A/B-tested against a "do not solve outright"
# instruction: method-only advice failed to rescue the executor on
# arithmetic- and bookkeeping-heavy tasks, while guidance carrying
# concrete intermediates and a proposed answer flipped them to passes
# without degrading tasks the executor already solved on its own.
_ADVISOR_INSTRUCTIONS = """\
You are a strategic advisor. The executor model is cheaper and will
continue the task after reading your guidance. Give concise,
high-leverage advice.

Work the problem as far as you can yourself: state the key intermediate
results, the pitfalls, and -- when you are confident -- your own answer,
plus how the executor should verify it. Method-only advice does not
rescue a weaker model from error-prone arithmetic or bookkeeping;
concrete numbers, enumerations, and near-code do. If the task needs
tools you do not have, say exactly what to run and what output to
expect.

If the executor is heading somewhere that will not work, say so plainly
and say what to do instead. A stop signal is a valid answer.
"""

_ADVISOR_DESCRIPTION = (
    "A stronger model that reviews the work so far and returns strategy, "
    "a correction, or a stop signal"
)

_ADVISOR_TRIGGER = (
    "before committing to an approach, when stuck on a recurring error, "
    "or before declaring a task done -- and only when no domain expert "
    "above covers the subject"
)


def build_advisor_expert() -> SkillDef:
    """Return the built-in advisor as an active, tool-less expert."""
    return SkillDef(
        name=ADVISOR_EXPERT_NAME,
        description=_ADVISOR_DESCRIPTION,
        content=_ADVISOR_INSTRUCTIONS,
        source=SKILL_SOURCE_PLATFORM,
        builtin=True,
        type="expert",
        trigger=_ADVISOR_TRIGGER,
        expert_model=ADVISOR_MODEL_SENTINEL,
        expert_status=EXPERT_STATUS_ACTIVE,
        expert_tools=[],
        expert_max_iterations=_ADVISOR_MAX_ITERATIONS,
    )


def is_advisor_expert(skill: object) -> bool:
    """Whether *skill* is the built-in advisor.

    Checked on ``builtin`` as well as the name so a tenant-authored
    expert called "advisor" is never mistaken for the platform one.
    """
    return (
        getattr(skill, "name", None) == ADVISOR_EXPERT_NAME
        and getattr(skill, "builtin", False)
        and getattr(skill, "is_expert", False)
    )


def build_expert_transcript(messages: list[dict]) -> str:
    """Render the recent conversation for an expert that reads it.

    Bounded twice on purpose: the last 12 messages, then a hard 16k
    character cut. An expert consult bills at its own model's rate, and
    an unbounded transcript would let a long session quietly multiply
    the cost of every consult.
    """
    fragments: list[str] = []
    for msg in messages[-12:]:
        role = msg.get("role", "unknown")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = _collapse_text_parts([
                part
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ])
        if not isinstance(content, str) or not content.strip():
            continue
        fragments.append(f"{role}: {content}")
    return "\n\n".join(fragments)[-16_000:]
