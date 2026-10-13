"""Changing a platform skill says it is read-only, not that it does not exist.

A PROD docx session tried to patch the built-in ``docx`` skill with a lesson it had
learned and was told "Skill 'docx' not found" -- about a skill it had just loaded.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from surogates.api.routes import skills as routes
from surogates.tools.loader import SKILL_SOURCE_PLATFORM, SKILL_SOURCE_USER, SkillDef


class _Storage:
    def __init__(self, existing: dict | None = None) -> None:
        self._existing = existing

    async def skill_exists(self, name: str) -> dict | None:
        return self._existing


def _request(monkeypatch: pytest.MonkeyPatch, catalog: list[SkillDef]) -> Any:
    class _Loader:
        async def load_skills(self, tenant, **_: Any) -> list[SkillDef]:
            return catalog

    async def _none(_request: Any) -> None:
        return None

    monkeypatch.setattr(routes, "_resource_loader", lambda _r: _Loader())
    monkeypatch.setattr(routes, "_resolve_agent_bundle", _none)
    monkeypatch.setattr(routes, "_resolve_system_bundle", _none)

    @asynccontextmanager
    async def _session_factory():
        yield None

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=_session_factory)))


def _skill(name: str, source: str) -> SkillDef:
    return SkillDef(name=name, description="", content="", source=source)


@pytest.mark.asyncio
async def test_a_platform_skill_is_read_only(monkeypatch):
    request = _request(monkeypatch, [_skill("docx", SKILL_SOURCE_PLATFORM)])
    with pytest.raises(HTTPException) as caught:
        await routes._writable_skill(_Storage(), "docx", request, tenant=None)
    assert caught.value.status_code == 403
    assert "read-only" in caught.value.detail


@pytest.mark.asyncio
async def test_an_unknown_skill_is_not_found(monkeypatch):
    request = _request(monkeypatch, [_skill("notes", SKILL_SOURCE_USER)])
    with pytest.raises(HTTPException) as caught:
        await routes._writable_skill(_Storage(), "docx", request, tenant=None)
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_tenant_skill_is_returned(monkeypatch):
    request = _request(monkeypatch, [])
    found = await routes._writable_skill(_Storage({"key_prefix": "skills/notes"}), "notes", request, tenant=None)
    assert found == {"key_prefix": "skills/notes"}
