"""Per-iteration and per-turn LLM summaries for the Simple chat view.

The :class:`TurnSummarizer` runs against the existing ``summary_model``
auxiliary LLM (the cheap model already wired up for context compression
and title generation) and produces:

* one-line imperative summaries for individual LLM iterations
  ("Rework hero paragraph to introduce brain/hands metaphor"), and
* a per-turn recap plus a curated artifact list (TurnSummaryCard).

Both methods degrade gracefully on timeouts, malformed responses, and
unconfigured clients: they return ``None`` and the harness's caller is
expected to omit the summary event rather than fail the turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Soft cap on each model call so a hung provider can't stall the turn.
_SUMMARY_TIMEOUT_SECONDS: float = 10.0

_MAX_ITERATION_SUMMARY_TOKENS: int = 64
_MAX_TURN_SUMMARY_TOKENS: int = 512

TurnArtifactKind = Literal["file", "artifact", "url", "command"]


@dataclass(frozen=True)
class TurnArtifact:
    """A single artifact reference shown in :class:`TurnSummaryCard`.

    ``ref`` semantics depend on ``kind``:

    * ``file``     — workspace-relative file path
    * ``artifact`` — artifact id (matches ``artifact.created`` system event)
    * ``url``      — absolute URL
    * ``command``  — tool-call id of the originating ``terminal`` call
    """

    kind: TurnArtifactKind
    label: str
    ref: str
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class TurnSummary:
    """Per-turn recap. ``recap`` is 1–3 short sentences in plain prose."""

    recap: str
    artifacts: list[TurnArtifact] = field(default_factory=list)


_ITERATION_PROMPT = (
    "You write one short imperative sentence describing what an agent "
    "just did in this iteration. Be specific and concrete. No quotes, "
    "no period at the end, no leading 'The agent', max 12 words."
)

_TURN_PROMPT = (
    "Summarize what an agent accomplished in this turn for the user. "
    "Return ONLY a JSON object with two fields:\n"
    "  recap: 1-3 short sentences in plain prose, no markdown\n"
    "  artifacts: a curated subset of the candidate artifacts the user "
    "would want quick access to. Drop noisy read-only lookups; keep "
    "files written/edited, created artifacts, fetched URLs, and "
    "notable commands. Each artifact is "
    '{"kind": "file|artifact|url|command", "label": str, "ref": str}.'
)


_VALID_KINDS: frozenset[str] = frozenset({"file", "artifact", "url", "command"})


class TurnSummarizer:
    """Produce one-line iteration summaries and per-turn recaps."""

    def __init__(self, *, summary_client: Any, summary_model: str) -> None:
        self._client = summary_client
        self._model = summary_model

    async def summarize_iteration(
        self,
        *,
        iteration_id: str,
        reasoning: str,
        tool_calls: list[dict[str, Any]],
        prior_iteration_summaries: list[str],
    ) -> str | None:
        """Summarize a single LLM iteration as a one-line imperative.

        Returns ``None`` when there's nothing to summarize, the model
        returns a blank response, or the call fails / times out.
        """
        if not reasoning and not tool_calls:
            return None

        tool_lines = self._format_tool_calls(tool_calls)
        user_block_parts: list[str] = []
        if prior_iteration_summaries:
            prior = "\n".join(f"- {s}" for s in prior_iteration_summaries)
            user_block_parts.append(f"Earlier in this turn:\n{prior}")
        if reasoning:
            user_block_parts.append(f"Reasoning:\n{reasoning[:2000]}")
        if tool_lines:
            user_block_parts.append("Tools called:\n" + "\n".join(tool_lines))
        user_block = "\n\n".join(user_block_parts)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _ITERATION_PROMPT},
                {"role": "user", "content": user_block},
            ],
            "max_tokens": _MAX_ITERATION_SUMMARY_TOKENS,
            "temperature": 0.2,
            "stream": False,
        }

        content = await self._chat_completion(
            kwargs, label=f"iteration {iteration_id}",
        )
        if content is None:
            return None
        text = content.strip().strip('"').rstrip(".")
        return text or None

    async def summarize_turn(
        self,
        *,
        turn_id: str,
        user_message: str,
        iteration_summaries: list[str],
        candidate_artifacts: list[TurnArtifact],
    ) -> TurnSummary | None:
        """Summarize a whole assistant turn into recap + artifact list.

        Returns ``None`` when the turn has nothing worth summarizing, the
        model returns invalid JSON, or the recap and artifact list both
        end up empty after filtering.
        """
        if not iteration_summaries and not candidate_artifacts:
            return None

        cand_lines = "\n".join(
            f"- kind={a.kind} label={a.label!r} ref={a.ref!r}"
            for a in candidate_artifacts
        )
        user_block_parts: list[str] = [f"User asked: {user_message[:1000]}"]
        if iteration_summaries:
            user_block_parts.append(
                "Iteration summaries:\n"
                + "\n".join(f"- {s}" for s in iteration_summaries)
            )
        if cand_lines:
            user_block_parts.append(f"Candidate artifacts:\n{cand_lines}")
        user_block = "\n\n".join(user_block_parts)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _TURN_PROMPT},
                {"role": "user", "content": user_block},
            ],
            "max_tokens": _MAX_TURN_SUMMARY_TOKENS,
            "temperature": 0.3,
            "stream": False,
            "response_format": {"type": "json_object"},
        }

        content = await self._chat_completion(kwargs, label=f"turn {turn_id}")
        if not content:
            return None

        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "turn summary returned non-JSON for %s: %r",
                turn_id,
                content[:200],
            )
            return None
        if not isinstance(parsed, dict):
            return None

        recap = str(parsed.get("recap") or "").strip()
        artifacts = self._parse_artifacts(parsed.get("artifacts"))
        if not recap and not artifacts:
            return None
        return TurnSummary(recap=recap, artifacts=artifacts)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _chat_completion(
        self,
        kwargs: dict[str, Any],
        *,
        label: str,
    ) -> str | None:
        """Run a single chat completion under the summary timeout.

        Returns the message content on success, ``None`` on any failure
        (timeout, network error, malformed response shape).
        """
        try:
            response = await asyncio.wait_for(
                self._client.chat.completions.create(**kwargs),
                timeout=_SUMMARY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("summary timed out for %s", label)
            return None
        except Exception as exc:
            logger.warning("summary call failed for %s: %r", label, exc)
            return None

        try:
            return response.choices[0].message.content
        except (AttributeError, IndexError, TypeError):
            logger.warning("summary response had unexpected shape for %s", label)
            return None

    @staticmethod
    def _format_tool_calls(tool_calls: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or "?"
            args = fn.get("arguments") or tc.get("arguments") or ""
            args_snippet = (args or "")[:200]
            out.append(f"{name}({args_snippet})")
        return out

    @staticmethod
    def _parse_artifacts(raw: Any) -> list[TurnArtifact]:
        if not isinstance(raw, list):
            return []
        out: list[TurnArtifact] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            label = item.get("label")
            ref = item.get("ref")
            if kind not in _VALID_KINDS:
                continue
            if not isinstance(label, str) or not isinstance(ref, str):
                continue
            if not label or not ref:
                continue
            out.append(TurnArtifact(kind=kind, label=label, ref=ref))
        return out
