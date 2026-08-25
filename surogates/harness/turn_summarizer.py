"""Per-iteration and per-turn LLM summaries for the Simple chat view.

The :class:`TurnSummarizer` produces:

* one-line imperative summaries for individual LLM iterations
  ("Rework hero paragraph to introduce brain/hands metaphor"), run on
  the cheap ``summary_model`` auxiliary LLM (already wired up for
  context compression and title generation) because they fire on every
  iteration, and
* a per-turn recap plus a curated list of downloadable artifacts
  (TurnSummaryCard), run on the agent's base model — picking the
  user's actual deliverable out of a pile of intermediate workspace
  files needs the stronger model, and it only runs once per turn.
  Only downloadable deliverables — workspace files and created
  artifacts — are surfaced; URLs and commands are not.

Both methods degrade gracefully on timeouts, malformed responses, and
unconfigured clients: they return ``None`` and the harness's caller is
expected to omit the summary event rather than fail the turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from surogates.harness.streaming_executor import _is_error_result
from surogates.harness.structured_output import parse_json_object

logger = logging.getLogger(__name__)

# Soft caps so a hung provider can't stall the turn. The turn summary
# runs on the base model, which is slower than the cheap summary model.
_ITERATION_SUMMARY_TIMEOUT_SECONDS: float = 10.0
_TURN_SUMMARY_TIMEOUT_SECONDS: float = 30.0

_MAX_ITERATION_SUMMARY_TOKENS: int = 64
_MAX_TURN_SUMMARY_TOKENS: int = 512

TurnArtifactKind = Literal["file", "artifact"]


@dataclass(frozen=True)
class TurnArtifact:
    """A single downloadable artifact shown in :class:`TurnSummaryCard`.

    ``ref`` semantics depend on ``kind``:

    * ``file``     — workspace-relative file path
    * ``artifact`` — artifact id (matches ``artifact.created`` system event)
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
    "You write one short sentence describing what the agent learned or "
    "accomplished in this iteration. Each tool call is paired with a "
    "short snippet of its result — use the result, not just the call, "
    "to decide the label. Two consecutive calls that look identical "
    "(e.g. several `python3 -c \"...Presentation...\"` commands) often "
    "do different things; distinguish them by their result. If a call "
    "failed, say so (e.g. 'Find pdftotext fails — falling back to "
    "pypdf'). Be specific and concrete. No quotes, no period at the "
    "end, no leading 'The agent', max 12 words.\n"
    "You are an observer writing a caption, not the agent. Never "
    "continue the agent's work, never speak as the agent ('I will…', "
    "'Let me…'), and never call a tool. Reply with the caption text "
    "alone: one line, no newlines, no bullet points, no JSON, no "
    "markup, and never repeat the transcript lines you were given."
)

_TURN_PROMPT = (
    "You are reviewing a completed agent turn. Return ONLY a JSON "
    "object with two fields:\n"
    "  recap: 1-3 short sentences in plain prose, no markdown, "
    "summarizing what the agent accomplished\n"
    "  artifacts: the downloadable deliverable(s) that satisfy what "
    "the user asked for. Re-read the user request first and work "
    "backwards from it: what file(s) did the user actually ask to "
    "receive? List those and nothing else — usually a single file. "
    "This list becomes the user-visible download card, so an "
    "intermediate file here is worse than a missing one.\n"
    "    KEEP only the final deliverable matching the request: asked "
    "for a presentation -> the .pptx; a report -> the .pdf/.docx/.md; "
    "a dataset -> the .csv/.xlsx; an image/video -> that media file; "
    "a created artifact -> that artifact.\n"
    "    DROP everything intermediate: scripts the agent wrote and "
    "ran itself (executed_by_terminal=true is almost always "
    "scaffolding), source-code files (.py, .sh, .js, .ts) unless the "
    "user explicitly asked for code, assets generated only to be "
    "embedded in the final deliverable (e.g. chart images rendered "
    "for a .pptx), scratch files, downloads the agent fetched as "
    "inputs, debugging output, and internal agent state (anything "
    "under a hidden directory like .agents/ is context for future "
    "turns, not a deliverable).\n"
    "  Each artifact is "
    '{"kind": "file|artifact", "label": str, "ref": str} — copy '
    "kind and ref verbatim from the matching candidate. Return an "
    "empty artifacts list when no candidate is a real deliverable "
    "for this user request."
)


_VALID_KINDS: frozenset[str] = frozenset({"file", "artifact"})



# Openers that mark the summary model role-playing the agent instead of
# captioning it ("I'll load the skill…", "Let me check…"). Matched
# case-insensitively against the first words of the reply.
_ROLE_PLAY_OPENERS: tuple[str, ...] = (
    "i'll ", "i will ", "i am ", "i'm ", "i need ", "i should ",
    "i can ", "i cannot ", "i've ", "i have ", "let me ", "let's ",
    "based on ", "first, ", "next, ",
)

# Markup that only ever appears when a model's native tool-call syntax
# surfaces as literal text instead of being parsed. ``｜`` (U+FF5C) is
# the DeepSeek delimiter; the angle-bracket forms cover the XML-style
# dialects; a fence means the reply is a code block, not a caption.
_MARKUP_LEAK_MARKERS: tuple[str, ...] = (
    "<tool_call", "<invoke", "<function", "\uff5c", "```",
)


def _is_self_describing_iteration(
    tool_calls: list[dict[str, Any]],
    tool_results: list[dict[str, Any]],
) -> bool:
    """True when the iteration's arguments already say everything.

    A successful ``skill_view`` is the only such iteration: its whole
    content is *which skill was loaded*, which the arguments state
    exactly. A caption can only restate that, less reliably, for the
    price of a model call.

    The qualifiers all guard against a caption that would have carried
    real information:

    * an empty batch is a text-only iteration — nothing to restate;
    * uncaptured results cannot be confirmed successful;
    * a *failed* load is news, and consumers that drop errored calls
      would otherwise show nothing at all. The prompt asks for exactly
      this ("if a call failed, say so").
    """
    if not tool_calls or not tool_results:
        return False
    names = {
        (tc.get("function") or {}).get("name") or tc.get("name")
        for tc in tool_calls
    }
    if names != {"skill_view"}:
        return False
    return not any(_is_error_result(tr) for tr in tool_results)


# The prompt forbids a narrator opener and the model uses one anyway in
# 6.6% of replies. Both observed forms are followed by a past-tense verb
# ("The agent reviewed…", "Agent found…"), never by a noun, so the
# prefix strips cleanly.
_NARRATOR_OPENER_RE = re.compile(r"^(?:the\s+)?agent\s+", re.IGNORECASE)


def _normalize_caption(text: str) -> str:
    """Drop the narrator prefix so the row reads as a caption.

    "The agent reviewed the rubric" → "Reviewed the rubric". The row
    already sits under the agent's own turn; naming it again is noise
    that costs a third of the line's visible width.
    """
    stripped = _NARRATOR_OPENER_RE.sub("", text, count=1)
    if stripped == text:
        return text
    return stripped[:1].upper() + stripped[1:]


def _valid_iteration_summary(text: str) -> bool:
    """True when ``text`` is a usable one-line caption.

    The cheap summary model periodically completes the prompt's
    transcript instead of captioning it: it echoes the ``call: … /
    result: …`` lines verbatim, returns the raw JSON tool result, emits
    its own tool-call markup as literal text, or continues the turn in
    the agent's first-person voice. All of those reach the user as the
    iteration's visible label, so they are rejected here and the caller
    emits no event — chat clients then fall back to their deterministic
    tool-derived label, which is what the row should have said anyway.
    """
    if not text:
        return False
    # A caption is a single line. Every observed structural failure
    # (transcript echo, bullet list, markup dump) is multi-line.
    if "\n" in text or "\r" in text:
        return False
    # Typographic apostrophes are normalized so a curly "I\u2019ll load the
    # skill" is caught by the same openers as the ASCII form.
    lowered = text.lower().replace("\u2019", "'")
    # The reply repeating the transcript it was handed. Keyed on
    # co-occurring field markers, because ``call:`` and ``result:`` turn
    # up in ordinary captions ("Search returns no result: falls back to
    # pypdf") while no caption ever pairs ``tool=`` with ``args=``.
    if (
        ("tool=" in lowered and "args=" in lowered)
        or ("call:" in lowered and "result:" in lowered)
        or lowered.startswith(("call:", "tools called"))
    ):
        return False
    if any(marker in lowered for marker in _MARKUP_LEAK_MARKERS):
        return False
    stripped = text.lstrip()
    # A JSON/array body, or a markdown bullet, is structure not prose.
    if stripped[:1] in {"{", "[", "#", "|"}:
        return False
    if stripped[:2] in {"- ", "* ", "> "}:
        return False
    if lowered.startswith(_ROLE_PLAY_OPENERS):
        return False
    # A caption is one short line. Anything appreciably longer is the
    # model dumping the transcript back rather than summarizing it, and
    # the render surface truncates past this regardless.
    if len(text.split()) > 30:
        return False
    return True


def _is_internal_workspace_path(path: str) -> bool:
    """True for workspace paths that are never user deliverables.

    Any hidden path segment marks agent-internal state (``.agents/``
    skill context files, ``.claude/`` config, ``.cache/`` …), and
    ``uploads/`` holds user-provided attachments — inputs, not
    outputs. Filtered deterministically so they never reach the
    summary LLM as candidates nor the user-visible download card.
    """
    segments = [s for s in path.split("/") if s]
    if any(s.startswith(".") for s in segments):
        return True
    return bool(segments) and segments[0] == "uploads"


class TurnSummarizer:
    """Produce one-line iteration summaries and per-turn recaps.

    Iteration summaries run on the cheap auxiliary ``summary_model``
    (they fire on every iteration); the per-turn recap + artifact
    curation runs on the agent's base model, which is reliable enough
    to pick the user's actual deliverable out of intermediate files.
    """

    def __init__(
        self,
        *,
        base_client: Any,
        base_model: str,
        summary_client: Any | None = None,
        summary_model: str = "",
    ) -> None:
        self._base_client = base_client
        self._base_model = base_model
        self._summary_client = summary_client
        self._summary_model = summary_model

    async def summarize_iteration(
        self,
        *,
        iteration_id: str,
        reasoning: str,
        tool_calls: list[dict[str, Any]],
        prior_iteration_summaries: list[str],
        tool_results: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Summarize a single LLM iteration as a one-line imperative.

        Returns ``None`` when there's nothing to summarize, no cheap
        summary model is configured, the model returns a blank
        response, or the call fails / times out.
        """
        if self._summary_client is None or not self._summary_model:
            return None
        if not reasoning and not tool_calls:
            return None
        if _is_self_describing_iteration(tool_calls, tool_results or []):
            return None

        tool_lines = self._format_tool_calls(tool_calls, tool_results or [])
        user_block_parts: list[str] = []
        if prior_iteration_summaries:
            prior = "\n".join(f"- {s}" for s in prior_iteration_summaries)
            user_block_parts.append(f"Earlier in this turn:\n{prior}")
        if reasoning:
            user_block_parts.append(f"Reasoning:\n{reasoning[:2000]}")
        if tool_lines:
            user_block_parts.append(
                "Tools called (with result snippets):\n"
                + "\n".join(tool_lines)
            )
        user_block = "\n\n".join(user_block_parts)

        kwargs: dict[str, Any] = {
            "model": self._summary_model,
            "messages": [
                {"role": "system", "content": _ITERATION_PROMPT},
                {"role": "user", "content": user_block},
            ],
            "max_tokens": _MAX_ITERATION_SUMMARY_TOKENS,
            "temperature": 0.2,
            "stream": False,
        }

        content = await self._chat_completion(
            self._summary_client,
            kwargs,
            label=f"iteration {iteration_id}",
            timeout=_ITERATION_SUMMARY_TIMEOUT_SECONDS,
        )
        if content is None:
            return None
        text = _normalize_caption(content.strip().strip('"').rstrip("."))
        if not _valid_iteration_summary(text):
            logger.warning(
                "discarding malformed iteration summary for %s: %r",
                iteration_id,
                text[:200],
            )
            return None
        return text

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
            + (
                " executed_by_terminal=true"
                if (a.meta or {}).get("executed_by_terminal")
                else ""
            )
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
            "model": self._base_model,
            "messages": [
                {"role": "system", "content": _TURN_PROMPT},
                {"role": "user", "content": user_block},
            ],
            "max_tokens": _MAX_TURN_SUMMARY_TOKENS,
            "temperature": 0.3,
            "stream": False,
            "response_format": {"type": "json_object"},
        }

        content = await self._chat_completion(
            self._base_client,
            kwargs,
            label=f"turn {turn_id}",
            timeout=_TURN_SUMMARY_TIMEOUT_SECONDS,
        )
        if not content:
            return None

        # Fence-tolerant: gateways that ignore ``response_format``
        # return the object wrapped in ```json fences — a strict parse
        # here once silenced recaps entirely.
        parsed = parse_json_object(content)
        if parsed is None:
            logger.warning(
                "turn summary returned no JSON object for %s: %r",
                turn_id,
                content[:200],
            )
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
        client: Any,
        kwargs: dict[str, Any],
        *,
        label: str,
        timeout: float,
    ) -> str | None:
        """Run a single chat completion under the given timeout.

        Returns the message content on success, ``None`` on any failure
        (timeout, network error, malformed response shape).
        """
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(**kwargs),
                timeout=timeout,
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
    def _format_tool_calls(
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
    ) -> list[str]:
        # Index results by tool_call_id so each call is paired with the
        # right result regardless of execution order. Two parallel
        # calls in the same iteration can return in either order.
        results_by_id: dict[str, str] = {}
        for tr in tool_results:
            call_id = str(tr.get("tool_call_id") or "")
            if not call_id:
                continue
            content = tr.get("content")
            if isinstance(content, str):
                results_by_id[call_id] = content
            elif isinstance(content, list):
                # Multipart content: concatenate text parts only.
                parts: list[str] = []
                for p in content:
                    if isinstance(p, dict) and isinstance(p.get("text"), str):
                        parts.append(p["text"])
                results_by_id[call_id] = "\n".join(parts)
        out: list[str] = []
        for index, tc in enumerate(tool_calls, 1):
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or "?"
            args = fn.get("arguments") or tc.get("arguments") or ""
            args_snippet = (args or "")[:200]
            call_id = str(tc.get("id") or "")
            result = results_by_id.get(call_id, "")
            # Keep result snippets short — the summarizer only needs
            # enough to tell two calls apart, not the full output.
            result_snippet = result[:300] if result else "(no result captured)"
            # Numbered and field-labelled rather than ``call: …`` —
            # a transcript line must not read like a plausible caption,
            # or the model completes the list instead of summarizing it.
            out.append(
                f"[{index}] tool={name} args={args_snippet}\n"
                f"    returned: {result_snippet}"
            )
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
            # The summary card only presents downloadable artifacts.
            # The LLM occasionally smuggles a web URL through as
            # kind=file; an absolute URL is not downloadable from the
            # workspace, so drop it rather than render a dead entry.
            if ref.startswith(("http://", "https://")):
                continue
            # Candidates are pre-filtered, but the model can still
            # invent refs — never let internal paths reach the card.
            if kind == "file" and _is_internal_workspace_path(ref):
                continue
            out.append(TurnArtifact(kind=kind, label=label, ref=ref))
        return out
