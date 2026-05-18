"""Deterministic routing helpers for harness-enforced expert consultation."""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field

from surogates.config import load_settings
from surogates.harness.auxiliary_client import build_summary_auxiliary_llm
from surogates.harness.structured_output import generate_structured
from surogates.tools.loader import SkillDef

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
_HARD_CATEGORY_SET: frozenset[str] = frozenset(HARD_TASK_CATEGORIES)


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


# ---------------------------------------------------------------------------
# LLM-based hard-task classifier (preferred path, regex is fallback)
# ---------------------------------------------------------------------------


_CategoryLiteral = Literal[
    "debugging",
    "terminal",
    "coding",
    "math",
    "problem_solving",
    "data_reasoning",
    "planning",
    "none",
]


class HardTaskJudgment(BaseModel):
    """Structured-output schema for the auxiliary-LLM classifier."""

    required: bool = Field(
        description="True iff the message needs a domain expert to answer well.",
    )
    category: _CategoryLiteral = Field(
        default="none",
        description="One of the routing categories, or 'none' for chitchat / trivial lookups.",
    )


_CLASSIFIER_SYSTEM_PROMPT = """\
You decide whether a specialist (expert) should be consulted before the main assistant answers the user's most recent message.

You will see a short slice of the recent conversation. Short or anaphoric replies like "yes do it", "now the same for the API", "ok try that" inherit the topic of the immediately preceding turn -- read the prior assistant turn to figure out what work the user is approving or extending, and classify based on THAT work.

Categories:
- terminal: shell/process operations, git/docker/kubectl/pytest commands, system inspection.
- math: arithmetic, equations, derivations, calculations, numerical reasoning.
- coding: writing or editing code, refactoring, implementing functions/classes/scripts.
- debugging: errors, tracebacks, failing tests, broken builds, fixing reported bugs.
- data_reasoning: SQL, schemas, datasets, queries, table or column operations.
- planning: architecture, strategy, multi-step roadmaps, design decisions.
- problem_solving: long multi-part requests that need careful reasoning but do not fit a category above.
- none: greetings, casual conversation, acknowledgements, simple lookups, requests that genuinely need no expert.

Examples (transcripts shown in the same format you will receive):

EX1:
[USER]
My pytest run fails with TypeError on test_login

[ASSISTANT]
Could you share the full traceback?

[USER]
yes do it
=> {"required": true, "category": "debugging"}

EX2:
[USER]
What's a good database schema for storing user sessions?

[ASSISTANT]
A sessions table with (id, user_id, created_at, expires_at, token) is typical. Want me to flesh it out?

[USER]
yes please
=> {"required": true, "category": "data_reasoning"}

EX3:
[USER]
hi there
=> {"required": false, "category": "none"}

EX4:
[USER]
thanks for explaining that

[ASSISTANT]
You're welcome.

[USER]
one more thing -- what time is it in Tokyo?
=> {"required": false, "category": "none"}

Reply with JSON only: {"required": <bool>, "category": "<category>"}.
"required" must be true iff "category" != "none".
"""


_MAX_CLASSIFIER_INPUT_CHARS = 4000
_CLASSIFIER_CONTEXT_TURNS = 6  # last N messages (user+assistant) considered
_CLASSIFIER_PER_TURN_CHARS = 1500  # truncation per turn before joining
_CLASSIFIER_CACHE_SIZE = 128


class _ClassifierCache:
    """Tiny LRU keyed by raw message content.

    The same user message is classified repeatedly across turns (the
    coordinator's last_user message doesn't change until a new user turn
    arrives), so a per-process cache avoids paying the auxiliary call
    cost on every iteration of the same task.
    """

    def __init__(self, max_entries: int = _CLASSIFIER_CACHE_SIZE) -> None:
        self._max = max_entries
        self._store: OrderedDict[str, HardTaskClassification] = OrderedDict()

    def get(self, key: str) -> HardTaskClassification | None:
        value = self._store.get(key)
        if value is not None:
            self._store.move_to_end(key)
        return value

    def put(self, key: str, value: HardTaskClassification) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        while len(self._store) > self._max:
            self._store.popitem(last=False)


_classifier_cache = _ClassifierCache()


def _serialize_message_content(content: Any) -> str:
    """Flatten OpenAI-style structured content into a single string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


_CACHE_KEY_ASSISTANT_PREFIX_CHARS = 200


def _build_classifier_payload(
    messages: list[dict[str, Any]],
) -> tuple[str, str, str]:
    """Return ``(latest_user_text, conversation_slice, cache_key)``.

    The slice is a short transcript of the last few user/assistant turns
    (most recent first), so the classifier can disambiguate anaphoric
    messages like "yes do it" against the actual task being discussed.
    Tool/system turns are skipped: they pollute the view with internals
    that don't change task classification.

    The cache key is built only from the **latest user message** and a
    prefix of the **immediately prior assistant message** so that
    classifier decisions stay stable across all LLM iterations within
    one user turn (the latest user message does not change as the
    assistant takes more steps), while still invalidating when the
    user sends a new turn.  Mixing in the prior assistant prefix
    disambiguates short repeated user messages ("yes do it") that
    might appear in unrelated turns.
    """
    relevant: list[tuple[str, str]] = []
    for message in reversed(messages):
        role = str(message.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = _serialize_message_content(message.get("content")).strip()
        if not text:
            continue
        if len(text) > _CLASSIFIER_PER_TURN_CHARS:
            text = text[:_CLASSIFIER_PER_TURN_CHARS] + " ...[truncated]"
        relevant.append((role, text))
        if len(relevant) >= _CLASSIFIER_CONTEXT_TURNS:
            break

    relevant.reverse()  # chronological for the prompt

    latest_user = ""
    prior_assistant = ""
    for index in range(len(relevant) - 1, -1, -1):
        role, text = relevant[index]
        if role == "user" and not latest_user:
            latest_user = text
            for j in range(index - 1, -1, -1):
                if relevant[j][0] == "assistant":
                    prior_assistant = relevant[j][1]
                    break
            break

    lines: list[str] = []
    for role, text in relevant:
        label = "USER" if role == "user" else "ASSISTANT"
        lines.append(f"[{label}]\n{text}")
    if relevant and relevant[-1][0] == "user":
        lines.append("\n>>> Classify the LAST [USER] message above. <<<")
    transcript = "\n\n".join(lines)
    if len(transcript) > _MAX_CLASSIFIER_INPUT_CHARS:
        transcript = (
            "...[older turns trimmed]...\n\n"
            + transcript[-_MAX_CLASSIFIER_INPUT_CHARS:]
        )

    cache_key = (
        prior_assistant[:_CACHE_KEY_ASSISTANT_PREFIX_CHARS]
        + "\x1f"  # ASCII unit separator -- can't collide with a real char
        + latest_user
    )
    return latest_user, transcript, cache_key


async def classify_hard_task_async(
    messages: list[dict[str, Any]],
    *,
    tenant: Any | None = None,
) -> HardTaskClassification:
    """Classify the latest user message using recent conversation context.

    Sends the last few user/assistant turns to the auxiliary LLM so the
    classifier can disambiguate short or anaphoric messages ("yes do
    it") that only make sense given prior context.  Falls back to the
    regex :func:`classify_hard_task` on the latest user text when the
    auxiliary is unconfigured, the call fails, or Outlines is missing.

    The auxiliary client (typically ``settings.llm.summary_model``) is
    built on demand at the call site -- same pattern as
    :func:`title_generator.maybe_generate_session_title`.
    """
    if not messages:
        return HardTaskClassification(False)

    latest_user, transcript, cache_key = _build_classifier_payload(messages)
    if not latest_user:
        return HardTaskClassification(False)

    cached = _classifier_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        aux = build_summary_auxiliary_llm(load_settings(), tenant)
    except Exception:
        logger.debug(
            "Auxiliary client unavailable for hard-task classifier; "
            "falling back to regex.",
            exc_info=True,
        )
        aux = None

    if aux is None:
        result = classify_hard_task(latest_user)
        _classifier_cache.put(cache_key, result)
        return result

    judgment: HardTaskJudgment | None = None
    try:
        judgment = await generate_structured(
            llm_client=aux.client,
            model=aux.model,
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            output_model=HardTaskJudgment,
            # Bumped from 80: the JSON is ~15 tokens but Outlines' grammar
            # generator can emit intermediate scaffolding (and some models
            # prefix a thinking preamble) that eats the budget before the
            # actual JSON lands.
            max_tokens=200,
            temperature=0,
        )
    except Exception:
        logger.debug(
            "LLM hard-task classifier raised; falling back to regex.",
            exc_info=True,
        )

    if judgment is None:
        result = classify_hard_task(latest_user)
        _classifier_cache.put(cache_key, result)
        return result

    category: str | None
    if judgment.category in _HARD_CATEGORY_SET:
        category = judgment.category
    else:
        category = None
    required = bool(judgment.required) and category is not None
    result = HardTaskClassification(
        required=required,
        category=category,
        reason="llm",
    )
    _classifier_cache.put(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Auto-think gate -- maps a classification onto a chat-template knob
# ---------------------------------------------------------------------------


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
    enable_thinking: bool,
) -> dict[str, Any]:
    """Return ``extra_body`` payload that gates the chat-template thinking knob.

    Only the disable side is meaningful: thinking is on by default for
    every model we care about, so we emit the field only when we want
    to suppress reasoning (e.g. classifier says easy task).  Callers
    that want to leave the default behavior should simply not call
    this builder.
    """
    return {"chat_template_kwargs": {"enable_thinking": bool(enable_thinking)}}


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
