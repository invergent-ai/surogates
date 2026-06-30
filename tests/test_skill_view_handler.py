"""Tests for ``_skill_view_handler`` -- the local-fallback branch used when
the harness has no ``api_client`` (e.g. anonymous website sessions).

The handler must:

* Return the DB body for skills loaded from the ``skills`` table even though
  no on-disk ``SKILL.md`` exists.  This is what unblocks ``/<db-skill>``
  slash invocations on the website channel.
* Refuse ``file_path`` requests for DB-backed skills (no linked files).
* Keep the existing disk-backed code path working.
* Honour DB-over-filesystem precedence so the local fallback matches the
  API-mediated path when both layers carry the same skill name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from surogates.tenant.context import TenantContext
from surogates.tools.builtin import skills as skills_mod
from surogates.tools.builtin.skills import _skill_view_handler
from surogates.tools.loader import (
    SKILL_SOURCE_ORG_DB,
    SKILL_SOURCE_PLATFORM,
    SkillDef,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tenant(tmp_path: Path) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset({"read"}),
        asset_root=str(tmp_path),
    )


def _stub_load_all_skills(
    monkeypatch: pytest.MonkeyPatch, skills_list: list[SkillDef]
) -> None:
    async def _fake_load(**_: Any) -> list[SkillDef]:
        return skills_list

    monkeypatch.setattr(skills_mod, "_load_all_skills", _fake_load)


def _raise_if_resolve_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentinel: the DB branch must NOT call ``_resolve_skill_dir``."""

    def _explode(*_a: Any, **_kw: Any) -> Path | None:
        raise AssertionError(
            "_resolve_skill_dir must not be called for DB-backed skills"
        )

    monkeypatch.setattr(skills_mod, "_resolve_skill_dir", _explode)


def _make_db_skill(
    *,
    name: str = "wiki",
    description: str = "Wiki tool",
    content: str = "# Wiki\nbody",
    tags: list[str] | None = None,
) -> SkillDef:
    return SkillDef(
        name=name,
        description=description,
        content=content,
        source=SKILL_SOURCE_ORG_DB,
        tags=tags if tags is not None else ["docs"],
    )


# ---------------------------------------------------------------------------
# DB-backed branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDbBackedSkill:
    async def test_returns_inlined_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DB skill resolves without touching the disk lookup."""
        _stub_load_all_skills(monkeypatch, [_make_db_skill()])
        _raise_if_resolve_called(monkeypatch)

        payload = json.loads(
            await _skill_view_handler(
                {"name": "wiki"},
                tenant=_make_tenant(tmp_path),
                session_factory=object(),  # required path; presence only
            )
        )

        assert payload["success"] is True
        assert payload["name"] == "wiki"
        assert payload["description"] == "Wiki tool"
        assert payload["tags"] == ["docs"]
        assert payload["content"] == "# Wiki\nbody"
        assert payload["linked_files"] is None
        assert payload["related_skills"] == []
        # No staging happened.
        assert "staged_at" not in payload

    async def test_file_path_request_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DB skills carry no linked files; ``file_path`` must fail clearly."""
        _stub_load_all_skills(monkeypatch, [_make_db_skill()])
        _raise_if_resolve_called(monkeypatch)

        payload = json.loads(
            await _skill_view_handler(
                {"name": "wiki", "file_path": "scripts/x.py"},
                tenant=_make_tenant(tmp_path),
            )
        )

        assert payload["success"] is False
        assert "DB-backed" in payload["error"]
        assert "no linked files" in payload["error"]

    async def test_empty_body_yields_no_body_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_load_all_skills(
            monkeypatch,
            [_make_db_skill(name="empty", content="")],
        )
        _raise_if_resolve_called(monkeypatch)

        payload = json.loads(
            await _skill_view_handler(
                {"name": "empty"},
                tenant=_make_tenant(tmp_path),
            )
        )

        assert payload["success"] is False
        assert "has no body" in payload["error"]

    async def test_db_skill_shadows_disk_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If both a DB row and an on-disk dir exist, the DB body wins.

        ``ResourceLoader.load_skills`` already gives DB layers precedence;
        the local fallback aligns with that so anonymous and authenticated
        sessions see the same body.
        """
        _stub_load_all_skills(monkeypatch, [_make_db_skill(content="DB BODY")])

        # Materialise an on-disk copy with different content.  The DB branch
        # is source-driven, so this should be ignored.
        platform_dir = tmp_path / "platform-skills" / "wiki"
        platform_dir.mkdir(parents=True)
        (platform_dir / "SKILL.md").write_text(
            "---\nname: wiki\n---\nFS BODY\n", encoding="utf-8"
        )
        # Force any disk resolve to point at the FS body so we'd notice if
        # the wrong branch ran.
        monkeypatch.setattr(
            skills_mod,
            "_resolve_skill_dir",
            lambda *a, **kw: platform_dir,
        )

        payload = json.loads(
            await _skill_view_handler(
                {"name": "wiki"},
                tenant=_make_tenant(tmp_path),
            )
        )

        assert payload["success"] is True
        assert payload["content"] == "DB BODY"


# ---------------------------------------------------------------------------
# Disk-backed branch (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDiskBackedSkill:
    async def test_reads_skill_md_and_linked_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disk-backed skills still produce their full response shape."""
        skill_dir = tmp_path / "skills" / "wiki"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: wiki\ndescription: From disk\n---\n# Wiki\nbody\n",
            encoding="utf-8",
        )
        refs = skill_dir / "references"
        refs.mkdir()
        (refs / "api.md").write_text("api ref", encoding="utf-8")

        disk_skill = SkillDef(
            name="wiki",
            description="From disk",
            content="# Wiki\nbody\n",
            source=SKILL_SOURCE_PLATFORM,
        )
        _stub_load_all_skills(monkeypatch, [disk_skill])
        monkeypatch.setattr(
            skills_mod,
            "_resolve_skill_dir",
            lambda *a, **kw: skill_dir,
        )

        payload = json.loads(
            await _skill_view_handler(
                {"name": "wiki"},
                tenant=_make_tenant(tmp_path),
            )
        )

        assert payload["success"] is True
        assert payload["name"] == "wiki"
        assert payload["description"] == "From disk"
        assert payload["linked_files"] == {"references": ["references/api.md"]}
        assert "# Wiki\nbody" in payload["content"]


# ---------------------------------------------------------------------------
# Other error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEmptyNameApiMediated:
    """An empty/missing name must be rejected on BOTH the local and the
    API-mediated paths. Forwarding an empty name to the API server builds
    ``/v1/skills/`` which 307-redirects, surfacing an opaque error to the agent.
    """

    async def test_empty_name_not_forwarded_to_api(self) -> None:
        class _ExplodingApiClient:
            async def view_skill(self, name: str, file_path: Any = None) -> str:
                raise AssertionError(
                    "view_skill must not be called with an empty name"
                )

        payload = json.loads(
            await _skill_view_handler(
                {"name": ""},
                api_client=_ExplodingApiClient(),
            )
        )
        assert payload["success"] is False
        assert "name is required" in payload["error"].lower()

    async def test_missing_name_not_forwarded_to_api(self) -> None:
        class _ExplodingApiClient:
            async def view_skill(self, name: str, file_path: Any = None) -> str:
                raise AssertionError("view_skill must not be called")

        payload = json.loads(
            await _skill_view_handler({}, api_client=_ExplodingApiClient())
        )
        assert payload["success"] is False
        assert "name is required" in payload["error"].lower()

    async def test_valid_name_still_delegates_to_api(self) -> None:
        seen: dict[str, Any] = {}

        class _RecordingApiClient:
            async def view_skill(self, name: str, file_path: Any = None) -> str:
                seen["name"] = name
                return json.dumps({"success": True, "name": name})

        payload = json.loads(
            await _skill_view_handler(
                {"name": "wiki"},
                api_client=_RecordingApiClient(),
            )
        )
        assert seen["name"] == "wiki"
        assert payload["success"] is True


@pytest.mark.asyncio
class TestUnknownSkill:
    async def test_unknown_name_returns_helpful_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_load_all_skills(monkeypatch, [])

        payload = json.loads(
            await _skill_view_handler(
                {"name": "unknown"},
                tenant=_make_tenant(tmp_path),
            )
        )

        assert payload["success"] is False
        assert "'unknown' not found" in payload["error"]
        assert payload["available_skills"] == []
