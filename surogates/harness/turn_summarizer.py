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
from surogates.harness.message_utils import message_texts
from surogates.harness.structured_output import (
    iter_json_objects,
    parse_json_object,
)

logger = logging.getLogger(__name__)

# Soft caps so a hung provider can't stall the turn. The turn summary
# runs on the base model, which is slower than the cheap summary model.
_ITERATION_SUMMARY_TIMEOUT_SECONDS: float = 10.0
_TURN_SUMMARY_TIMEOUT_SECONDS: float = 30.0

# The ``{"caption": …}`` wrapper costs tokens the caption used to have
# to itself, and a reply truncated mid-object parses to nothing at all
# where a truncated string was still usable.
_MAX_ITERATION_SUMMARY_TOKENS: int = 96
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


# Demonstrated rather than specified. The rule-list version of this
# prompt forbade a narrator opener and got one anyway in 6.6% of
# replies; a style this small is cheaper to show than to legislate, and
# the examples fix the tense, the length and the voice at once. The
# stated shape is the row's real shape -- IterationGroup renders it in a
# single truncating line -- so the model can reason about the budget
# instead of counting to a word limit it was never shown.
_ITERATION_PROMPT = (
    "You caption one step of an agent's work for someone watching it "
    "happen.\n"
    # Deliberately NOT the git-commit-subject framing Claude Code uses.
    # That wording cut clipped captions from ~45 to ~8 per 140 steps, but
    # compressed similar steps onto the same words: after de-duplication
    # it surfaced 94 rows where this wording surfaces 107. A row that
    # exists and gets clipped was judged more useful than no row at all,
    # so length stays loose and the distinctness instruction does the
    # work. Do not "tighten" this back up without re-measuring row yield.
    "The caption is one line, past tense, leading with the most "
    "distinctive noun. Keep the detail that makes this step "
    "recognisable — a row identical to the one above it is worse than a "
    "long one. Around 12 words. No quotes, no trailing period.\n"
    "Each tool call comes with a snippet of its result. Caption what the "
    "result showed, not what the call attempted — two calls that look "
    "identical often do different things. When a call failed, that is "
    "the news: say so.\n"
    # The research examples are load-bearing. With only file/code
    # examples the model mirrored the voice of whatever it had just read:
    # on a web_search turn it wrote the source's headline in the present
    # tense ("JWST reveals a secondary atmosphere on 55 Cancri e") rather
    # than what the step found. Showing the shape on a research step is
    # what pins the tense there.
    "Examples:\n"
    "- Read the grading rubric from uploads\n"
    "- pdftotext missing, fell back to pypdf\n"
    "- Found three failing cases in test_parser\n"
    "- Rewrote the hero paragraph around the brain/hands metaphor\n"
    "- Slide render crashed on an empty title\n"
    "- Searched for the invoice template, no match\n"
    "- Searched for 55 Cancri e, found a secondary atmosphere\n"
    "- Pulled the ESA release, no 2024 figures in it\n"
    "- Read the WASP-107b paper, methane implies a large core\n"
    "When the result is an article, a paper or a search hit, caption what "
    "the step turned up — never restate the source's own headline, and "
    "never borrow its present tense. 'JWST reveals a new atmosphere' is "
    "the article's title; 'Found a secondary atmosphere on 55 Cancri e' "
    "is the caption.\n"
    "You are an observer writing a caption, never the agent: no 'I "
    "will…', no 'Let me…', never continue the work, never call a tool, "
    "and never repeat the transcript lines you were given.\n"
    # Brevity and distinctiveness pull against each other: asking for
    # short git-subject captions measurably raised the repeat rate on
    # real transcripts. This paragraph, a bare "say something NEW", and
    # a word-count ceiling were each measured and none of them fixed it
    # -- _duplicates_prior_caption does. Kept because it costs nothing
    # and shapes what a non-duplicate caption says; do not mistake it
    # for the guard.
    "When earlier captions from this turn are listed, this caption must "
    "read differently from all of them. Steps often repeat an action; "
    "the caption carries what CHANGED — the file, the query, the page, "
    "the number, the outcome — not the verb they share. Three searches "
    "become 'Searched for the 2020 burst paper, no match', 'Found the "
    "AGILE time profile', 'Pulled the radio flux table' — never the "
    "same line three times.\n"
    'Return ONLY a JSON object: {"caption": "<the one line>"}.'
)

_PICK_PROMPT = (
    "The agent produced several files for one request. Name the ones the "
    "user actually asked to receive -- usually one.\n"
    "Work backwards from the request: a presentation means the .pptx, a "
    "report means the .pdf/.docx/.md, a dataset means the .csv/.xlsx. "
    "Leave out anything made along the way: source files unless code was "
    "what was asked for, images rendered only to be embedded in a "
    "document, scratch and debug output.\n"
    "Reply with one path per line, copied exactly, and nothing else. If "
    "every file looks like part of the answer, list them all."
)


_TURN_PROMPT = (
    "You are reviewing a completed agent turn. Reply with 1-3 short "
    "sentences of plain prose, no markdown, summarizing what the agent "
    "accomplished for the user. Do not list files -- the deliverables "
    "are decided elsewhere and shown separately. Reply with the "
    "sentences only, no preamble and no JSON."
)



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


def _rejects_response_format(exc: BaseException) -> bool:
    """True when the provider refused the request, not the network.

    Only a 4xx that is not a rate limit means *this request* was
    malformed for *this provider* — and ``response_format`` is the only
    thing about it that varies by provider. A dropped connection, a
    timeout or a 5xx is transient, and latching on one of those would
    let a single blip degrade the rest of the session's captions to
    unvalidated prose.
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(exc, "status", None)
    return isinstance(status, int) and 400 <= status < 500 and status != 429


def _extract_caption(content: str) -> str:
    """Pull the caption out of the model's reply.

    Returns raw text rather than ``None`` on a miss: a gateway that
    ignores ``response_format`` answers in prose, and captions
    degrading to unvalidated prose is recoverable where every caption
    on that deployment vanishing is not.

    An object *without* a usable caption field is deliberately not
    treated as prose. The most common such reply is the echoed tool
    result, and its raw body must never become the label — returning it
    unchanged lets the validator reject it on its leading brace.
    """
    for parsed in iter_json_objects(content):
        caption = parsed.get("caption")
        if isinstance(caption, str) and caption.strip():
            return caption
    return content


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


#: Words too common to make a caption distinctive. Dropped before
#: comparing two captions so "Searched for X" and "Searched for Y" are
#: judged on X and Y.
_CAPTION_STOPWORDS = frozenset({
    "the", "a", "an", "for", "to", "in", "of", "and", "on", "with",
    "from", "no", "not", "found", "its", "at", "by", "was", "were",
})

#: Overlap at or above which two captions read as the same row. Tuned
#: against real DEV transcripts: 0.7 catches rephrasings ("Opened the
#: arXiv page" / "Opened arXiv page") without merging genuinely
#: different steps that share a verb.
_CAPTION_DUPLICATE_THRESHOLD = 0.7


def _caption_content_words(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9_./-]+", text.lower())
        if w not in _CAPTION_STOPWORDS and len(w) > 2
    }


def _duplicates_prior_caption(text: str, priors: list[str]) -> bool:
    """True when *text* would render as a repeat of a caption already shown.

    Asking the model for short git-subject captions measurably improves
    truncation and voice, and measurably increases repetition: two
    similar steps collapse to the same eight words. Three prompt-level
    attempts to fix that failed, so it is settled here instead, where the
    other caption failure modes already are.

    Dropping the caption is not the same as showing nothing: the client
    falls back to its deterministic tool-derived label, which for a step
    that resembles the previous one is the more honest row anyway.
    """
    words = _caption_content_words(text)
    if not words:
        return False
    for prior in priors:
        prior_words = _caption_content_words(prior)
        if not prior_words:
            continue
        overlap = len(words & prior_words) / len(words | prior_words)
        if overlap >= _CAPTION_DUPLICATE_THRESHOLD:
            return True
    return False


#: Server-internal workspace directories, mirroring
#: ``surogates.api.routes.workspace._RESERVED_PREFIXES`` and
#: ``_HIDDEN_PREFIXES``. These hold the storage behind a rendered
#: surface, not files the user asked for: ``_artifacts/`` is where
#: ArtifactStore keeps ``v1.json``, ``v2.json`` … for an artifact that is
#: already shown as its own panel, and ``_whiteboard/`` holds the canvas.
#:
#: Listed explicitly rather than filtering every ``_``-prefixed path: a
#: Jekyll site's ``_posts/`` and ``_config.yml`` are real deliverables,
#: and hiding a user's work is a worse failure than showing one extra row.
_INTERNAL_WORKSPACE_PREFIXES: tuple[str, ...] = ("_artifacts", "_whiteboard")


def _is_internal_workspace_path(path: str) -> bool:
    """True for workspace paths that are never user deliverables.

    Any hidden path segment marks agent-internal state (``.agents/``
    skill context files, ``.claude/`` config, ``.cache/`` …),
    ``uploads/`` holds user-provided attachments — inputs, not
    outputs — and the underscore directories above are storage for a
    surface the chat already renders. Filtered deterministically so they
    never reach the summary LLM as candidates nor the user-visible
    download card.
    """
    segments = [s for s in path.split("/") if s]
    if any(s.startswith(".") for s in segments):
        return True
    if not segments:
        return False
    return (
        segments[0] == "uploads"
        or segments[0] in _INTERNAL_WORKSPACE_PREFIXES
    )


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
        recap_enabled: bool = True,
    ) -> None:
        self._base_client = base_client
        self._base_model = base_model
        self._summary_client = summary_client
        self._summary_model = summary_model
        # Iteration one-liners and the end-of-turn recap are separately
        # controlled: the one-liners are written while the turn runs, the
        # recap only after the agent has stopped talking, where it sits
        # between the last word and session.complete.
        self._recap_enabled = recap_enabled
        # Cleared the first time this summarizer's provider rejects
        # ``response_format``; see :meth:`summarize_iteration`.
        self._iteration_json_mode = True

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

        label = f"iteration {iteration_id}"
        json_mode = self._iteration_json_mode
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            content = await self._chat_completion(
                self._summary_client,
                kwargs,
                label=label,
                timeout=_ITERATION_SUMMARY_TIMEOUT_SECONDS,
                reraise=json_mode,
            )
        except Exception as exc:
            if not _rejects_response_format(exc):
                return None
            # ``response_format`` is a request, not a guarantee, and the
            # summary slot is configured separately from the base model
            # that has always used it. A provider that rejects the
            # parameter would otherwise silence every caption it serves
            # — worse than an unvalidated one — so drop the constraint
            # and retry once. Plain prose still goes through the
            # validator.
            #
            # The latch is per-summarizer, which is per-session: the
            # summary endpoint is resolved per tenant
            # (build_summary_auxiliary_llm reads org overrides), so a
            # process-wide latch would let one tenant's gateway disable
            # JSON mode for every other tenant. If this is ever seen
            # firing per-session in volume, the endpoint-keyed home for
            # it is AuxiliaryLLM, which is shared by every auxiliary
            # caller of the same provider.
            logger.warning(
                "summary provider rejected response_format; "
                "falling back to plain-text captions",
            )
            self._iteration_json_mode = False
            kwargs.pop("response_format", None)
            content = await self._chat_completion(
                self._summary_client,
                kwargs,
                label=label,
                timeout=_ITERATION_SUMMARY_TIMEOUT_SECONDS,
            )
        if content is None:
            return None
        caption = _extract_caption(content)
        text = _normalize_caption(caption.strip().strip('"').rstrip("."))
        if not _valid_iteration_summary(text):
            logger.warning(
                "discarding malformed iteration summary for %s: %r",
                iteration_id,
                text[:200],
            )
            return None
        if _duplicates_prior_caption(text, prior_iteration_summaries):
            logger.debug(
                "discarding duplicate iteration summary for %s: %r",
                iteration_id,
                text[:200],
            )
            return None
        return text

    async def pick_deliverables(
        self,
        *,
        turn_id: str,
        user_message: str,
        artifacts: list[TurnArtifact],
    ) -> list[TurnArtifact]:
        """Narrow several surviving files to the ones actually asked for.

        The deterministic filters answer "is this a real file this turn
        produced". They cannot answer "is this what the user wanted" --
        that is a question about the request, not the workspace, and it
        is the one rule from the retired curation prompt that does not
        reduce to bookkeeping.

        So it is asked, but only when it is actually a question: with one
        surviving file there is nothing to choose between, and the common
        turn skips the call entirely.

        Fails open. A timeout, a refusal, or an unrecognisable reply
        returns everything -- showing an extra file costs a cluttered
        card, dropping a real one costs the user their work.
        """
        files = [a for a in artifacts if a.kind == "file"]
        if len(files) < 2:
            return artifacts

        client = self._summary_client or self._base_client
        model = self._summary_model or self._base_model
        listing = "\n".join(a.ref for a in files)

        content = await self._chat_completion(
            client,
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": _PICK_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Request: {user_message[:1000]}\n\n"
                            f"Files:\n{listing}"
                        ),
                    },
                ],
                "max_tokens": 200,
                "temperature": 0,
                "stream": False,
            },
            label=f"pick {turn_id}",
            timeout=_TURN_SUMMARY_TIMEOUT_SECONDS,
        )
        if not content:
            return artifacts

        # Intersect with what we offered rather than trusting the reply:
        # a model that invents a path cannot add one to the card, and a
        # reply that matches nothing falls through to "keep everything".
        offered = {a.ref: a for a in files}
        chosen = [
            offered[line]
            for line in (ln.strip().strip("-* `") for ln in content.splitlines())
            if line in offered
        ]
        if not chosen:
            logger.debug(
                "deliverable pick for %s matched no candidate: %r",
                turn_id, content[:200],
            )
            return artifacts

        keep = {a.ref for a in chosen}
        return [a for a in artifacts if a.kind != "file" or a.ref in keep]

    async def summarize_turn(
        self,
        *,
        turn_id: str,
        user_message: str,
        iteration_summaries: list[str],
        artifacts: list[TurnArtifact],
    ) -> TurnSummary | None:
        """Write the turn's recap. ``artifacts`` are already decided.

        Curation used to happen here: every touched file went into the
        prompt and the base model picked the user's deliverable. That is
        now reconciled against the workspace (see
        :mod:`surogates.harness.delivery_manifest`), so this call only
        writes prose -- which is what the cheap summary model is for, and
        it is the reason the call no longer runs on the base model.

        Returns ``None`` when the turn has nothing worth summarizing or
        the model returns nothing usable.
        """
        if not self._recap_enabled:
            return None
        if not iteration_summaries and not artifacts:
            return None

        user_block_parts: list[str] = [f"User asked: {user_message[:1000]}"]
        if iteration_summaries:
            user_block_parts.append(
                "Iteration summaries:\n"
                + "\n".join(f"- {s}" for s in iteration_summaries)
            )
        if artifacts:
            user_block_parts.append(
                "Delivered:\n"
                + "\n".join(f"- {a.label}" for a in artifacts)
            )
        user_block = "\n\n".join(user_block_parts)

        # Prose only, so the cheap auxiliary is enough. Falling back to
        # the base model keeps recaps working where no summary model is
        # configured, rather than dropping them.
        client = self._summary_client or self._base_client
        model = self._summary_model or self._base_model

        content = await self._chat_completion(
            client,
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": _TURN_PROMPT},
                    {"role": "user", "content": user_block},
                ],
                "max_tokens": _MAX_TURN_SUMMARY_TOKENS,
                "temperature": 0.3,
                "stream": False,
            },
            label=f"turn {turn_id}",
            timeout=_TURN_SUMMARY_TIMEOUT_SECONDS,
        )
        recap = (content or "").strip()
        if not recap and not artifacts:
            return None
        return TurnSummary(recap=recap, artifacts=list(artifacts))

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
        reraise: bool = False,
    ) -> str | None:
        """Run a single chat completion under the given timeout.

        Returns the message content on success, ``None`` on any failure
        (timeout, network error, malformed response shape).

        ``reraise`` propagates provider errors instead of swallowing
        them, so a caller can tell a rejected request parameter from a
        slow provider — a timeout never reraises, since retrying it
        without the parameter would blame the wrong thing.
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
            if reraise:
                raise
            return None

        try:
            message = response.choices[0].message
        except (AttributeError, IndexError, TypeError):
            logger.warning("summary response had unexpected shape for %s", label)
            return None

        return next(iter(message_texts(message)), None)

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
