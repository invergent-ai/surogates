"""The worker overlays runtime-config repos onto a wake-local session config.

The overlay lets ``/code`` and tool dispatch read the agent's configured
repositories from ``session.config['repos']`` without ever writing them back
to the persisted session row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from surogates.orchestrator.worker import _overlay_session_repos
from surogates.session.models import Session

_REPOS = ({"url": "https://github.com/acme/api", "default_branch": "main"},)


def _session(config=None) -> Session:
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    return Session(
        id=uuid4(), org_id=uuid4(), agent_id="a1", channel="slack",
        status="active", config=config or {}, created_at=now, updated_at=now,
    )


def test_overlay_sets_repos_on_a_copy():
    original = _session({"multi_party": True})
    out = _overlay_session_repos(original, _REPOS)
    assert out is not original
    assert out.config["repos"] == [
        {"url": "https://github.com/acme/api", "default_branch": "main"},
    ]
    # Existing config keys are preserved.
    assert out.config["multi_party"] is True
    # The persisted row's config is untouched (no write-back).
    assert "repos" not in original.config


def test_overlay_deep_copies_repo_dicts():
    repos = [{"url": "https://github.com/acme/api", "default_branch": "main"}]
    out = _overlay_session_repos(_session(), tuple(repos))
    repos[0]["url"] = "mutated"
    assert out.config["repos"][0]["url"] == "https://github.com/acme/api"


def test_overlay_noop_when_no_repos():
    original = _session({"multi_party": True})
    out = _overlay_session_repos(original, ())
    # No repos → session returned unchanged (no needless copy).
    assert out is original
