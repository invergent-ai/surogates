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

# The advisor when it runs on the executor's own model (``llm.advisor_same_model``): a second opinion in a fresh
# context, not a stronger model, and no standing invitation to stop. On 11-21 Inkling-2-Mini, advising itself under the
# text above, told itself to halt in 44 % (AutomationBench) and 46 % (EnterpriseOps-Gym) of consulted sessions, W4
# 13 / 20 %, and failed 27 of 29 EnterpriseOps-Gym sessions after a halt.
_SAME_MODEL_INSTRUCTIONS = """\
You are a reviewer giving a second opinion on the executor's work, in a
fresh context. The executor will continue the task after reading your
guidance. Give concise, high-leverage advice.

Work the problem as far as you can yourself: state the key intermediate
results, the pitfalls, and -- when you are confident -- your own answer,
plus how the executor should verify it. Concrete numbers, enumerations,
and near-code help more than method-only advice. If the task needs tools
you do not have, say exactly what to run and what output to expect.

If the executor is heading somewhere that will not work, say so plainly
and say what to do instead. Recommend stopping only when the task cannot
be completed with the tools available; otherwise give the next concrete
step.
"""

_SAME_MODEL_DESCRIPTION = (
    "A reviewer in a fresh context that checks the work so far and returns "
    "strategy or a correction"
)


def _same_model() -> bool:
    """Whether the advisor runs on the executor's own model (``llm.advisor_same_model``)."""
    from surogates.config import load_settings

    return bool(getattr(load_settings().llm, "advisor_same_model", False))


# The caller-side half of the advisor protocol. The executor decides when
# to consult on its own -- the classifier preflight that used to make that
# call was measured useful 0.7% of the time and deleted -- so the timing
# rules have to live where the executor reads them before choosing, which
# is this trigger line under the advisor's roster entry.
_ADVISOR_TRIGGER = (
    "before committing to an approach, when stuck on a recurring error, "
    "or before declaring a task done -- and only when no domain expert "
    "above covers the subject.\n"
    "  Consult before substantive work, not after: finding files, reading "
    "a source, or looking at what is there is orientation and does not "
    "count -- writing, editing, and declaring an answer do. On a task of "
    "more than a few steps, consult once before committing to an approach "
    "and once before declaring it done; on short reactive steps dictated "
    "by output you just read, do not keep consulting.\n"
    "  Before the done-consult, if the deliverable is a file or a code "
    "change, write or commit it first -- the consult takes time, and a "
    "session that ends during it keeps a written result and loses an "
    "unwritten one. Don't write a chat answer to a file just to save it.\n"
    "  Weigh the answer seriously. Adapt only on empirical failure or "
    "primary-source evidence against a specific claim; a self-test that "
    "passes is not evidence the advice was wrong, only that the test does "
    "not check what the advice checks. If your own findings point one way "
    "and the advisor points another, do not silently switch -- consult "
    "once more naming the conflict and asking which constraint breaks the "
    "tie."
)


def build_advisor_expert() -> SkillDef:
    """Return the built-in advisor as an active, tool-less expert."""
    same = _same_model()
    return SkillDef(
        name=ADVISOR_EXPERT_NAME,
        description=_SAME_MODEL_DESCRIPTION if same else _ADVISOR_DESCRIPTION,
        content=_SAME_MODEL_INSTRUCTIONS if same else _ADVISOR_INSTRUCTIONS,
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


def advisor_available(model: str | None) -> bool:
    """Whether a session running on *model* may consult the advisor.

    A session already on the Pro tier would consult its own model: a
    second Pro-rate completion that brings no stronger opinion.
    """
    return model != ADVISOR_MODEL_SENTINEL


def build_expert_transcript(messages: list[dict]) -> str:
    """Render the recent conversation for an expert that reads it.

    Bounded twice on purpose: the last 12 messages, then a hard 16k
    character cut. An expert consult bills at its own model's rate, and
    an unbounded transcript would let a long session quietly multiply
    the cost of every consult.

    The session's first user message (the task) leads it, cut to 4,000
    characters, when it is not among the last 12: without it the advisor
    judged work against a task it could not read (11-21: the task was in
    the window for 0 % of AutomationBench consults, 3 % Claw-Eval, 35 %
    EnterpriseOps-Gym). The 16k bound holds for the whole.
    """
    def text_of(msg: dict) -> str:
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = _collapse_text_parts([
                part
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ])
        return content if isinstance(content, str) else ""

    recent = messages[-12:]
    fragments: list[str] = []
    for msg in recent:
        content = text_of(msg)
        if not content.strip():
            continue
        fragments.append(f"{msg.get('role', 'unknown')}: {content}")
    task = next((m for m in messages if m.get("role") == "user" and text_of(m).strip()), None)
    head = ""
    if task is not None and not any(m is task for m in recent):
        head = f"user (the task, the session's first message): {text_of(task)[:4_000]}\n\n"
    return head + "\n\n".join(fragments)[-(16_000 - len(head)):]
