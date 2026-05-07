"""Deterministic routing helpers for harness-enforced expert consultation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from surogates.tools.loader import SkillDef

HARD_TASK_CATEGORIES: tuple[str, ...] = (
    "debugging",
    "terminal",
    "coding",
    "math",
    "problem_solving",
    "data_reasoning",
    "planning",
)


@dataclass(frozen=True, slots=True)
class HardTaskClassification:
    """Result of deterministic hard-task classification."""

    required: bool
    category: str | None = None
    reason: str = ""


_DEBUGGING_RE = re.compile(
    r"\b(traceback|stack trace|exception|error|failing|failed|bug|debug|fix)\b",
    re.IGNORECASE,
)
_TERMINAL_RE = re.compile(
    r"\b(run|execute|terminal|shell|bash|command|pytest|npm|pnpm|uv|git|docker|kubectl)\b",
    re.IGNORECASE,
)
_CODING_RE = re.compile(
    r"\b(code|coding|function|class|method|implement|refactor|python|typescript|"
    r"javascript|sql|api|endpoint|test|tests|parser|script)\b",
    re.IGNORECASE,
)
_MATH_RE = re.compile(
    r"(\b(solve|calculate|derive|equation|integral|derivative|probability|"
    r"matrix|algebra|geometry)\b|[0-9]\s*[+\-*/=]\s*[0-9a-z])",
    re.IGNORECASE,
)
_DATA_RE = re.compile(
    r"\b(schema|query|dataset|dataframe|sql|join|aggregate|table|column|migration)\b",
    re.IGNORECASE,
)
_PLANNING_RE = re.compile(
    r"\b(plan|architecture|design|strategy|multi-step|roadmap|break down|"
    r"implementation plan)\b",
    re.IGNORECASE,
)
_GENERIC_RE = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|yes|no|sounds good|"
    r"summarize this|rewrite this)\b",
    re.IGNORECASE,
)


def classify_hard_task(text: str) -> HardTaskClassification:
    """Classify whether *text* requires expert consultation.

    This is intentionally deterministic and conservative.  It routes only
    categories that are clearly reasoning-intensive or tool/action oriented.
    """
    content = (text or "").strip()
    if not content:
        return HardTaskClassification(False)
    if _GENERIC_RE.search(content) and len(content) < 120:
        return HardTaskClassification(False)

    if _TERMINAL_RE.search(content):
        return HardTaskClassification(True, "terminal", "terminal keyword")
    if _MATH_RE.search(content):
        return HardTaskClassification(True, "math", "math keyword")
    if _DATA_RE.search(content):
        return HardTaskClassification(True, "data_reasoning", "data keyword")
    if _DEBUGGING_RE.search(content) and _CODING_RE.search(content):
        return HardTaskClassification(True, "debugging", "debugging keyword")
    if _CODING_RE.search(content):
        return HardTaskClassification(True, "coding", "coding keyword")
    if _PLANNING_RE.search(content):
        return HardTaskClassification(True, "planning", "planning keyword")
    if len(content) > 500 and ("?" in content or "\n" in content):
        return HardTaskClassification(
            True, "problem_solving", "long multi-part request",
        )

    return HardTaskClassification(False)


def classify_tool_calls(tool_calls: list[dict]) -> HardTaskClassification:
    """Classify explicit LLM tool intent into an expert routing category."""
    names = [
        str(tc.get("function", {}).get("name", ""))
        for tc in tool_calls
    ]
    if any(name in {"terminal", "process"} for name in names):
        return HardTaskClassification(True, "terminal", "terminal tool call")
    if any(name in {"write_file", "patch"} for name in names):
        return HardTaskClassification(True, "coding", "code mutation tool call")
    return HardTaskClassification(False)


_TRIGGER_SPLIT_RE = re.compile(r"[,;\n]+")
_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_TRIGGER_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "by", "for", "in", "of", "on", "or",
    "the", "to", "with",
})


def select_expert_for_task(
    experts: Iterable[SkillDef],
    task: str,
) -> SkillDef | None:
    """Select an active expert whose trigger matches *task*.

    Trigger text is the expert's routing contract.  Matching is
    deterministic: exact phrase matches score highest, token coverage
    scores next, and ties are resolved by stable expert name order.
    """
    haystack = _normalise_trigger_text(task)
    scored: list[tuple[int, str, SkillDef]] = []
    for expert in experts:
        if not expert.is_active_expert:
            continue
        score = _trigger_match_score(expert.trigger or "", haystack)
        if score > 0:
            scored.append((score, expert.name, expert))
    if not scored:
        return None
    return sorted(scored, key=lambda item: (-item[0], item[1]))[0][2]


def _normalise_trigger_text(text: str) -> str:
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _trigger_match_score(trigger: str, haystack: str) -> int:
    if not trigger.strip() or not haystack:
        return 0

    haystack_tokens = set(haystack.split())
    best = 0
    for raw_phrase in _TRIGGER_SPLIT_RE.split(trigger):
        phrase = _normalise_trigger_text(raw_phrase)
        if not phrase:
            continue
        if phrase in haystack:
            best = max(best, 100 + len(phrase.split()))
            continue
        phrase_tokens = [
            token for token in phrase.split()
            if token not in _TRIGGER_STOPWORDS
        ]
        if not phrase_tokens:
            continue
        overlap = sum(1 for token in phrase_tokens if token in haystack_tokens)
        if overlap == len(phrase_tokens):
            best = max(best, 50 + overlap)
        elif overlap > 0 and len(phrase_tokens) > 1:
            best = max(best, overlap)
    return best


async def load_skills_for_expert_routing(
    tenant: object,
    *,
    session_factory: object | None = None,
) -> list[SkillDef]:
    """Load tenant skills for forced expert routing."""
    from surogates.tools.loader import ResourceLoader

    loader = ResourceLoader()
    if session_factory is not None:
        async with session_factory() as db_session:  # type: ignore[misc]
            return await loader.load_skills(tenant, db_session=db_session)
    return await loader.load_skills(tenant)
