"""Skills REST API — CRUD operations on skills.

Provides endpoints for listing, viewing, creating, editing, patching,
and deleting skills and their supporting files.  All endpoints are
tenant-scoped via ``TenantContext``.

Skills are stored on a ``StorageBackend`` (local filesystem in dev,
MinIO/S3 in production).  Platform skills (baked into the container
image at ``/etc/surogates/skills/``) are read-only and included in
list/view responses.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from surogates.storage.tenant import TenantStorage
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tools.builtin.skill_validation import (
    validate_category,
    validate_content_size,
    validate_file_path,
    validate_file_size,
    validate_frontmatter,
    validate_name,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response / request schemas
# ---------------------------------------------------------------------------


class SkillSummary(BaseModel):
    """Lightweight skill info for the slash-command menu."""

    name: str
    description: str
    category: str | None = None
    trigger: str | None = None


class SkillListResponse(BaseModel):
    skills: list[SkillSummary]
    total: int


class SkillDetail(BaseModel):
    """Full skill content with linked files listing."""

    name: str
    description: str
    content: str
    category: str | None = None
    tags: list[str] | None = None
    trigger: str | None = None
    source: str  # "platform", "org", "user"
    linked_files: list[str] = Field(default_factory=list)


class CreateSkillRequest(BaseModel):
    name: str
    content: str
    category: str | None = None


class EditSkillRequest(BaseModel):
    content: str


class PatchSkillRequest(BaseModel):
    old_string: str
    new_string: str
    file_path: str | None = None
    replace_all: bool = False


class WriteFileRequest(BaseModel):
    file_path: str
    file_content: str


class SkillActionResponse(BaseModel):
    success: bool
    message: str
    category: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tenant_storage(request: Request, tenant: TenantContext) -> TenantStorage:
    """Create a TenantStorage for the current request."""
    return TenantStorage(
        backend=request.app.state.storage,
        org_id=tenant.org_id,
        user_id=tenant.user_id,
    )


def _raise_validation(err: str | None) -> None:
    """Raise HTTP 422 if *err* is not None."""
    if err:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)


def _parse_frontmatter(content: str, fallback_name: str) -> dict[str, Any]:
    """Extract metadata from SKILL.md frontmatter."""
    from surogates.tools.loader import _parse_skill_frontmatter
    return _parse_skill_frontmatter(content, fallback_name)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/skills", response_model=SkillListResponse)
async def list_skills(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillListResponse:
    """List available skills from all layers (platform, org, user).

    Returns lightweight summaries suitable for the frontend
    slash-command menu.
    """
    ts = _get_tenant_storage(request, tenant)
    await ts.ensure_bucket()

    # S3-backed skills (org + user layers).
    s3_skills = await ts.list_all_skills()

    summaries: list[SkillSummary] = []
    seen_names: set[str] = set()

    for entry in s3_skills:
        name = entry["name"]
        seen_names.add(name)
        try:
            content = await ts.read_skill(entry["key_prefix"])
            meta = _parse_frontmatter(content, name)
            summaries.append(SkillSummary(
                name=meta.get("name", name),
                description=meta.get("description", ""),
                category=meta.get("category"),
                trigger=meta.get("trigger"),
            ))
        except (KeyError, Exception):
            logger.warning("Failed to read skill %s", name, exc_info=True)

    # Platform skills (container filesystem, read-only).
    from surogates.tools.loader import ResourceLoader
    loader = ResourceLoader()
    platform_skills = loader._load_skills_from_dir(
        loader._platform_skills_dir, "platform",
    )
    for skill in platform_skills:
        if skill.name not in seen_names:
            summaries.append(SkillSummary(
                name=skill.name,
                description=skill.description,
                category=skill.category,
                trigger=skill.trigger,
            ))

    summaries.sort(key=lambda s: (s.category or "", s.name))
    return SkillListResponse(skills=summaries, total=len(summaries))


@router.get("/skills/{name}", response_model=SkillDetail)
async def view_skill(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillDetail:
    """View full skill content and linked files listing."""
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)

    if not existing:
        # Check platform skills.
        from surogates.tools.loader import ResourceLoader
        loader = ResourceLoader()
        platform_skills = loader._load_skills_from_dir(
            loader._platform_skills_dir, "platform",
        )
        skill_def = next((s for s in platform_skills if s.name == name), None)
        if skill_def:
            return SkillDetail(
                name=skill_def.name,
                description=skill_def.description,
                content=skill_def.content,
                category=skill_def.category,
                tags=skill_def.tags,
                trigger=skill_def.trigger,
                source="platform",
            )
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    content = await ts.read_skill(existing["key_prefix"])
    meta = _parse_frontmatter(content, name)
    files = await ts.list_skill_files(existing["key_prefix"])
    # Filter out SKILL.md itself.
    linked = [f for f in files if f != "SKILL.md"]

    return SkillDetail(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        content=content,
        category=meta.get("category"),
        tags=meta.get("tags"),
        trigger=meta.get("trigger"),
        source=existing["layer"],
        linked_files=linked,
    )


@router.get("/skills/{name}/file")
async def read_skill_file(
    name: str,
    path: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> dict[str, Any]:
    """Read a linked file from a skill directory."""
    _raise_validation(validate_file_path(path))

    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    if not await ts.skill_file_exists(existing["key_prefix"], path):
        raise HTTPException(status_code=404, detail=f"File '{path}' not found in skill '{name}'.")

    try:
        content = await ts.read_skill_file(existing["key_prefix"], path)
        return {"file_path": path, "content": content, "binary": False}
    except UnicodeDecodeError:
        return {"file_path": path, "content": "[Binary file]", "binary": True}


@router.post("/skills", response_model=SkillActionResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: CreateSkillRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Create a new user skill."""
    _raise_validation(validate_name(body.name))
    _raise_validation(validate_category(body.category))
    _raise_validation(validate_frontmatter(body.content))
    _raise_validation(validate_content_size(body.content))

    ts = _get_tenant_storage(request, tenant)
    await ts.ensure_bucket()

    existing = await ts.skill_exists(body.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A skill named '{body.name}' already exists.",
        )

    await ts.write_skill(body.name, body.content, body.category)

    return SkillActionResponse(
        success=True,
        message=f"Skill '{body.name}' created.",
        category=body.category,
    )


@router.put("/skills/{name}", response_model=SkillActionResponse)
async def edit_skill(
    name: str,
    body: EditSkillRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Replace the full SKILL.md content of an existing skill."""
    _raise_validation(validate_frontmatter(body.content))
    _raise_validation(validate_content_size(body.content))

    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    await ts.overwrite_skill(existing["key_prefix"], body.content)

    return SkillActionResponse(
        success=True,
        message=f"Skill '{name}' updated.",
    )


@router.patch("/skills/{name}", response_model=SkillActionResponse)
async def patch_skill(
    name: str,
    body: PatchSkillRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Targeted find-and-replace within a skill file."""
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    key_prefix = existing["key_prefix"]
    file_key = body.file_path or "SKILL.md"

    if body.file_path:
        _raise_validation(validate_file_path(body.file_path))

    # Read current content.
    try:
        if body.file_path:
            content = await ts.read_skill_file(key_prefix, body.file_path)
        else:
            content = await ts.read_skill(key_prefix)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"File '{file_key}' not found.")

    count = content.count(body.old_string)
    if count == 0:
        raise HTTPException(status_code=400, detail="old_string not found in file.")
    if count > 1 and not body.replace_all:
        raise HTTPException(
            status_code=400,
            detail=f"old_string matches {count} locations. Use replace_all=true or provide more context.",
        )

    new_content = content.replace(body.old_string, body.new_string)
    _raise_validation(validate_content_size(new_content, label=file_key))

    if not body.file_path:
        _raise_validation(validate_frontmatter(new_content))
        await ts.overwrite_skill(key_prefix, new_content)
    else:
        await ts.write_skill_file(key_prefix, body.file_path, new_content)

    return SkillActionResponse(
        success=True,
        message=f"Patched {file_key} in skill '{name}' ({count} replacement{'s' if count > 1 else ''}).",
    )


@router.delete("/skills/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Delete a skill and all its files."""
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    await ts.delete_skill(existing["key_prefix"])


@router.post("/skills/{name}/files", response_model=SkillActionResponse, status_code=status.HTTP_201_CREATED)
async def write_skill_file(
    name: str,
    body: WriteFileRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Add or overwrite a supporting file within a skill directory."""
    _raise_validation(validate_file_path(body.file_path))
    _raise_validation(validate_content_size(body.file_content, label=body.file_path))
    _raise_validation(validate_file_size(body.file_content))

    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    await ts.write_skill_file(existing["key_prefix"], body.file_path, body.file_content)

    return SkillActionResponse(
        success=True,
        message=f"File '{body.file_path}' written to skill '{name}'.",
    )


@router.delete("/skills/{name}/files", status_code=status.HTTP_204_NO_CONTENT)
async def remove_skill_file(
    name: str,
    path: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Remove a supporting file from a skill directory."""
    _raise_validation(validate_file_path(path))

    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    if not await ts.skill_file_exists(existing["key_prefix"], path):
        raise HTTPException(status_code=404, detail=f"File '{path}' not found in skill '{name}'.")

    await ts.delete_skill_file(existing["key_prefix"], path)
