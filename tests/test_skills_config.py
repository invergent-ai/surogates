"""Regression tests for configured platform skill directories."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from surogates.api.routes.skills import list_skills
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.skills import _skills_list_handler


def _make_tenant(asset_root: Path) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=str(asset_root),
    )


def _write_platform_skill(platform_dir: Path) -> None:
    skill_dir = platform_dir / "configured-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: configured-skill\n"
        "description: Loaded from configured platform dir\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )


class _EmptyResult:
    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list[object]:
        return []


class _EmptyDbSession:
    async def execute(self, _stmt: object) -> _EmptyResult:
        return _EmptyResult()


class _SessionFactory:
    def __call__(self) -> "_SessionFactory":
        return self

    async def __aenter__(self) -> _EmptyDbSession:
        return _EmptyDbSession()

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_api_list_skills_uses_configured_platform_skills_dir(tmp_path: Path):
    platform_dir = tmp_path / "platform-skills"
    _write_platform_skill(platform_dir)

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(platform_skills_dir=str(platform_dir)),
                session_factory=_SessionFactory(),
            ),
        ),
    )

    response = await list_skills(
        request=request,
        tenant=_make_tenant(tmp_path / "assets"),
    )

    assert response.total == 1
    assert response.skills[0].name == "configured-skill"


@pytest.mark.asyncio
async def test_builtin_skills_list_uses_configured_platform_skills_dir(
    tmp_path: Path,
):
    platform_dir = tmp_path / "platform-skills"
    _write_platform_skill(platform_dir)

    raw = await _skills_list_handler(
        {},
        tenant=_make_tenant(tmp_path / "assets"),
        settings=SimpleNamespace(platform_skills_dir=str(platform_dir)),
    )

    result = json.loads(raw)
    assert result["count"] == 1
    assert result["skills"][0]["name"] == "configured-skill"
