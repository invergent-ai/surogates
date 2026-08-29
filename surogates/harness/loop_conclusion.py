"""Last-resort conclusion for a turn that ended without answering.

The loop's guards catch a turn that produces nothing (empty or punctuation-only
content) or that trails off mid-intent, and give the model another chance. When
that chance is used up the turn still has to end, and today it ends with
whatever the model last said -- an empty string, an ellipsis, or "Let me check
the page directly via the browser....".

That is a bad ending for a benchmark and a worse one for a person: the work was
done, the finding is somewhere in the transcript, and the reply concludes
nothing. This module asks the cheap summary model for the conclusion the
transcript already supports.

**It extracts; it never invents.** A model told to produce an answer will
produce one whether or not the evidence is there -- that is how a missing audio
transcription became a confidently fabricated shopping list. So the prompt
demands a verbatim-grounded answer and reserves an explicit NOT_FOUND, and a
NOT_FOUND is returned to the caller as ``None``: no conclusion is strictly
better than an invented one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The model must be able to decline. Without a reserved token for "the
#: transcript does not contain this", the only shape available to it is an
#: answer, and it will supply one.
_NOT_FOUND = "NOT_FOUND"

_CONCLUSION_PROMPT = (
    "You are reading the transcript of an agent that worked on a task and "
    "then stopped without stating its conclusion.\n\n"
    "Report the conclusion the transcript ALREADY supports. Every fact you "
    "state must be visible in the transcript.\n\n"
    "Do not solve the task yourself. Do not reason past what was found. Do "
    "not fill a gap with what is likely or plausible.\n\n"
    f"If the transcript does not contain the answer, reply with exactly "
    f"{_NOT_FOUND} and nothing else. Replying {_NOT_FOUND} is the correct "
    "answer whenever the work did not get there -- a wrong answer is far "
    "worse than none.\n\n"
    "Otherwise reply with the conclusion alone, in the form the task asked "
    "for, with no preamble."
)

#: The turn is already over and the user is waiting on it, so this is a tail
#: cost on an already-failed turn.  Short enough that a stalled summary model
#: cannot make the ending worse than the one it is replacing.
CONCLUSION_TIMEOUT_SECONDS: float = 20.0

_MAX_TRANSCRIPT_CHARS = 24_000
_MAX_CONCLUSION_TOKENS = 400


def _render_transcript(messages: list[dict[str, Any]]) -> str:
    """Flatten the turn into text, keeping the most recent content.

    Truncates from the front: a conclusion is supported by what the agent
    found most recently, and the opening of a long session is setup.
    """
    parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict)
            )
        if not content:
            # An assistant turn that only made tool calls still carries the
            # intent behind them, which is context for the conclusion.
            calls = m.get("tool_calls") or []
            names = [
                (c.get("function") or {}).get("name")
                for c in calls if isinstance(c, dict)
            ]
            if names:
                parts.append(f"[{role}] called: {', '.join(n for n in names if n)}")
            continue
        parts.append(f"[{role}] {content}")
    text = "\n".join(parts)
    if len(text) > _MAX_TRANSCRIPT_CHARS:
        text = "...\n" + text[-_MAX_TRANSCRIPT_CHARS:]
    return text


async def conclude_from_transcript(
    client: Any,
    model: str,
    *,
    question: str,
    messages: list[dict[str, Any]],
) -> str | None:
    """Return the conclusion the transcript supports, or ``None``.

    ``None`` covers every uncertain case -- no summary model configured, the
    call failed or timed out, the model declined with NOT_FOUND, or it
    returned nothing usable. The caller keeps its existing ending in all of
    them, so this can only add a conclusion, never replace a good one with a
    worse one.
    """
    if client is None or not model:
        return None
    transcript = _render_transcript(messages)
    if not transcript.strip():
        return None

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _CONCLUSION_PROMPT},
                    {
                        "role": "user",
                        "content": f"Task:\n{question}\n\nTranscript:\n{transcript}",
                    },
                ],
                max_tokens=_MAX_CONCLUSION_TOKENS,
                temperature=0.0,
                stream=False,
            ),
            timeout=CONCLUSION_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.debug("Conclusion fallback failed", exc_info=True)
        return None

    try:
        content = (resp.choices[0].message.content or "").strip()
    except (AttributeError, IndexError):
        return None

    # Accept the decline in whatever wrapping the model gives it -- some
    # models answer "NOT_FOUND." or quote it -- but only when it is the whole
    # reply, so a conclusion that happens to mention the token still counts.
    if not content or content.strip(" .\"'`").upper() == _NOT_FOUND:
        return None
    return content
