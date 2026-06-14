"""Worker-local ``skill_view`` honours session-scoped skill overrides.

Shared-runtime sessions resolve overrides through the API path; dedicated
(helm) agents run tools worker-locally with ``api_client is None``.  For
those, the override must be applied in ``_skill_view_handler`` via
``_load_all_skills`` + the disk-branch override, so the candidate body is
served while supporting files keep resolving from the original skill tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from surogates.tenant.context import TenantContext
from surogates.tools.builtin.skills import _skill_view_handler


def _make_tenant(asset_root: Path) -> TenantContext:
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset({"read"}),
        asset_root=str(asset_root),
    )


def _write_user_skill(asset_root: Path, name: str, *, body: str, files=None) -> None:
    """Write a user-layer skill the loader + ``_resolve_skill_dir`` both see."""
    skill_dir = (
        asset_root
        / "00000000-0000-0000-0000-000000000001"
        / "users"
        / "00000000-0000-0000-0000-000000000002"
        / "skills"
        / name
    )
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: original desc\n---\n{body}\n",
        encoding="utf-8",
    )
    for rel, content in (files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_worker_local_skill_view_applies_override(tmp_path):
    tenant = _make_tenant(tmp_path)
    _write_user_skill(tmp_path, "browser-research", body="ORIGINAL BODY")

    out = await _skill_view_handler(
        {"name": "browser-research"},
        tenant=tenant,
        api_client=None,
        session_config={
            "skill_overrides": {
                "browser-research": {"content": "CANDIDATE BODY"},
            },
        },
    )
    payload = json.loads(out)
    assert payload["success"] is True
    assert "CANDIDATE BODY" in payload["content"]
    assert "ORIGINAL BODY" not in payload["content"]


@pytest.mark.asyncio
async def test_worker_local_skill_view_lists_original_files(tmp_path):
    tenant = _make_tenant(tmp_path)
    _write_user_skill(
        tmp_path, "research-staged", body="ORIGINAL",
        files={"scripts/run.py": "print('original script')"},
    )

    out = await _skill_view_handler(
        {"name": "research-staged"},
        tenant=tenant,
        api_client=None,
        session_config={
            "skill_overrides": {
                "research-staged": {"content": "CANDIDATE"},
            },
        },
    )
    payload = json.loads(out)
    assert "CANDIDATE" in payload["content"]
    # The original supporting files are still advertised, unaffected by the
    # content override.
    assert payload["linked_files"]["scripts"] == ["scripts/run.py"]


@pytest.mark.asyncio
async def test_worker_local_skill_view_no_override_serves_original(tmp_path):
    tenant = _make_tenant(tmp_path)
    _write_user_skill(tmp_path, "plain", body="ORIGINAL BODY")

    out = await _skill_view_handler(
        {"name": "plain"},
        tenant=tenant,
        api_client=None,
        session_config={},
    )
    payload = json.loads(out)
    assert "ORIGINAL BODY" in payload["content"]


@pytest.mark.asyncio
async def test_worker_local_skill_view_override_ignored_when_flag_disabled(
    tmp_path, monkeypatch,
):
    from surogates.config import Settings

    tenant = _make_tenant(tmp_path)
    _write_user_skill(tmp_path, "browser-research", body="ORIGINAL BODY")

    settings = Settings()
    settings.worker.skill_overrides_enabled = False

    out = await _skill_view_handler(
        {"name": "browser-research"},
        tenant=tenant,
        api_client=None,
        settings=settings,
        session_config={
            "skill_overrides": {
                "browser-research": {"content": "CANDIDATE BODY"},
            },
        },
    )
    payload = json.loads(out)
    assert "ORIGINAL BODY" in payload["content"]
    assert "CANDIDATE BODY" not in payload["content"]
