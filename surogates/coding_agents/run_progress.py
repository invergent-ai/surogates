"""Throttled, channel-deliverable progress for a coding run.

Code-run PROGRESS events render live in the web UI (CodeRunBlock) but are not
delivered to channels, so a Slack/Telegram user sees nothing for the minutes a
run takes.  These helpers format a periodic heartbeat (delivered via a
``CODE_RUN_CHANNEL_UPDATE`` event) so the channel knows the run is alive.
"""
from __future__ import annotations

from collections.abc import Mapping

from surogates.coding_agents.repo_resolve import _repo_slug

# Emit at most one channel update this often (seconds) during a run.
CHANNEL_UPDATE_INTERVAL = 45.0


def _repo_where(repo: Mapping[str, str] | None) -> str:
    slug = _repo_slug(repo.get("url", "")) if repo else None
    return f" on `{slug}`" if slug else ""


def _activity_line(text: str | None, limit: int = 140) -> str:
    """The first substantive line of a progress chunk (skipping ``›`` tool
    markers), truncated — a short hint at what the run is doing right now."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("›"):
            return s[:limit]
    return ""


def render_code_run_ack(agent: str, repo: Mapping[str, str] | None) -> str:
    """The initial 'started' message posted to the channel when a run begins."""
    return (
        f"🛠️ On it — running {agent}{_repo_where(repo)}. "
        "I'll post the result when it's done."
    )


def render_code_run_update(repo: Mapping[str, str] | None, activity: str | None) -> str:
    """A periodic 'still working' heartbeat with a short activity hint."""
    line = _activity_line(activity)
    tail = f" — {line}" if line else ""
    return f"🛠️ Still working{_repo_where(repo)}…{tail}"
