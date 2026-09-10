"""Slash-skill expansion through tool dispatch and content overrides."""

from __future__ import annotations

import json
from typing import Any

import pytest

from surogates.harness.slash_skill import (
    expand_slash_skill,
)


class _FakeRegistry:
    """Captures dispatch calls and returns a canned ``skill_view`` payload."""

    def __init__(self, payload: dict[str, Any] | None) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def dispatch(
        self, name: str, arguments: Any, **kwargs: Any,
    ) -> str:
        self.calls.append((name, arguments, kwargs))
        if self._payload is None:
            raise RuntimeError("skill_view unavailable")
        return json.dumps(self._payload)


class _FakeRegistryReturningInvalidJSON:
    async def dispatch(self, name: str, arguments: Any, **kwargs: Any) -> str:
        return "this is not json {{{"


@pytest.mark.asyncio
class TestExpandSlashSkill:
    async def test_expands_known_skill(self) -> None:
        registry = _FakeRegistry({
            "success": True,
            "name": "arxiv",
            "content": "# arxiv\n\nSearch papers.",
            "staged_at": "/workspace/.skills/arxiv",
        })
        result = await expand_slash_skill(
            text="/arxiv cuda training",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )

        assert result is not None
        expanded, skill_name, staged_at, kind = result
        assert kind == "skill"
        assert skill_name == "arxiv"
        assert staged_at == "/workspace/.skills/arxiv"
        assert "# arxiv" in expanded
        assert "cuda training" in expanded

        # Verify dispatch was called with the right tool + args.
        assert len(registry.calls) == 1
        call_name, call_args, call_kwargs = registry.calls[0]
        assert call_name == "skill_view"
        assert call_args == {"name": "arxiv"}
        assert call_kwargs["session_id"] == "sess-1"


    async def test_returns_none_for_unknown_skill(self) -> None:
        registry = _FakeRegistry({
            "success": False,
            "error": "Skill 'nope' not found.",
        })
        result = await expand_slash_skill(
            text="/nope do thing",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None

    async def test_returns_none_when_dispatch_raises(self) -> None:
        registry = _FakeRegistry(payload=None)  # raises in dispatch
        result = await expand_slash_skill(
            text="/arxiv x",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None

    async def test_returns_none_when_dispatch_returns_non_json(self) -> None:
        result = await expand_slash_skill(
            text="/arxiv x",
            tools=_FakeRegistryReturningInvalidJSON(),
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None

    async def test_returns_none_when_skill_body_empty(self) -> None:
        registry = _FakeRegistry({
            "success": True,
            "name": "arxiv",
            "content": "",
        })
        result = await expand_slash_skill(
            text="/arxiv x",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=None,
        )
        assert result is None

    async def test_expands_db_backed_skill_without_staging(self) -> None:
        """End-to-end shape check for the DB-backed local-fallback response.

        ``_skill_view_handler`` returns no ``staged_at`` for DB skills (they
        carry no linked files).  ``expand_slash_skill`` must still produce a
        valid expansion -- the harness emits an audit event with
        ``staged_at=None`` rather than skipping the expansion.
        """
        registry = _FakeRegistry({
            "success": True,
            "name": "wiki",
            "description": "Wiki tool",
            "tags": ["docs"],
            "related_skills": [],
            "content": "# Wiki\nbody",
            "linked_files": None,
            "usage_hint": None,
            "token_estimate": 4,
        })
        result = await expand_slash_skill(
            text="/wiki cancel subscription",
            tools=registry,
            tenant=object(),
            session_id="sess-1",
            api_client=None,
            session_factory=object(),
        )

        assert result is not None
        expanded, skill_name, staged_at, kind = result
        assert kind == "skill"
        assert skill_name == "wiki"
        assert staged_at is None
        assert "# Wiki" in expanded
        assert "cancel subscription" in expanded


class _ApiBackedRegistry:
    """Tool registry whose ``skill_view`` delegates to the API client.

    Mirrors the production wiring where, in shared-runtime mode, the
    ``skill_view`` tool handler forwards to ``HarnessAPIClient.view_skill``.
    """

    async def dispatch(self, name: str, arguments: Any, **kwargs: Any) -> str:
        assert name == "skill_view"
        api_client = kwargs["api_client"]
        return await api_client.view_skill(arguments["name"])


class _OverrideApiClient:
    """Fake API client returning candidate (override) content for one skill."""

    async def view_skill(self, name: str, file_path: str | None = None) -> str:
        return json.dumps({
            "success": True,
            "name": name,
            "content": "CANDIDATE BODY",
        })

    async def list_skills(self, category: str | None = None) -> str:
        return json.dumps({
            "success": True,
            "skills": [
                {"name": "browser-research", "description": "Research the web",
                 "type": "skill", "category": None, "trigger": None},
            ],
            "count": 1,
        })


@pytest.mark.asyncio
class TestExpandSlashSkillOverride:
    async def test_slash_expansion_uses_override_content(self) -> None:
        """End-to-end: the API path serves override content into the expansion.

        Task 5 made ``view_skill`` return the session's override body; this
        proves the slash ``/<skill>`` path surfaces that candidate content so
        a future refactor cannot silently regress it.
        """
        result = await expand_slash_skill(
            text="/browser-research compare vendors",
            tools=_ApiBackedRegistry(),
            tenant=object(),
            session_id="sess-1",
            api_client=_OverrideApiClient(),
            session_factory=None,
        )
        assert result is not None
        expanded_text, name, staged_at, kind = result
        assert name == "browser-research"
        assert kind == "skill"
        assert "CANDIDATE BODY" in expanded_text
