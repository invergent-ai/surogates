"""Tests ensuring SKILL.graph.json is never exposed to agents in listings or reads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from surogates.tenant.context import TenantContext
from surogates.tools.builtin import skills as skills_mod
from surogates.tools.builtin.skills import _skill_view_handler
from surogates.tools.loader import SKILL_SOURCE_PLATFORM, SkillDef


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


# ---------------------------------------------------------------------------
# Graph file hiding in listings and reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSkillViewHidesGraphFile:
    async def test_graph_file_not_in_linked_files_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A skill with SKILL.md, SKILL.graph.json, and references/ lists only references/."""
        skill_dir = tmp_path / "skills" / "proc"
        skill_dir.mkdir(parents=True)

        # Create SKILL.md
        (skill_dir / "SKILL.md").write_text(
            "---\nname: proc\ndescription: Procedure skill\n---\n# Proc\nbody\n",
            encoding="utf-8",
        )

        # Create SKILL.graph.json (should be hidden)
        (skill_dir / "SKILL.graph.json").write_text(
            '{"version": 1, "nodes": []}',
            encoding="utf-8",
        )

        # Create supporting files (should be visible)
        refs = skill_dir / "references"
        refs.mkdir()
        (refs / "notes.md").write_text("reference notes", encoding="utf-8")

        disk_skill = SkillDef(
            name="proc",
            description="Procedure skill",
            content="# Proc\nbody\n",
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
                {"name": "proc"},
                tenant=_make_tenant(tmp_path),
            )
        )

        assert payload["success"] is True
        # The graph file must NOT appear in linked_files
        linked_files = payload.get("linked_files", {})
        all_files = []
        for file_list in linked_files.values():
            all_files.extend(file_list)

        assert all_files == ["references/notes.md"], \
            f"Expected only ['references/notes.md'], got {all_files}"
        assert not any("SKILL.graph.json" in f for f in all_files), \
            "SKILL.graph.json must not appear in linked_files"

    async def test_graph_file_read_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skill_view with file_path='SKILL.graph.json' returns an error."""
        skill_dir = tmp_path / "skills" / "proc"
        skill_dir.mkdir(parents=True)

        (skill_dir / "SKILL.md").write_text(
            "---\nname: proc\ndescription: Procedure skill\n---\n# Proc\nbody\n",
            encoding="utf-8",
        )

        (skill_dir / "SKILL.graph.json").write_text(
            '{"version": 1, "nodes": []}',
            encoding="utf-8",
        )

        refs = skill_dir / "references"
        refs.mkdir()
        (refs / "notes.md").write_text("reference notes", encoding="utf-8")

        disk_skill = SkillDef(
            name="proc",
            description="Procedure skill",
            content="# Proc\nbody\n",
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
                {"name": "proc", "file_path": "SKILL.graph.json"},
                tenant=_make_tenant(tmp_path),
            )
        )

        # The graph file read must be rejected with an error
        assert payload["success"] is False
        assert "graph" in payload["error"].lower() or "not readable" in payload["error"].lower() or "not found" in payload["error"].lower()

    async def test_graph_file_read_rejected_with_variant_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """skill_view with file_path='./SKILL.graph.json' (normalized variant) returns an error."""
        skill_dir = tmp_path / "skills" / "proc"
        skill_dir.mkdir(parents=True)

        (skill_dir / "SKILL.md").write_text(
            "---\nname: proc\ndescription: Procedure skill\n---\n# Proc\nbody\n",
            encoding="utf-8",
        )

        (skill_dir / "SKILL.graph.json").write_text(
            '{"version": 1, "nodes": []}',
            encoding="utf-8",
        )

        disk_skill = SkillDef(
            name="proc",
            description="Procedure skill",
            content="# Proc\nbody\n",
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
                {"name": "proc", "file_path": "./SKILL.graph.json"},
                tenant=_make_tenant(tmp_path),
            )
        )

        # The normalized graph file read must also be rejected
        assert payload["success"] is False
        assert "graph" in payload["error"].lower() or "not readable" in payload["error"].lower() or "not found" in payload["error"].lower()
