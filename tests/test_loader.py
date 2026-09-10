"""Skill loading combines resource sources with the correct user scope and precedence."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from surogates.tenant.context import TenantContext
from surogates.tools.loader import ResourceLoader


class _FakeBundle:
    """Minimal in-memory stand-in for :class:`AgentFileBundle`.

    Exposes the ``list(prefix)`` / ``read_text(path)`` surface the loader
    uses for bundle-backed skill layers.
    """

    def __init__(self, files: dict[str, str]) -> None:
        self._files = dict(files)

    async def list(self, prefix: str = "") -> list[str]:
        return sorted(p for p in self._files if p.startswith(prefix))

    async def read_text(self, path: str) -> str:
        if path not in self._files:
            raise LookupError(path)
        return self._files[path]


def _make_tenant(asset_root: str) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root=asset_root,
    )


class TestMergePrecedence:
    """User bucket files override the (lower-precedence) system bundle.

    The platform-directory layers were retired: Layer 1 is now the system
    bundle and the user-file layer (Layer 2) is merged on top of it, so a
    user-file skill shadows a system-bundle skill of the same name.
    """

    @pytest.mark.asyncio
    async def test_user_files_override_system_bundle(self, tmp_path: Path):
        org_id = "00000000-0000-0000-0000-000000000001"
        user_id = "00000000-0000-0000-0000-000000000002"

        # System bundle skill.
        system_bundle = _FakeBundle(
            {
                "shared/SKILL.md": (
                    "---\nname: shared\ndescription: platform version\n---\n"
                    "Platform body\n"
                ),
            }
        )

        # User skill (same name) on disk.
        user_skills_dir = tmp_path / "assets" / org_id / "users" / user_id / "skills"
        user_skills_dir.mkdir(parents=True)
        (user_skills_dir / "shared_skill.md").write_text(
            "---\nname: shared\ndescription: user version\n---\nUser body\n",
            encoding="utf-8",
        )

        loader = ResourceLoader()

        tenant = _make_tenant(str(tmp_path / "assets"))
        skills = await loader.load_skills(tenant, system_bundle=system_bundle)

        # Only one skill named "shared", and it should be the user version.
        shared_skills = [s for s in skills if s.name == "shared"]
        assert len(shared_skills) == 1
        assert shared_skills[0].description == "user version"
        assert shared_skills[0].source == "user"


class TestLoadSkillsWithUserIdNone:
    """SA-token callers reach the loader with ``user_id=None``.

    The system-bundle layer must still resolve; user-files and user-DB
    layers must be skipped cleanly without constructing a path containing
    the literal string ``"None"``.
    """

    @pytest.mark.asyncio
    async def test_user_id_none_returns_platform_only(self, tmp_path: Path):
        system_bundle = _FakeBundle(
            {
                "demo/SKILL.md": (
                    "---\nname: demo\ndescription: Demo skill\n---\nBody\n"
                ),
            }
        )

        # Booby-trap: a regression that re-introduces ``str(None)`` as a
        # path component would pick this file up.
        asset_root = tmp_path / "assets"
        booby_trap = (
            asset_root
            / "00000000-0000-0000-0000-000000000001"
            / "users"
            / "None"
            / "skills"
        )
        booby_trap.mkdir(parents=True)
        (booby_trap / "evil.md").write_text(
            "---\nname: evil\ndescription: Should not appear\n---\nBody\n",
            encoding="utf-8",
        )

        sa_tenant = TenantContext(
            org_id=UUID("00000000-0000-0000-0000-000000000001"),
            user_id=None,
            org_config={},
            user_preferences={},
            permissions=frozenset(),
            asset_root=str(asset_root),
        )

        loader = ResourceLoader()
        skills = await loader.load_skills(
            sa_tenant, db_session=None, system_bundle=system_bundle,
        )

        names = {s.name for s in skills}
        assert "demo" in names
        assert "evil" not in names, (
            "user-files layer must be skipped when tenant.user_id is None; "
            "the loader is constructing a path containing 'None'"
        )
