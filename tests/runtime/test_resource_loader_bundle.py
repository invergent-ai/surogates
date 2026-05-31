"""Tests for ResourceLoader bundle overlay.

load_skills(tenant, db_session=None,
bundle=None) — when bundle is provided, layer 1 (platform skills)
reads come from the bundle's skills/ prefix instead of
/etc/surogates/skills/.

Same overlay for load_agents.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeBundle:
    def __init__(self, files):
        self._files = files

    async def list(self, prefix=""):
        return sorted(p for p in self._files if p.startswith(prefix))

    async def read_text(self, path, encoding="utf-8"):
        return self._files[path].decode(encoding)


def _make_tenant(*, asset_root="/nonexistent"):
    """A minimal tenant stub matching what ResourceLoader reads
    (org_id, user_id, asset_root)."""
    return SimpleNamespace(
        org_id=uuid4(), user_id=None, asset_root=asset_root,
    )


@pytest.mark.asyncio
async def test_load_skills_overlay_reads_from_bundle_when_present(tmp_path):
    from surogates.tools.loader import ResourceLoader

    bundle = _FakeBundle({
        "skills/foo/SKILL.md": (
            b"---\nname: foo\ndescription: Foo skill\n---\n# foo skill\n"
            b"platform built-in"
        ),
    })
    tenant = _make_tenant(asset_root=str(tmp_path))
    loader = ResourceLoader(platform_skills_dir=str(tmp_path / "noexist"))
    skills = await loader.load_skills(
        tenant, db_session=None, bundle=bundle,
    )
    assert any(s.name == "foo" for s in skills)


@pytest.mark.asyncio
async def test_load_skills_bundle_none_falls_back_to_filesystem(tmp_path):
    """Legacy / helm-mode path: when bundle is None the loader
    reads layer 1 from platform_dir on disk exactly as before."""
    from surogates.tools.loader import ResourceLoader

    tenant = _make_tenant(asset_root=str(tmp_path))
    loader = ResourceLoader(platform_skills_dir=str(tmp_path / "noexist"))
    skills = await loader.load_skills(
        tenant, db_session=None, bundle=None,
    )
    # Loader didn't crash; returned skills come from the filesystem
    # (here empty because the dir doesn't exist).
    assert isinstance(skills, list)


@pytest.mark.asyncio
async def test_load_agents_overlay_reads_from_bundle_when_present(tmp_path):
    from surogates.tools.loader import ResourceLoader

    bundle = _FakeBundle({
        "agents/sub-bot/AGENT.md": (
            b"---\nname: sub-bot\ndescription: Sub-bot\n---\n# sub-bot"
        ),
    })
    tenant = _make_tenant(asset_root=str(tmp_path))
    loader = ResourceLoader(platform_agents_dir=str(tmp_path / "noexist"))
    agents = await loader.load_agents(
        tenant, db_session=None, bundle=bundle,
    )
    assert any(a.name == "sub-bot" for a in agents)
