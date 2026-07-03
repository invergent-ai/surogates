"""Throttled, channel-deliverable progress for a coding run.

Code-run PROGRESS events render live in the web UI (CodeRunBlock) but are not
delivered to channels, so a Slack/Telegram user sees nothing for the minutes a
run takes.  These helpers format a periodic heartbeat (delivered via a
``CODE_RUN_CHANNEL_UPDATE`` event) so the channel knows the run is alive.

The heartbeat's activity line is written by the agent's summary model
(:func:`summarize_progress_activity`) — a short present-tense sentence about
what the run is doing right now — falling back to the most recent transcript
line (:func:`_activity_line`) when no summary model is configured or the call
fails.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from surogates.coding_agents.repo_resolve import _repo_slug

logger = logging.getLogger(__name__)

# Emit at most one channel update this often (seconds) during a run.
CHANNEL_UPDATE_INTERVAL = 45.0

# Only the recent transcript matters for "what is it doing now"; cap what we
# hand the summariser so a long run doesn't grow the request unboundedly.
_TRANSCRIPT_TAIL = 4000

_SUMMARY_SYSTEM = (
    "You report the progress of an autonomous coding agent to a chat user. "
    "Given a transcript of what the agent has done so far, reply with ONE short "
    "present-tense clause (max 15 words) naming what it is doing right now — "
    "for example 'Adding fuzzy matching to the search tool and updating its "
    "tests'. No preamble, no markdown, no quotes, no trailing period."
)


def _repo_where(repo: Mapping[str, str] | None) -> str:
    slug = _repo_slug(repo.get("url", "")) if repo else None
    return f" on `{slug}`" if slug else ""


def _activity_line(text: str | None, limit: int = 140) -> str:
    """The most recent substantive line of the progress transcript, truncated.

    Prefers the agent's prose (its own narration); falls back to the last
    ``›`` tool marker with the marker stripped when there is no prose yet.
    Used as the heuristic when the summary model is unavailable — taking the
    *last* line (not the first) so the hint reflects current activity.
    """
    prose = ""
    tool = ""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("›"):
            tool = s.lstrip("›").strip()
        else:
            prose = s
    return (prose or tool)[:limit]


async def summarize_progress_activity(
    client: Any | None,
    model: str | None,
    transcript: str | None,
    *,
    timeout: float = 12.0,
) -> str:
    """One-line summary of what the run is doing now, via the summary model.

    Returns ``""`` when no summary client/model is configured, the transcript
    is empty, or the call fails for any reason — the caller falls back to
    :func:`_activity_line` so a heartbeat is always producible.
    """
    tail = (transcript or "").strip()[-_TRANSCRIPT_TAIL:]
    if client is None or not model or not tail:
        return ""
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SUMMARY_SYSTEM},
                    {"role": "user", "content": tail},
                ],
                temperature=0.0,
                max_tokens=60,
            ),
            timeout=timeout,
        )
    except Exception as exc:  # timeout, network, malformed response — never fatal
        logger.warning("code-run progress summary failed: %r", exc)
        return ""
    try:
        content = resp.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        return ""
    return (content or "").strip()[:200]


def render_code_run_ack(agent: str, repo: Mapping[str, str] | None) -> str:
    """The initial 'started' message posted to the channel when a run begins."""
    return (
        f"🛠️ On it — running {agent}{_repo_where(repo)}. "
        "I'll post the result when it's done."
    )


def render_code_run_update(repo: Mapping[str, str] | None, activity: str | None) -> str:
    """A periodic 'still working' heartbeat with a short activity hint.

    ``activity`` is a summary-model sentence when available, otherwise a raw
    transcript from which the most recent line is extracted.
    """
    line = _activity_line(activity)
    tail = f" — {line}" if line else ""
    return f"🛠️ Still working{_repo_where(repo)}…{tail}"


def render_code_run_done(agent: str, repo: Mapping[str, str] | None, *, ok: bool) -> str:
    """The terminal state for the edited-in-place main coding message.

    Replaces the trailing 'still working' heartbeat once the run ends so the
    message doesn't linger as if the run were still going — the detailed result
    (PR link or error) arrives as its own message.
    """
    if ok:
        return f"✅ {agent} finished{_repo_where(repo)}."
    return f"⚠️ {agent} stopped{_repo_where(repo)}."
