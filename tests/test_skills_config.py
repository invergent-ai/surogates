"""The skills API lists skills attached through the agent bundle."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from surogates.api.routes.skills import list_skills
from surogates.tenant.context import TenantContext

TEST_AGENT_ID = "agent-under-test"


def _make_tenant() -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/no-such-asset-root",
    )


def _skill_md(name: str, description: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nBody\n"


class _FakeBundle:
    """Minimal in-memory stand-in for :class:`AgentFileBundle`."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = dict(files)

    async def list(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self._files if p.startswith(prefix))

    async def read_text(self, path: str) -> str:
        if path not in self._files:
            raise LookupError(path)
        return self._files[path]


class _FakeBundleCache:
    """Stand-in for ``app.state.file_bundle_cache`` keyed by agent_id."""

    def __init__(self, bundle: _FakeBundle) -> None:
        self._bundle = bundle

    async def get(self, agent_id: str) -> _FakeBundle:
        return self._bundle


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
async def test_api_list_skills_surfaces_bundle_attached_skill():
    """The ``/skills`` route lists per-agent bundle skills (Layer 1)."""
    bundle = _FakeBundle(
        {
            "skills/configured-skill/SKILL.md": _skill_md(
                "configured-skill", "Loaded from the per-agent bundle",
            ),
        }
    )

    request = SimpleNamespace(
        query_params={"agent_id": TEST_AGENT_ID},
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(),
                session_factory=_SessionFactory(),
                file_bundle_cache=_FakeBundleCache(bundle),
                system_bundle_cache=None,
                slug_resolver_cache=None,
            ),
        ),
    )

    response = await list_skills(
        request=request,
        tenant=_make_tenant(),
    )

    # The built-in advisor expert is always loaded, so assert on the
    # skill under test rather than an exact total.
    names = [s.name for s in response.skills]
    assert "configured-skill" in names
