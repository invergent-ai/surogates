"""Automatic context window compression for long conversations.

Self-contained class with its own LLM client for summarisation.
Uses auxiliary model (cheap/fast) to summarise middle turns while
protecting head and tail context.

Improvements over v1:
  - Structured summary template (Goal, Progress, Decisions, Files, Next Steps)
  - Iterative summary updates (preserves info across multiple compactions)
  - Token-budget tail protection instead of fixed message count
  - Tool output pruning before LLM summarisation (cheap pre-pass)
  - Scaled summary budget (proportional to compressed content)
  - Richer tool call/result detail in summariser input
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from surogates.harness.model_metadata import (
    ModelInfo,
    estimate_tokens,
    get_model_info,
    resolve_model_info,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Summary prefix / legacy prefix
# ---------------------------------------------------------------------------

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. Respond ONLY to the latest user message "
    "that appears AFTER this summary. The current session state (files, "
    "config, etc.) may reflect work described here — avoid repeating it:"
)
LEGACY_SUMMARY_PREFIX = "[CONTEXT SUMMARY]:"

# Delimiter used when a summary has to be merged into the first tail message
# because neither role would preserve alternation.
SUMMARY_END_DELIMITER = (
    "--- END OF CONTEXT SUMMARY — respond to the message below, "
    "not the summary above ---"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum tokens for the summary output.
_MIN_SUMMARY_TOKENS: int = 2000
# Proportion of compressed content to allocate for summary.
_SUMMARY_RATIO: float = 0.20
# Absolute ceiling for summary tokens (even on very large context windows).
_SUMMARY_TOKENS_CEILING: int = 12_000

# Placeholder used when pruning old tool results.
_PRUNED_TOOL_PLACEHOLDER: str = "[Old tool output cleared to save context space]"

# Chars per token rough estimate.
_CHARS_PER_TOKEN: int = 4
_SUMMARY_FAILURE_COOLDOWN_SECONDS: int = 600

# Fallback context window when the model is unknown.
_DEFAULT_CONTEXT_WINDOW: int = 128_000


def _estimate_messages_tokens_rough(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate for a message list (chars/4 heuristic)."""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        if isinstance(content, str):
            total += len(content) // _CHARS_PER_TOKEN + 10
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    total += len(block["text"]) // _CHARS_PER_TOKEN + 10
                else:
                    total += 256
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                args = tc.get("function", {}).get("arguments", "")
                total += len(args) // _CHARS_PER_TOKEN
    return total


# Tools whose results are fully superseded by the next call of the same tool.
# The agent acts only on the most recent snapshot (and re-fetches state on
# demand), so older results are dead weight that can be pruned eagerly instead
# of waiting for a compaction pass.
# Tools whose results are a snapshot of the current page: only the most
# recent matters, because the agent re-fetches state rather than re-reading an
# old one.  ``browser_navigate`` belongs here because it now returns the page
# outline too -- without it those snapshots accumulate for the whole session.
# Observed on GAIA dev-021: one task made 34 browser calls and reached 324,700
# input tokens against a 262,144 window.
_SUPERSEDED_STATE_TOOLS: frozenset[str] = frozenset({
    "browser_get_state",
    "browser_navigate",
})


def prune_superseded_tool_results(
    messages: list[dict[str, Any]],
    *,
    tool_names: frozenset[str] | set[str],
    keep_last: int,
    placeholder: str,
    min_chars: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    """Replace all but the most recent ``keep_last`` results of ``tool_names``.

    A tool result is identified by mapping its ``tool_call_id`` back to the
    ``name`` of the assistant ``tool_call`` that produced it.  For each targeted
    tool, every result except the most recent ``keep_last`` has its content
    replaced with ``placeholder`` -- provided the content is a string longer
    than ``min_chars`` and not already the placeholder.

    ``tool_call_id`` and every other field are preserved, so tool-call pairing
    is never broken.  The input list and its dicts are not mutated; changed
    messages are shallow-copied into a new list.

    Returns ``(new_messages, pruned_count)``.
    """
    if not messages or keep_last < 0:
        return messages, 0

    # 1. Collect the tool_call_ids belonging to the targeted tools (a result
    #    message carries only the id, not the tool name).  Most sessions never
    #    call these tools, so an empty set lets us skip the result scan
    #    entirely instead of walking the whole history twice every turn.
    target_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            if (tc.get("function") or {}).get("name") in tool_names:
                call_id = tc.get("id")
                if call_id:
                    target_ids.add(call_id)
    if not target_ids:
        return messages, 0

    # 2. Indices of the targeted tool-result messages, in conversation order.
    target_indices = [
        i
        for i, msg in enumerate(messages)
        if msg.get("role") == "tool" and msg.get("tool_call_id") in target_ids
    ]
    if len(target_indices) <= keep_last:
        return messages, 0

    # 3. Everything except the most recent ``keep_last`` is prunable.
    prune_indices = set(target_indices[: len(target_indices) - keep_last])

    result = list(messages)
    pruned = 0
    for i in prune_indices:
        content = result[i].get("content")
        if not isinstance(content, str):
            continue
        if content == placeholder or len(content) <= min_chars:
            continue
        result[i] = {**result[i], "content": placeholder}
        pruned += 1

    return result, pruned


class ContextCompressor:
    """Compresses conversation context when approaching the model's context limit.

    Algorithm:
      1. Prune old tool results (cheap, no LLM call)
      2. Protect head messages (system prompt + first exchange)
      3. Protect tail messages by token budget (most recent ~20K tokens)
      4. Summarise middle turns with structured LLM prompt
      5. On subsequent compactions, iteratively update the previous summary
    """

    def __init__(
        self,
        model_id: str,
        *,
        threshold_percent: float = 0.50,
        protect_first_n: int = 3,
        protect_last_n: int = 20,
        summary_target_ratio: float = 0.20,
        prune_browser_state: bool = True,
        browser_state_keep_last: int = 2,
        quiet_mode: bool = False,
        summary_model_override: str | None = None,
        base_url: str = "",
        api_key: str = "",
        model_overrides: dict[str, dict] | None = None,
        summary_client: Any | None = None,
    ) -> None:
        self._model_id = model_id
        info: ModelInfo | None = resolve_model_info(
            model_id,
            base_url=base_url,
            api_key=api_key,
            overrides=model_overrides,
        )
        if info is None and not quiet_mode:
            # Fallback is a correctness hazard — the compressor will
            # under- or over-compact relative to the real window.  Warn
            # so operators can add a config override rather than
            # silently running with a default.
            logger.warning(
                "Model %r not found in the static catalog, provider /models "
                "discovery, or config overrides — falling back to %d-token "
                "context window.  Add llm.models.%s.context_window in "
                "config.yaml to set the correct value.",
                model_id, _DEFAULT_CONTEXT_WINDOW, model_id,
            )
        self.context_length: int = info.context_window if info else _DEFAULT_CONTEXT_WINDOW

        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.summary_target_ratio = max(0.10, min(summary_target_ratio, 0.80))
        self.quiet_mode = quiet_mode
        self._prune_browser_state = prune_browser_state
        self._browser_state_keep_last = browser_state_keep_last

        self.compression_count = 0
        self._derive_budgets()

        if not quiet_mode:
            logger.info(
                "Context compressor initialized: model=%s context_length=%d "
                "threshold=%d (%.0f%%) target_ratio=%.0f%% tail_budget=%d",
                model_id, self.context_length, self.threshold_tokens,
                threshold_percent * 100, self.summary_target_ratio * 100,
                self.tail_token_budget,
            )

        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0

        self.summary_model = summary_model_override or ""
        self._summary_client = summary_client

        # Stores the previous compaction summary for iterative updates.
        self._previous_summary: Optional[str] = None
        self._summary_failure_cooldown_until: float = 0.0

    def _derive_budgets(self) -> None:
        """Recompute every budget derived from the context window.

        Called from ``__init__`` and again whenever the window is revised
        downwards, so the threshold and summary budgets never keep
        referring to a window the provider has already rejected.
        """
        self.threshold_tokens = int(self.context_length * self.threshold_percent)
        # Ratio is relative to the threshold, not to total context.
        self.tail_token_budget = int(self.threshold_tokens * self.summary_target_ratio)
        self.max_summary_tokens = min(
            int(self.context_length * 0.05), _SUMMARY_TOKENS_CEILING,
        )

    def set_context_length(self, context_length: int) -> bool:
        """Shrink the assumed context window and re-derive budgets.

        Returns ``True`` if the window actually moved.  Only shrinking is
        allowed: the caller reaches here because the provider rejected a
        request as too long, so a larger value would be the guess that
        just failed.
        """
        if context_length <= 0 or context_length >= self.context_length:
            return False
        self.context_length = context_length
        self._derive_budgets()
        return True

    # ------------------------------------------------------------------
    # Token tracking
    # ------------------------------------------------------------------

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Update tracked token usage from API response."""
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)

    def should_compress(
        self,
        messages: list[dict[str, Any]] | int,
        system_prompt: str = "",
    ) -> bool:
        """Check if context exceeds the compression threshold.

        Accepts either:
        - ``(messages, system_prompt)`` -- estimates tokens from the message
          list plus the system prompt string.
        - ``(prompt_tokens,)`` -- an integer token count (e.g. from API
          response usage).
        """
        if isinstance(messages, int):
            return messages >= self.threshold_tokens
        rough = _estimate_messages_tokens_rough(messages)
        if system_prompt:
            rough += len(system_prompt) // _CHARS_PER_TOKEN
        return rough >= self.threshold_tokens

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        """Quick pre-flight check using rough estimate (before API call)."""
        rough_estimate = _estimate_messages_tokens_rough(messages)
        return rough_estimate >= self.threshold_tokens

    def get_status(self) -> dict[str, Any]:
        """Get current compression status for display/logging."""
        return {
            "last_prompt_tokens": self.last_prompt_tokens,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": min(100, (self.last_prompt_tokens / self.context_length * 100)) if self.context_length else 0,
            "compression_count": self.compression_count,
        }

    # ------------------------------------------------------------------
    # Tool output pruning (cheap pre-pass, no LLM call)
    # ------------------------------------------------------------------

    def _prune_old_tool_results(
        self, messages: list[dict[str, Any]], protect_tail_count: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Replace old tool result contents with a short placeholder.

        Walks backward from the end, protecting the most recent
        ``protect_tail_count`` messages. Older tool results get their
        content replaced with a placeholder string.

        Returns (pruned_messages, pruned_count).
        """
        if not messages:
            return messages, 0

        result = [m.copy() for m in messages]
        pruned = 0
        prune_boundary = len(result) - protect_tail_count

        for i in range(prune_boundary):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if not content or content == _PRUNED_TOOL_PLACEHOLDER:
                continue
            # Only prune if the content is substantial (>200 chars).
            if len(content) > 200:
                result[i] = {**msg, "content": _PRUNED_TOOL_PLACEHOLDER}
                pruned += 1

        return result, pruned

    def prune_stale_browser_states(
        self, messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Drop superseded browser page snapshots from the replayed history.

        Keeps the most recent ``browser_state_keep_last`` ``browser_get_state``
        results and replaces older ones with a short placeholder.  No-op when
        disabled (``prune_browser_state=False``).

        Safe to run on every turn: it preserves tool-call pairing and does not
        itself write the durable event log.  (A later compaction runs on the
        already-pruned view, so superseded snapshots it would otherwise
        summarise are dropped -- an intended part of the reduction, not a
        separate write path.)  The model always keeps the current page state,
        while older snapshots, which the agent never re-reads (it re-fetches
        state on demand), stop being re-sent on every subsequent call.  This
        also lowers the token estimate that gates compaction, so compaction
        fires less often.
        """
        if not self._prune_browser_state:
            return messages
        pruned_messages, pruned_count = prune_superseded_tool_results(
            messages,
            tool_names=_SUPERSEDED_STATE_TOOLS,
            # Never below 1: the current page state must always survive, or the
            # agent is left with no DOM to act on.
            keep_last=max(1, self._browser_state_keep_last),
            placeholder=_PRUNED_TOOL_PLACEHOLDER,
        )
        if pruned_count and not self.quiet_mode:
            logger.info(
                "Pruned %d superseded browser state(s) before LLM call",
                pruned_count,
            )
        return pruned_messages

    # ------------------------------------------------------------------
    # Summarisation
    # ------------------------------------------------------------------

    def _compute_summary_budget(self, turns_to_summarize: list[dict[str, Any]]) -> int:
        """Scale summary token budget with the amount of content being compressed.

        The maximum scales with the model's context window (5% of context,
        capped at ``_SUMMARY_TOKENS_CEILING``) so large-context models get
        richer summaries instead of being hard-capped at 8K tokens.
        """
        content_tokens = _estimate_messages_tokens_rough(turns_to_summarize)
        budget = int(content_tokens * _SUMMARY_RATIO)
        return max(_MIN_SUMMARY_TOKENS, min(budget, self.max_summary_tokens))

    def _serialize_for_summary(self, turns: list[dict[str, Any]]) -> str:
        """Serialize conversation turns into labelled text for the summariser.

        Includes tool call arguments and result content (up to 3000 chars
        per message) so the summariser can preserve specific details like
        file paths, commands, and outputs.
        """
        parts: list[str] = []
        for msg in turns:
            role = msg.get("role", "unknown")
            content = msg.get("content") or ""

            # Tool results: keep more content than before (3000 chars).
            if role == "tool":
                tool_id = msg.get("tool_call_id", "")
                if len(content) > 3000:
                    content = content[:2000] + "\n...[truncated]...\n" + content[-800:]
                parts.append(f"[TOOL RESULT {tool_id}]: {content}")
                continue

            # Assistant messages: include tool call names AND arguments.
            if role == "assistant":
                if len(content) > 3000:
                    content = content[:2000] + "\n...[truncated]...\n" + content[-800:]
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tc_parts: list[str] = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            # Truncate long arguments but keep enough for context.
                            if len(args) > 500:
                                args = args[:400] + "..."
                            tc_parts.append(f"  {name}({args})")
                        else:
                            fn = getattr(tc, "function", None)
                            name = getattr(fn, "name", "?") if fn else "?"
                            tc_parts.append(f"  {name}(...)")
                    content += "\n[Tool calls:\n" + "\n".join(tc_parts) + "\n]"
                parts.append(f"[ASSISTANT]: {content}")
                continue

            # User and other roles.
            if len(content) > 3000:
                content = content[:2000] + "\n...[truncated]...\n" + content[-800:]
            parts.append(f"[{role.upper()}]: {content}")

        return "\n\n".join(parts)

    async def _generate_summary(
        self,
        turns_to_summarize: list[dict[str, Any]],
        llm_client: Any,
        *,
        pre_compress_guidance: str = "",
    ) -> Optional[str]:
        """Generate a structured summary of conversation turns.

        Uses a structured template (Goal, Progress, Decisions, Resolved/Pending
        Questions, Files, Remaining Work) with explicit preamble telling the
        summarizer not to answer questions.  When a previous summary exists,
        generates an iterative update instead of summarizing from scratch.

        Args:
            focus_topic: Optional focus string for guided compression.  When
                provided, the summariser prioritises preserving information
                related to this topic and is more aggressive about compressing
                everything else.  Inspired by Claude Code's ``/compact``.

        Returns None if all attempts fail — the caller should drop
        the middle turns without a summary rather than inject a useless
        placeholder.
        """
        now = time.monotonic()
        if now < self._summary_failure_cooldown_until:
            logger.debug(
                "Skipping context summary during cooldown (%.0fs remaining)",
                self._summary_failure_cooldown_until - now,
            )
            return None

        summary_budget = self._compute_summary_budget(turns_to_summarize)
        content_to_summarize = self._serialize_for_summary(turns_to_summarize)

        # Insights harvested by memory providers from the turns about to be
        # discarded.  Threaded into the summary so they survive compaction.
        guidance_block = ""
        if pre_compress_guidance and pre_compress_guidance.strip():
            guidance_block = (
                "\n\nMEMORY FACTS TO PRESERVE (carry these into the summary "
                "verbatim where relevant):\n"
                f"{pre_compress_guidance.strip()}\n"
            )

        # Preamble shared by both first-compaction and iterative-update prompts.
        # Inspired by OpenCode's "do not respond to any questions" instruction
        # and Codex's "another language model" framing.
        _summarizer_preamble = (
            "You are a summarization agent creating a context checkpoint. "
            "Your output will be injected as reference material for a DIFFERENT "
            "assistant that continues the conversation. "
            "Do NOT respond to any questions or requests in the conversation — "
            "only output the structured summary. "
            "Do NOT include any preamble, greeting, or prefix."
        )
        
        # Shared structured template (used by both paths).
        # Key changes vs v1:
        #   - "Pending User Asks" section (from Claude Code) explicitly tracks
        #     unanswered questions so the model knows what's resolved vs open
        #   - "Remaining Work" replaces "Next Steps" to avoid reading as active
        #     instructions
        #   - "Resolved Questions" makes it clear which questions were already
        #     answered (prevents model from re-answering them)
        _template_sections = f"""## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Progress
### Done
[Completed steps. Format: numbered list, one step per line, `N. <description> [Done]`. Use existing indices when updating from a previous summary; never renumber.
Example:
1. Wire OIDC discovery into auth middleware [Done]]
### In Progress
[Steps currently underway. Format: numbered list, `N. <description> [In Progress]`. Use existing indices when updating from a previous summary; never renumber.
Example:
2. Run integration tests against Auth0 staging [In Progress]]
### Blocked
[Steps blocked on external action. Format: numbered list, `N. <description> [Blocked: <reason>]`. Use existing indices when updating from a previous summary; never renumber.
Example:
3. Deploy to staging [Blocked: missing AUTH_SECRET env var]]

## Key Decisions
[Important technical decisions and why they were made]

## Resolved Questions
[Questions the user asked that were ALREADY answered — include the answer so the next assistant does not re-answer them]

## Pending User Asks
[Questions or requests from the user that have NOT yet been answered or fulfilled. If none, write "None."]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Remaining Work
[Future steps not yet started. Format: numbered list, `N. <description>`. Shares the SAME index space as ### Done / ### In Progress / ### Blocked. New steps get the next free index in the shared sequence.
Example:
4. Add cache invalidation on token refresh]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation]

## Tools & Patterns
[Which tools were used, how they were used effectively, and any tool-specific discoveries]

Target ~{summary_budget} tokens. Be specific — include file paths, command outputs, error messages, and concrete values rather than vague descriptions.

INDEX RULES for the four plan sections (### Done, ### In Progress, ### Blocked, ## Remaining Work): they share ONE integer index space. Each step has a stable index. When a step's status changes (e.g. In Progress → Done), MOVE it between sections KEEPING its index. New steps append with the next free index. NEVER renumber an existing step.

Write only the summary body. Do not include any preamble or prefix."""


        if self._previous_summary:
            # Iterative update: preserve existing info, add new progress
            prompt = f"""{_summarizer_preamble}

You are updating a context compaction summary. A previous compaction produced the summary below. New conversation turns have occurred since then and need to be incorporated.

PREVIOUS SUMMARY:
{self._previous_summary}

NEW TURNS TO INCORPORATE:
{content_to_summarize}{guidance_block}

Update the summary using this exact structure. PRESERVE all existing information that is still relevant. ADD new progress. Move items from "In Progress" to "Done" when completed. Move answered questions to "Resolved Questions". Remove information only if it is clearly obsolete.

CRITICAL — PLAN STEP INDEX PRESERVATION: Preserve every step index that appears in the four plan sections of PREVIOUS SUMMARY (### Done, ### In Progress, ### Blocked, ## Remaining Work). When a step's status changes, MOVE it between sections KEEPING its existing index. NEVER renumber an existing step. Append new steps with the next free index after the highest index in PREVIOUS SUMMARY. The index of a step is its identity — changing it means the next assistant will treat it as a new step and may re-do completed work.

{_template_sections}"""
        else:
            # First compaction: summarize from scratch
            prompt = f"""{_summarizer_preamble}

Create a structured handoff summary for a different assistant that will continue this conversation after earlier turns are compacted. The next assistant should be able to understand what happened without re-reading the original turns.

TURNS TO SUMMARIZE:
{content_to_summarize}{guidance_block}

Use this exact structure:

{_template_sections}"""

        # Pick summariser model: prefer surogate, fall back to session model.
        summariser_model = self.summary_model or "surogate"
        info = get_model_info(summariser_model)
        if info is None:
            summariser_model = self._model_id

        try:
            client = self._summary_client or llm_client
            response = await client.chat.completions.create(
                model=summariser_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=summary_budget * 2,
            )
            content = response.choices[0].message.content
            # Handle cases where content is not a string (e.g., dict from llama.cpp).
            if not isinstance(content, str):
                content = str(content) if content else ""
            summary = content.strip()
            # Fail-soft normalisation: if the summariser produced free-prose
            # plan sections, rewrite them as numbered lists so step identity
            # survives across compactions.
            try:
                summary = self._normalize_plan_sections(
                    summary, previous_summary=self._previous_summary,
                )
            except Exception:
                logger.warning(
                    "Plan-section normaliser failed; accepting summary as-is.",
                    exc_info=True,
                )
            # Store for iterative updates on next compaction.
            self._previous_summary = summary
            self._summary_failure_cooldown_until = 0.0
            return self._with_summary_prefix(summary)
        except Exception as e:
            self._summary_failure_cooldown_until = time.monotonic() + _SUMMARY_FAILURE_COOLDOWN_SECONDS
            logger.warning(
                "Failed to generate context summary: %s. "
                "Further summary attempts paused for %d seconds.",
                e,
                _SUMMARY_FAILURE_COOLDOWN_SECONDS,
            )
            return None

    # ------------------------------------------------------------------
    # Plan-section index normalisation
    # ------------------------------------------------------------------
    #
    # The four plan sections (### Done, ### In Progress, ### Blocked,
    # ## Remaining Work) share a single integer index space.  The
    # summariser is prompted to preserve indices across compactions but
    # smaller models (gpt-4o-mini, surogate) routinely produce free-prose
    # bodies under these headers.  Free prose loses step identity:
    # post-compaction the agent cannot tell "step 3: done" from
    # "step 3: still pending", and re-does completed work.
    #
    # ``_normalize_plan_sections`` is a fail-soft post-pass: it walks the
    # summary line-by-line and ensures every non-empty content line under
    # one of the four plan headers begins with ``N. ``.  Existing indices
    # are preserved verbatim.  When a previous summary is supplied (the
    # iterative path), indices observed there reserve the corresponding
    # numbers so newly-assigned indices don't collide.

    _PLAN_HEADERS_INDENT = {
        "### done": "[Done]",
        "### in progress": "[In Progress]",
        "### blocked": "[Blocked]",
        "## remaining work": "",
    }
    _ANY_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
    _INDEXED_LINE_RE = re.compile(r"^\s*(\d+)\.\s+")
    _BULLET_PREFIX_RE = re.compile(r"^[-*•]\s+")
    _PLACEHOLDER_LINE_RE = re.compile(r"^\s*[\[\(]")

    @classmethod
    def _normalize_plan_sections(
        cls, summary: str, previous_summary: str | None = None,
    ) -> str:
        """Rewrite plan sections so every top-level content line is ``N. ...``.

        Top-level lines (column 0) under a plan header are treated as
        items: already-indexed lines pass through verbatim with their
        index reserved; bare prose / bullet lines are stripped of any
        bullet marker and assigned the next free index.

        Indented lines (any leading whitespace on the raw line) are
        treated as **continuations** of the item above them and pass
        through unchanged — markdown sub-bullets, multi-line
        descriptions, and code-block content are preserved as the model
        wrote them.  This prevents the normaliser from inflating each
        sub-bullet of a richly-described step into its own (collision-
        prone) index.

        Empty lines and bracketed placeholder/example lines pass
        through unchanged.

        When ``previous_summary`` is supplied, indices observed in any
        of its plan sections (top-level or indented) reserve those
        numbers so a new index does not collide with one the previous
        summary still owns.
        """

        used: set[int] = set()
        if previous_summary:
            for line in previous_summary.splitlines():
                m = cls._INDEXED_LINE_RE.match(line)
                if m:
                    try:
                        used.add(int(m.group(1)))
                    except ValueError:
                        pass

        next_index = max(used, default=0) + 1

        def _take_index() -> int:
            nonlocal next_index
            while next_index in used:
                next_index += 1
            chosen = next_index
            used.add(chosen)
            next_index += 1
            return chosen

        out: list[str] = []
        current_section: str | None = None  # one of the keys of _PLAN_HEADERS_INDENT, lowercased

        for raw in summary.splitlines():
            stripped = raw.strip()

            if cls._ANY_HEADER_RE.match(raw):
                normalised = stripped.lower()
                current_section = (
                    normalised if normalised in cls._PLAN_HEADERS_INDENT else None
                )
                out.append(raw)
                continue

            if current_section is None or not stripped:
                out.append(raw)
                continue

            # Indented lines are continuations of the item above (sub-bullets,
            # multi-line descriptions, fenced code).  Pass through verbatim;
            # do NOT re-index, do NOT extract numbers as new items.  Items
            # at column 0 are the only candidates for the indexed contract.
            if raw[:1] in (" ", "\t"):
                out.append(raw)
                continue

            # Pass example / placeholder lines through unchanged so they
            # don't get re-indexed by the normaliser.
            if cls._PLACEHOLDER_LINE_RE.match(stripped):
                out.append(raw)
                continue

            indexed = cls._INDEXED_LINE_RE.match(stripped)
            if indexed:
                try:
                    used.add(int(indexed.group(1)))
                except ValueError:
                    pass
                out.append(raw)
                continue

            # Strip leading bullet/asterisk markers.
            cleaned = cls._BULLET_PREFIX_RE.sub("", stripped)
            if not cleaned:
                out.append(raw)
                continue

            # Skip lines that look like sub-headers/separators we missed.
            if cleaned.startswith("`") and cleaned.endswith("`"):
                out.append(raw)
                continue

            tag = cls._PLAN_HEADERS_INDENT[current_section]
            # If the model already emitted a status tag in the prose, don't
            # double-tag.
            if tag and tag.lower() in cleaned.lower():
                tag = ""
            new_line = f"{_take_index()}. {cleaned}"
            if tag:
                new_line = f"{new_line} {tag}"
            out.append(new_line)

        return "\n".join(out)

    def _recover_previous_summary(
        self,
        messages: list[dict[str, Any]],
        start: int,
        end: int,
    ) -> int:
        """Adopt the newest summary marker in ``messages[start:end]``.

        ``harness_factory`` rebuilds the compressor every wake, so
        ``_previous_summary`` is None on the first compaction of a turn even
        when the context already carries a summary from an earlier one.
        Without this the iterative-update prompt never fires across wakes and
        the stable plan-step index contract silently stops holding.

        Returns the marker's index so the caller can keep it out of the turns
        being summarised -- otherwise the same text is fed to the summariser
        twice, once as PREVIOUS SUMMARY and again verbatim as a new turn.
        """
        for idx in range(end - 1, start - 1, -1):
            content = messages[idx].get("content")
            if not isinstance(content, str):
                continue
            for prefix in (SUMMARY_PREFIX, LEGACY_SUMMARY_PREFIX):
                pos = content.find(prefix)
                if pos == -1:
                    continue
                if self._previous_summary is None:
                    body = content[pos + len(prefix):]
                    # A merged summary carries the live tail message after the
                    # delimiter; that part is not summary text.
                    body = body.split(SUMMARY_END_DELIMITER, 1)[0]
                    self._previous_summary = body.strip()
                return idx
        return -1

    @staticmethod
    def _with_summary_prefix(summary: str) -> str:
        """Normalise summary text to the current compaction handoff format."""
        text = (summary or "").strip()
        for prefix in (LEGACY_SUMMARY_PREFIX, SUMMARY_PREFIX):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                break
        return f"{SUMMARY_PREFIX}\n{text}" if text else SUMMARY_PREFIX

    # ------------------------------------------------------------------
    # Tool-call / tool-result pair integrity helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_tool_call_id(tc: Any) -> str:
        """Extract the call ID from a tool_call entry (dict or SimpleNamespace)."""
        if isinstance(tc, dict):
            return tc.get("id", "")
        return getattr(tc, "id", "") or ""

    def _sanitize_tool_pairs(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fix orphaned tool_call / tool_result pairs after compression.

        Two failure modes:
        1. A tool *result* references a call_id whose assistant tool_call was
           removed (summarised/truncated).  The API rejects this with
           "No tool call found for function call output with call_id ...".
        2. An assistant message has tool_calls whose results were dropped.
           The API rejects this because every tool_call must be followed by
           a tool result with the matching call_id.

        This method removes orphaned results and inserts stub results for
        orphaned calls so the message list is always well-formed.
        """
        surviving_call_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = self._get_tool_call_id(tc)
                    if cid:
                        surviving_call_ids.add(cid)

        result_call_ids: set[str] = set()
        for msg in messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_call_ids.add(cid)

        # 1. Remove tool results whose call_id has no matching assistant tool_call.
        orphaned_results = result_call_ids - surviving_call_ids
        if orphaned_results:
            messages = [
                m for m in messages
                if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
            ]
            if not self.quiet_mode:
                logger.info("Compression sanitizer: removed %d orphaned tool result(s)", len(orphaned_results))

        # 2. Add stub results for assistant tool_calls whose results were dropped.
        missing_results = surviving_call_ids - result_call_ids
        if missing_results:
            patched: list[dict[str, Any]] = []
            for msg in messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = self._get_tool_call_id(tc)
                        if cid in missing_results:
                            patched.append({
                                "role": "tool",
                                "content": "[Result from earlier conversation — see context summary above]",
                                "tool_call_id": cid,
                            })
            messages = patched
            if not self.quiet_mode:
                logger.info("Compression sanitizer: added %d stub tool result(s)", len(missing_results))

        return messages

    def _align_boundary_forward(self, messages: list[dict[str, Any]], idx: int) -> int:
        """Push a compress-start boundary forward past any orphan tool results.

        If ``messages[idx]`` is a tool result, slide forward until we hit a
        non-tool message so we don't start the summarised region mid-group.
        """
        while idx < len(messages) and messages[idx].get("role") == "tool":
            idx += 1
        return idx

    def _align_boundary_backward(self, messages: list[dict[str, Any]], idx: int) -> int:
        """Pull a compress-end boundary backward to avoid splitting a
        tool_call / result group.

        If the boundary falls in the middle of a tool-result group (i.e.
        there are consecutive tool messages before ``idx``), walk backward
        past all of them to find the parent assistant message.  If found,
        move the boundary before the assistant so the entire
        assistant + tool_results group is included in the summarised region
        rather than being split (which causes silent data loss when
        ``_sanitize_tool_pairs`` removes the orphaned tail results).
        """
        if idx <= 0 or idx >= len(messages):
            return idx
        # Walk backward past consecutive tool results.
        check = idx - 1
        while check >= 0 and messages[check].get("role") == "tool":
            check -= 1
        # If we landed on the parent assistant with tool_calls, pull the
        # boundary before it so the whole group gets summarised together.
        if check >= 0 and messages[check].get("role") == "assistant" and messages[check].get("tool_calls"):
            idx = check
        return idx

    # ------------------------------------------------------------------
    # Tail protection by token budget
    # ------------------------------------------------------------------

    def _find_tail_cut_by_tokens(
        self, messages: list[dict[str, Any]], head_end: int,
        token_budget: int | None = None,
    ) -> int:
        """Walk backward from the end of messages, accumulating tokens until
        the budget is reached. Returns the index where the tail starts.

        ``token_budget`` defaults to ``self.tail_token_budget`` which is
        derived from ``summary_target_ratio * context_length``, so it
        scales automatically with the model's context window.

        Never cuts inside a tool_call/result group. Falls back to the old
        ``protect_last_n`` if the budget would protect fewer messages.
        """
        if token_budget is None:
            token_budget = self.tail_token_budget
        n = len(messages)
        min_tail = self.protect_last_n
        accumulated = 0
        cut_idx = n  # start from beyond the end

        for i in range(n - 1, head_end - 1, -1):
            msg = messages[i]
            content = msg.get("content") or ""
            msg_tokens = len(content) // _CHARS_PER_TOKEN + 10  # +10 for role/metadata
            # Include tool call arguments in estimate.
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments", "")
                    msg_tokens += len(args) // _CHARS_PER_TOKEN
            if accumulated + msg_tokens > token_budget and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut_idx = i

        # Ensure we protect at least protect_last_n messages.
        fallback_cut = n - min_tail
        if cut_idx > fallback_cut:
            cut_idx = fallback_cut

        # If the token budget would protect everything (small conversations),
        # fall back to the fixed protect_last_n approach so compression can
        # still remove middle turns.
        if cut_idx <= head_end:
            cut_idx = fallback_cut

        # Align to avoid splitting tool groups.
        cut_idx = self._align_boundary_backward(messages, cut_idx)

        return max(cut_idx, head_end + 1)

    # ------------------------------------------------------------------
    # Main compression entry point
    # ------------------------------------------------------------------

    async def compress(
        self,
        messages: list[dict[str, Any]],
        llm_client: Any,
        current_tokens: int | None = None,
        *,
        pre_compress_guidance: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Compress conversation messages by summarising middle turns.

        Algorithm:
          1. Prune old tool results (cheap pre-pass, no LLM call)
          2. Protect head messages (system prompt + first exchange)
          3. Find tail boundary by token budget (~20K tokens of recent context)
          4. Summarise middle turns with structured LLM prompt
          5. On re-compression, iteratively update the previous summary

        After compression, orphaned tool_call / tool_result pairs are cleaned
        up so the API never receives mismatched IDs.

        Returns ``(compressed_messages, summary_data)`` where
        *summary_data* is a dict suitable for the ``CONTEXT_COMPACT`` event
        payload.
        """
        n_messages = len(messages)
        original_tokens = _estimate_messages_tokens_rough(messages)

        if n_messages <= self.protect_first_n + self.protect_last_n + 1:
            if not self.quiet_mode:
                logger.warning(
                    "Cannot compress: only %d messages (need > %d)",
                    n_messages,
                    self.protect_first_n + self.protect_last_n + 1,
                )
            return messages, self._build_summary_data(
                original_count=n_messages,
                original_tokens=original_tokens,
                compressed_count=n_messages,
                compressed_tokens=original_tokens,
                strategy="too_few_messages",
            )

        display_tokens = current_tokens if current_tokens else self.last_prompt_tokens or _estimate_messages_tokens_rough(messages)

        # Phase 1: Prune old tool results (cheap, no LLM call).
        messages, pruned_count = self._prune_old_tool_results(
            messages, protect_tail_count=self.protect_last_n * 3,
        )
        if pruned_count and not self.quiet_mode:
            logger.info("Pre-compression: pruned %d old tool result(s)", pruned_count)

        # Phase 2: Determine boundaries.
        compress_start = self.protect_first_n
        compress_start = self._align_boundary_forward(messages, compress_start)

        # Use token-budget tail protection instead of fixed message count.
        compress_end = self._find_tail_cut_by_tokens(messages, compress_start)

        if compress_start >= compress_end:
            return messages, self._build_summary_data(
                original_count=n_messages,
                original_tokens=original_tokens,
                compressed_count=len(messages),
                compressed_tokens=_estimate_messages_tokens_rough(messages),
                strategy="boundaries_overlap",
            )

        summary_idx = self._recover_previous_summary(
            messages, compress_start, compress_end,
        )
        turns_to_summarize = [
            msg
            for i, msg in enumerate(
                messages[compress_start:compress_end], compress_start,
            )
            if i != summary_idx
        ]

        if not self.quiet_mode:
            logger.info(
                "Context compression triggered (%d tokens >= %d threshold)",
                display_tokens,
                self.threshold_tokens,
            )
            logger.info(
                "Model context limit: %d tokens (%.0f%% = %d)",
                self.context_length,
                self.threshold_percent * 100,
                self.threshold_tokens,
            )
            tail_msgs = n_messages - compress_end
            logger.info(
                "Summarizing turns %d-%d (%d turns), protecting %d head + %d tail messages",
                compress_start + 1,
                compress_end,
                len(turns_to_summarize),
                compress_start,
                tail_msgs,
            )

        # Phase 3: Generate structured summary.
        summary = await self._generate_summary(
            turns_to_summarize,
            llm_client,
            pre_compress_guidance=pre_compress_guidance,
        )

        # A summariser outage still has to free tokens, but dropping the
        # middle silently makes the agent re-do finished work and contradict
        # its own decisions with no visible cause.  Say so in-context and in
        # the event payload instead; placement then reuses the normal path.
        summary_failed = summary is None
        if summary_failed:
            summary = (
                f"{SUMMARY_PREFIX}\n[{len(turns_to_summarize)} earlier messages "
                "were dropped to free context space and could not be summarised "
                "-- the summarisation model was unavailable. That detail is not "
                "recoverable here; ask the user rather than assuming.]"
            )

        # Phase 4: Assemble compressed message list.
        compressed: list[dict[str, Any]] = []
        for i in range(compress_start):
            msg = messages[i].copy()
            if i == 0 and msg.get("role") == "system" and self.compression_count == 0:
                msg["content"] = (
                    (msg.get("content") or "")
                    + "\n\n[Note: Some earlier conversation turns have been compacted into a handoff summary to preserve context space. The current session state may still reflect earlier work, so build on that summary and state rather than re-doing work.]"
                )
            compressed.append(msg)

        _merge_summary_into_tail = False
        if summary:
            last_head_role = messages[compress_start - 1].get("role", "user") if compress_start > 0 else "user"
            first_tail_role = messages[compress_end].get("role", "user") if compress_end < n_messages else "user"
            # Pick a role that avoids consecutive same-role with both neighbours.
            # Priority: avoid colliding with head (already committed), then tail.
            if last_head_role in ("assistant", "tool"):
                summary_role = "user"
            else:
                summary_role = "assistant"
            # If the chosen role collides with the tail AND flipping wouldn't
            # collide with the head, flip it.
            if summary_role == first_tail_role:
                flipped = "assistant" if summary_role == "user" else "user"
                if flipped != last_head_role:
                    summary_role = flipped
                else:
                    # Both roles would create consecutive same-role messages
                    # (e.g. head=assistant, tail=user — neither role works).
                    # Merge the summary into the first tail message instead
                    # of inserting a standalone message that breaks alternation.
                    _merge_summary_into_tail = True
            if not _merge_summary_into_tail:
                compressed.append({"role": summary_role, "content": summary})

        for i in range(compress_end, n_messages):
            msg = messages[i].copy()
            if _merge_summary_into_tail and i == compress_end:
                original = msg.get("content") or ""
                msg["content"] = (
                    summary + "\n\n" + SUMMARY_END_DELIMITER + "\n\n" + original
                )
                _merge_summary_into_tail = False
            compressed.append(msg)

        self.compression_count += 1

        compressed = self._sanitize_tool_pairs(compressed)

        compressed_tokens = _estimate_messages_tokens_rough(compressed)
        if not self.quiet_mode:
            saved_estimate = display_tokens - compressed_tokens
            logger.info(
                "Compressed: %d -> %d messages (~%d tokens saved)",
                n_messages,
                len(compressed),
                saved_estimate,
            )
            logger.info("Compression #%d complete", self.compression_count)

        return compressed, self._build_summary_data(
            original_count=n_messages,
            original_tokens=original_tokens,
            compressed_count=len(compressed),
            compressed_tokens=compressed_tokens,
            strategy="summary_unavailable" if summary_failed else "summarise",
            summary=summary,
        )

    @staticmethod
    def _build_summary_data(
        *,
        original_count: int,
        original_tokens: int,
        compressed_count: int,
        compressed_tokens: int,
        strategy: str,
        summary: str | None = None,
    ) -> dict[str, Any]:
        """Build the data payload for a ``CONTEXT_COMPACT`` event."""
        data: dict[str, Any] = {
            "original_message_count": original_count,
            "original_token_estimate": original_tokens,
            "compressed_message_count": compressed_count,
            "compressed_token_estimate": compressed_tokens,
            "strategy": strategy,
        }
        if summary is not None:
            data["summary"] = summary
        return data
