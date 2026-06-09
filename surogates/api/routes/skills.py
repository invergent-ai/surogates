"""Skills REST API — CRUD operations on skills.

Provides endpoints for listing, viewing, creating, editing, patching,
and deleting skills and their supporting files.  All endpoints are
tenant-scoped via ``TenantContext``.

User and org-shared skills are stored on a ``StorageBackend`` (local
filesystem in dev, S3 in production).  Platform skills come from the
per-agent Surogate Hub bundle (``skills/<name>/...``) and are
read-only — they are included in list/view responses via the bundle
accessor, not the filesystem.

``GET /skills/{name}`` and ``GET /skills/{name}/file`` accept an
optional ``session_id`` query parameter.  When supplied, the skill's
supporting files (``scripts/``, ``assets/``, ``templates/``,
``references/``) are auto-staged into ``sessions/{session_id}/.skills/
{name}/`` so the sandbox can execute scripts and read binary assets
directly at ``{workspace_path}/.skills/{name}/``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from surogates.api.routes._shared import (
    normalize_source,
    raise_validation,
    require_not_channel_principal,
    resolve_agent_bundle as _resolve_agent_bundle,
    resolve_system_bundle as _resolve_system_bundle,
)
from surogates.storage.skill_staging import SkillStager, has_stageable_assets
from surogates.storage.tenant import (
    TenantStorage,
    agent_session_bucket,
)
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

# Read endpoints (list / view / file fetch) are mounted at both ``/v1/``
# (JWT) and ``/v1/api/`` (service-account) so ops can populate the chat
# slash menu with the harness's merged skill catalogue without forwarding
# a user JWT.  Write endpoints (create/edit/delete + expert lifecycle)
# stay JWT-only — service-account principals don't manage skills.
read_router = APIRouter()
write_router = APIRouter()


# ---------------------------------------------------------------------------
# Response / request schemas
# ---------------------------------------------------------------------------


class SkillSummary(BaseModel):
    """Lightweight skill info for the slash-command menu."""

    name: str
    description: str
    type: str = "skill"
    category: str | None = None
    trigger: str | None = None
    source: str = "platform"  # "platform", "org", or "user"
    # True only for framework built-ins (the shared system-skills
    # bundle). Org-attached per-agent skills also carry
    # ``source="platform"``, so the slash menu keys "hide built-ins" on
    # this flag, not on ``source``.
    builtin: bool = False
    # Expert-specific fields (only present when type="expert").
    expert_status: str | None = None
    expert_endpoint: str | None = None
    expert_model: str | None = None


class SkillListResponse(BaseModel):
    skills: list[SkillSummary]
    total: int


class SkillDetail(BaseModel):
    """Full skill content with linked files listing."""

    name: str
    description: str
    type: str = "skill"
    content: str
    category: str | None = None
    tags: list[str] | None = None
    trigger: str | None = None
    source: str  # "platform", "org", "user"
    linked_files: list[str] = Field(default_factory=list)
    #: Workspace-visible directory where supporting files have been staged,
    #: populated only when ``session_id`` was supplied and the skill has
    #: stageable assets (scripts/, assets/, templates/, references/).
    staged_at: str | None = None
    # Expert-specific fields.
    expert_model: str | None = None
    expert_endpoint: str | None = None
    expert_adapter: str | None = None
    expert_status: str | None = None
    expert_tools: list[str] | None = None
    expert_max_iterations: int | None = None
    expert_stats: dict[str, Any] | None = None


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
        bucket=request.app.state.settings.storage.bucket,
    )


def _get_skill_stager(
    request: Request, storage_key_prefix: str = "",
) -> SkillStager:
    """Create a ``SkillStager`` bound to the request's storage backend.

    Threads the app-wide Redis client through so concurrent staging calls
    for the same ``(session_id, skill_name)`` are serialised across
    worker replicas.  When Redis is not wired (tests / dev without a
    broker), the stager falls back to an in-process ``asyncio.Lock``.

    *storage_key_prefix* is the per-agent prefix under the shared
    workspace bucket (typically ``{project_id}/{agent_id}``).  Callers
    fetch it from the session config; ``""`` reproduces the
    bucket-rooted layout used before the shared-bucket cutover.
    """
    redis = getattr(request.app.state, "redis", None)
    bucket = agent_session_bucket(request.app.state.settings.storage.bucket)
    return SkillStager(
        backend=request.app.state.storage,
        storage_bucket=bucket,
        storage_key_prefix=storage_key_prefix,
        redis=redis,
    )


def _session_storage_key_prefix(session: Any) -> str:
    """Return the per-agent storage prefix stamped onto *session*."""
    return (session.config or {}).get("storage_key_prefix", "") or ""


def _resource_loader(request: Request):
    """Build a resource loader from the active API settings."""
    from surogates.tools.loader import ResourceLoader

    return ResourceLoader.from_settings(request.app.state.settings)


def _staging_preamble(skill_name: str, staged_at: str) -> str:
    """Return a directive preamble that tells the LLM how to address staged files.

    Prepending this to the SKILL.md body lets authors write relative paths
    (``scripts/foo.py``) without knowing about staging.  The preamble is
    phrased as a direct instruction (not a passive statement) because the
    sandbox CWD is ``/workspace``, not the skill directory -- the LLM must
    actively prepend ``staged_at`` to every relative path the skill body
    mentions or the command will fail with "No such file or directory".
    """
    base = staged_at.rstrip("/")
    return (
        f"> **Skill staging.** This skill's files live at `{base}/` "
        f"inside the sandbox.  The sandbox working directory is "
        f"`/workspace`, NOT the skill directory, so every relative path "
        f"that appears below MUST be prefixed with `{base}/` when you "
        f"invoke it.  For example, `scripts/foo.py` in this document "
        f"means `{base}/scripts/foo.py` on the command line; "
        f"`assets/template.pptx` means `{base}/assets/template.pptx`. "
        f"Do not `cd` into the skill directory -- prefix the paths.\n\n"
    )


async def _authorize_session_for_staging(
    request: Request,
    tenant: TenantContext,
    session_id: UUID,
) -> Any:
    """Verify *session_id* belongs to the tenant and return the session.

    Staging writes under ``sessions/{session_id}/`` in the agent bucket;
    without this check any authenticated user could pollute another tenant's
    sessions or trigger arbitrary workspace writes with forged UUIDs.  Raises
    ``HTTPException(404)`` with a generic message for both the not-found,
    wrong-tenant, and wrong-agent cases to avoid leaking session existence.

    Returns the authorized session so callers can read its
    ``storage_key_prefix`` without re-fetching from the store.
    """
    from surogates.api.routes.sessions import _get_session_for_tenant
    from surogates.runtime import agent_runtime_context_dep

    # ``_get_session_for_tenant`` requires ``agent_id`` to gate
    # cross-agent session reads.  Resolve it via
    # ``agent_runtime_context_dep`` so the staging helper stays in
    # lock-step with every other session-scoped route.
    agent_runtime = await agent_runtime_context_dep(request)
    return await _get_session_for_tenant(
        request, session_id, tenant, agent_runtime.agent_id,
    )


async def _stage_skill_for_session(
    request: Request,
    tenant: TenantContext,
    skill_def: Any,
    session_id: UUID,
    linked_files: list[str] | dict[str, list[str]] | None,
    storage_key_prefix: str = "",
    bundle: Any = None,
) -> str | None:
    """Auto-stage a skill into the session workspace when it has assets to stage.

    Returns the workspace-visible ``staged_at`` path on success, or ``None``
    when the skill has nothing to stage (no supporting files, or DB-only).

    The caller is responsible for authorizing the session against the
    tenant via :func:`_authorize_session_for_staging` before calling this
    function.

    ``bundle`` is the per-tenant Hub-backed bundle for shared-runtime
    sessions; when set AND the platform skill has no on-disk source
    directory (i.e. it came from the bundle's ``skills/{name}/`` tree),
    we stage from the bundle instead of failing.
    """
    from surogates.tools.loader import (
        SKILL_SOURCE_ORG,
        SKILL_SOURCE_PLATFORM,
        SKILL_SOURCE_USER,
    )

    if not has_stageable_assets(linked_files):
        return None

    stager = _get_skill_stager(request, storage_key_prefix=storage_key_prefix)

    if skill_def.source == SKILL_SOURCE_PLATFORM:
        if bundle is None:
            logger.warning(
                "Cannot stage platform skill '%s': no bundle available "
                "(agent has no hub_ref configured)",
                skill_def.name,
            )
            return None
        return await stager.stage_from_bundle(
            session_id=session_id,
            skill_name=skill_def.name,
            bundle=bundle,
        )

    if skill_def.source in (SKILL_SOURCE_USER, SKILL_SOURCE_ORG):
        ts = _get_tenant_storage(request, tenant)
        existing = await ts.skill_exists(skill_def.name)
        if not existing:
            return None
        return await stager.stage_from_object_store(
            session_id=session_id,
            skill_name=skill_def.name,
            source_bucket=request.app.state.settings.storage.bucket,
            source_prefix=existing["key_prefix"],
        )

    # DB-backed skills have no linked files beyond what is in the content
    # column itself, so there is nothing to stage.
    return None


def _parse_frontmatter(content: str, fallback_name: str) -> dict[str, Any]:
    """Extract metadata from SKILL.md frontmatter."""
    from surogates.tools.loader import _parse_skill_frontmatter
    return _parse_skill_frontmatter(content, fallback_name)


def _populate_expert_summary(
    summary: SkillSummary,
    *,
    skill: Any = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Populate expert-specific fields on a :class:`SkillSummary`.

    Accepts either a :class:`~surogates.tools.loader.SkillDef` object
    or a parsed frontmatter dict.
    """
    if skill is not None:
        summary.expert_status = skill.expert_status
        summary.expert_endpoint = skill.expert_endpoint
        summary.expert_model = skill.expert_model
    elif meta is not None:
        summary.expert_status = meta.get("expert_status", "draft")
        summary.expert_endpoint = meta.get("expert_endpoint")
        summary.expert_model = meta.get("expert_model")


def _populate_expert_detail(
    detail: SkillDetail,
    *,
    skill: Any = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Populate expert-specific fields on a :class:`SkillDetail`.

    Accepts either a :class:`~surogates.tools.loader.SkillDef` object
    or a parsed frontmatter dict.
    """
    if skill is not None:
        detail.expert_model = skill.expert_model
        detail.expert_endpoint = skill.expert_endpoint
        detail.expert_adapter = skill.expert_adapter
        detail.expert_status = skill.expert_status
        detail.expert_tools = skill.expert_tools
        detail.expert_max_iterations = skill.expert_max_iterations
    elif meta is not None:
        detail.expert_model = meta.get("expert_model")
        detail.expert_endpoint = meta.get("expert_endpoint")
        detail.expert_adapter = meta.get("expert_adapter")
        detail.expert_status = meta.get("expert_status", "draft")
        detail.expert_tools = meta.get("expert_tools")
        max_iter = meta.get("expert_max_iterations")
        if max_iter is not None:
            try:
                detail.expert_max_iterations = int(max_iter)
            except (TypeError, ValueError):
                pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@read_router.get("/skills", response_model=SkillListResponse)
async def list_skills(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    type: str | None = None,
) -> SkillListResponse:
    """List available skills from all layers (platform, user files, org DB, user DB).

    Returns lightweight summaries suitable for the frontend
    slash-command menu.

    Query Parameters
    ----------------
    type:
        Optional filter: ``"skill"`` for regular skills, ``"expert"``
        for expert skills, or ``None`` (default) for all.
    """
    loader = _resource_loader(request)
    session_factory = request.app.state.session_factory
    bundle = await _resolve_agent_bundle(request)
    system_bundle = await _resolve_system_bundle(request)
    async with session_factory() as db_session:
        all_skills = await loader.load_skills(
            tenant,
            db_session=db_session,
            bundle=bundle,
            system_bundle=system_bundle,
        )

    summaries: list[SkillSummary] = []
    for skill in all_skills:
        if type is not None and skill.type != type:
            continue
        summary = SkillSummary(
            name=skill.name,
            description=skill.description,
            type=skill.type,
            category=skill.category,
            trigger=skill.trigger,
            source=normalize_source(skill.source),
            builtin=skill.builtin,
        )
        if skill.is_expert:
            _populate_expert_summary(summary, skill=skill)
        summaries.append(summary)

    summaries.sort(key=lambda s: (s.category or "", s.name))
    return SkillListResponse(skills=summaries, total=len(summaries))


@read_router.get("/skills/{name}", response_model=SkillDetail)
async def view_skill(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    session_id: UUID | None = None,
) -> SkillDetail:
    """View full skill content and linked files listing.

    When ``session_id`` is provided and the skill has supporting files,
    the skill tree is auto-staged into ``sessions/{session_id}/.skills/
    {name}/`` and a ``staged_at`` workspace path is returned.  A one-line
    preamble is prepended to ``content`` so the LLM can resolve relative
    paths (``scripts/foo.py``) against the staged directory.
    """
    from surogates.tools.loader import SKILL_SOURCE_PLATFORM, SKILL_SOURCE_USER

    loader = _resource_loader(request)
    session_factory = request.app.state.session_factory
    bundle = await _resolve_agent_bundle(request)
    system_bundle = await _resolve_system_bundle(request)
    async with session_factory() as db_session:
        all_skills = await loader.load_skills(
            tenant,
            db_session=db_session,
            bundle=bundle,
            system_bundle=system_bundle,
        )

    skill_def = next((s for s in all_skills if s.name == name), None)
    if skill_def is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    detail = SkillDetail(
        name=skill_def.name,
        description=skill_def.description,
        type=skill_def.type,
        content=skill_def.content,
        category=skill_def.category,
        tags=skill_def.tags,
        trigger=skill_def.trigger,
        source=normalize_source(skill_def.source),
    )
    if skill_def.is_expert:
        _populate_expert_detail(detail, skill=skill_def)

    # Populate linked_files from the skill's source layer.
    if skill_def.source == SKILL_SOURCE_USER:
        ts = _get_tenant_storage(request, tenant)
        existing = await ts.skill_exists(name)
        if existing:
            files = await ts.list_skill_files(existing["key_prefix"])
            detail.linked_files = [f for f in files if f != "SKILL.md"]
    elif skill_def.source == SKILL_SOURCE_PLATFORM and bundle is not None:
        # Bundle-backed platform skill: enumerate the bundle's
        # ``skills/{name}/`` prefix.  SKILL.md is excluded so the list
        # contains only the auxiliary files that get auto-staged.
        prefix = f"skills/{name}/"
        bundle_paths = await bundle.list(prefix)
        detail.linked_files = sorted(
            p[len(prefix):] for p in bundle_paths
            if p.startswith(prefix) and not p.endswith("/SKILL.md")
            and p != prefix
        )

    # Auto-stage the skill tree when a session is specified and there are
    # files beyond SKILL.md itself.  Authorize first: the session must
    # belong to this tenant before we write into its bucket.
    if session_id is not None:
        session = await _authorize_session_for_staging(request, tenant, session_id)
        staged_at = await _stage_skill_for_session(
            request=request,
            tenant=tenant,
            skill_def=skill_def,
            session_id=session_id,
            linked_files=detail.linked_files,
            storage_key_prefix=_session_storage_key_prefix(session),
            bundle=bundle,
        )
        if staged_at is not None:
            detail.staged_at = staged_at
            detail.content = _staging_preamble(name, staged_at) + detail.content

    return detail


@read_router.get("/skills/{name}/file")
async def read_skill_file(
    name: str,
    path: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    session_id: UUID | None = None,
) -> dict[str, Any]:
    """Read a linked file from a skill directory.

    When ``session_id`` is provided and the file is binary, the skill tree
    is auto-staged and the response points the caller at the staged
    workspace path instead of returning a placeholder.  Text files are
    always returned inline regardless of ``session_id``.

    Platform skills (bundle-backed) are supported in addition to
    tenant-bucket-backed user/org skills.
    """
    # Reads aren't constrained to references/templates/scripts/assets —
    # the listing endpoint advertises any non-SKILL.md file (including
    # root-level docs like editing.md, LICENSE.txt). Only block path
    # traversal here; both branches concatenate prefix + path so a
    # ``../`` segment is the only escape worth filtering at this layer.
    if ".." in Path(path).parts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Path traversal ('..') is not allowed.",
        )

    from surogates.tools.loader import SKILL_SOURCE_PLATFORM

    # Authorize the session up-front: any redirect-to-staged path writes
    # into the session workspace, so ownership must be verified even though
    # the caller may only be reading a text file in the end.
    session_for_staging: Any | None = None
    if session_id is not None:
        session_for_staging = await _authorize_session_for_staging(
            request, tenant, session_id,
        )

    loader = _resource_loader(request)
    session_factory = request.app.state.session_factory
    bundle = await _resolve_agent_bundle(request)
    system_bundle = await _resolve_system_bundle(request)
    async with session_factory() as db_session:
        all_skills = await loader.load_skills(
            tenant,
            db_session=db_session,
            bundle=bundle,
            system_bundle=system_bundle,
        )
    skill_def = next((s for s in all_skills if s.name == name), None)
    if skill_def is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    async def _redirect_to_staged(skill_def_to_stage: Any) -> dict[str, Any] | None:
        """Stage the skill and return a redirect response, or ``None``."""
        if session_id is None or session_for_staging is None:
            return None
        key_prefix = _session_storage_key_prefix(session_for_staging)
        staged_at = await _stage_skill_for_session(
            request=request,
            tenant=tenant,
            skill_def=skill_def_to_stage,
            session_id=session_id,
            linked_files=[path],  # forces stageable_assets to be True
            storage_key_prefix=key_prefix,
        )
        if staged_at is None:
            return None
        stager = _get_skill_stager(request, storage_key_prefix=key_prefix)
        return {
            "file_path": path,
            "binary": True,
            "staged_at": staged_at,
            "staged_file_path": stager.staged_file_path(session_id, name, path),
            "content": None,
            "hint": (
                f"File is available in the sandbox at "
                f"`{stager.staged_file_path(session_id, name, path)}`."
            ),
        }

    # Platform skills come from the per-agent Surogate Hub bundle.
    if skill_def.source == SKILL_SOURCE_PLATFORM:
        if bundle is None:
            raise HTTPException(
                status_code=404,
                detail=f"Skill '{name}' source not found (no bundle).",
            )
        bundle_path = f"skills/{name}/{path}"
        try:
            content = await bundle.read_text(bundle_path)
        except LookupError:
            raise HTTPException(
                status_code=404,
                detail=f"File '{path}' not found in skill '{name}'.",
            )
        except UnicodeDecodeError:
            redirect = await _redirect_to_staged(skill_def)
            if redirect is not None:
                return redirect
            return {"file_path": path, "content": "[Binary file]", "binary": True}
        return {"file_path": path, "content": content, "binary": False}

    # Tenant-bucket-backed skills (user / org-shared).
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
        redirect = await _redirect_to_staged(skill_def)
        if redirect is not None:
            return redirect
        return {"file_path": path, "content": "[Binary file]", "binary": True}


@write_router.post("/skills", response_model=SkillActionResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: CreateSkillRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Create a new user skill."""
    require_not_channel_principal(tenant)
    raise_validation(validate_name(body.name))
    raise_validation(validate_category(body.category))
    raise_validation(validate_frontmatter(body.content))
    raise_validation(validate_content_size(body.content))

    ts = _get_tenant_storage(request, tenant)

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


@write_router.put("/skills/{name}", response_model=SkillActionResponse)
async def edit_skill(
    name: str,
    body: EditSkillRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Replace the full SKILL.md content of an existing skill."""
    require_not_channel_principal(tenant)
    raise_validation(validate_frontmatter(body.content))
    raise_validation(validate_content_size(body.content))

    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    await ts.overwrite_skill(existing["key_prefix"], body.content)

    return SkillActionResponse(
        success=True,
        message=f"Skill '{name}' updated.",
    )


@write_router.patch("/skills/{name}", response_model=SkillActionResponse)
async def patch_skill(
    name: str,
    body: PatchSkillRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Targeted find-and-replace within a skill file."""
    require_not_channel_principal(tenant)
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    key_prefix = existing["key_prefix"]
    file_key = body.file_path or "SKILL.md"

    if body.file_path:
        raise_validation(validate_file_path(body.file_path))

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
    raise_validation(validate_content_size(new_content, label=file_key))

    if not body.file_path:
        raise_validation(validate_frontmatter(new_content))
        await ts.overwrite_skill(key_prefix, new_content)
    else:
        await ts.write_skill_file(key_prefix, body.file_path, new_content)

    return SkillActionResponse(
        success=True,
        message=f"Patched {file_key} in skill '{name}' ({count} replacement{'s' if count > 1 else ''}).",
    )


@write_router.delete("/skills/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Delete a skill and all its files."""
    require_not_channel_principal(tenant)
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    await ts.delete_skill(existing["key_prefix"])


@write_router.post("/skills/{name}/files", response_model=SkillActionResponse, status_code=status.HTTP_201_CREATED)
async def write_skill_file(
    name: str,
    body: WriteFileRequest,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Add or overwrite a supporting file within a skill directory."""
    require_not_channel_principal(tenant)
    raise_validation(validate_file_path(body.file_path))
    raise_validation(validate_content_size(body.file_content, label=body.file_path))
    raise_validation(validate_file_size(body.file_content))

    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    await ts.write_skill_file(existing["key_prefix"], body.file_path, body.file_content)

    return SkillActionResponse(
        success=True,
        message=f"File '{body.file_path}' written to skill '{name}'.",
    )


@write_router.delete("/skills/{name}/files", status_code=status.HTTP_204_NO_CONTENT)
async def remove_skill_file(
    name: str,
    path: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Remove a supporting file from a skill directory."""
    require_not_channel_principal(tenant)
    raise_validation(validate_file_path(path))

    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    if not await ts.skill_file_exists(existing["key_prefix"], path):
        raise HTTPException(status_code=404, detail=f"File '{path}' not found in skill '{name}'.")

    await ts.delete_skill_file(existing["key_prefix"], path)


# ---------------------------------------------------------------------------
# Expert-specific action endpoints
# ---------------------------------------------------------------------------


class ExpertActivateRequest(BaseModel):
    """Request body for activating an expert skill."""
    endpoint: str | None = None  # optionally set endpoint during activation


class ExpertTrainingDataResponse(BaseModel):
    """Response for training data listing."""
    datasets: list[str]
    total: int


@write_router.post("/skills/{name}/activate", response_model=SkillActionResponse)
async def activate_expert(
    name: str,
    request: Request,
    body: ExpertActivateRequest | None = None,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Set an expert skill's status to ``active``.

    Requires the skill to be ``type: expert`` and to have an
    ``endpoint`` configured (either in the SKILL.md frontmatter, the
    DB overlay, or provided in the request body).
    """
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    content = await ts.read_skill(existing["key_prefix"])
    meta = _parse_frontmatter(content, name)

    if meta.get("type") != "expert":
        raise HTTPException(
            status_code=400,
            detail=f"Skill '{name}' is not an expert (type={meta.get('type', 'skill')}).",
        )

    # Check that an endpoint is available.
    endpoint = (
        (body.endpoint if body else None)
        or meta.get("expert_endpoint")
    )
    if not endpoint:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Expert '{name}' has no endpoint configured. "
                "Set 'endpoint' in SKILL.md frontmatter or provide it in the request body."
            ),
        )

    # Update the frontmatter to set expert_status: active (and endpoint if provided).
    new_content = _update_frontmatter_field(content, "expert_status", "active")
    if body and body.endpoint:
        new_content = _update_frontmatter_field(new_content, "endpoint", body.endpoint)

    raise_validation(validate_frontmatter(new_content))
    await ts.overwrite_skill(existing["key_prefix"], new_content)

    return SkillActionResponse(
        success=True,
        message=f"Expert '{name}' activated with endpoint: {endpoint}",
    )


@write_router.post("/skills/{name}/retire", response_model=SkillActionResponse)
async def retire_expert(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Set an expert skill's status to ``retired``."""
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    content = await ts.read_skill(existing["key_prefix"])
    meta = _parse_frontmatter(content, name)

    if meta.get("type") != "expert":
        raise HTTPException(
            status_code=400,
            detail=f"Skill '{name}' is not an expert.",
        )

    new_content = _update_frontmatter_field(content, "expert_status", "retired")
    await ts.overwrite_skill(existing["key_prefix"], new_content)

    return SkillActionResponse(
        success=True,
        message=f"Expert '{name}' retired.",
    )


@write_router.post("/skills/{name}/collect", response_model=SkillActionResponse)
async def collect_training_data(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> SkillActionResponse:
    """Trigger training data export from the event log for an expert.

    Collects successful conversation trajectories involving this expert
    and writes them as JSONL to the skill's ``training/`` directory.
    """
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    content = await ts.read_skill(existing["key_prefix"])
    meta = _parse_frontmatter(content, name)

    if meta.get("type") != "expert":
        raise HTTPException(
            status_code=400,
            detail=f"Skill '{name}' is not an expert.",
        )

    # Import the collector and run collection.
    from surogates.jobs.training_collector import TrainingDataCollector

    session_store = getattr(request.app.state, "session_store", None)
    storage_backend = getattr(request.app.state, "storage", None)

    if session_store is None:
        raise HTTPException(
            status_code=503,
            detail="Session store not available for training data collection.",
        )

    collector = TrainingDataCollector(
        session_store=session_store,
        storage=storage_backend,
    )
    examples = await collector.collect_for_expert(
        expert_name=name,
        org_id=tenant.org_id,
    )

    if not examples:
        return SkillActionResponse(
            success=True,
            message=f"No training data found for expert '{name}'.",
        )

    key = await collector.export_jsonl(
        expert_name=name,
        examples=examples,
        org_id=tenant.org_id,
    )

    # Update frontmatter to collecting status if still in draft.
    if meta.get("expert_status", "draft") == "draft":
        new_content = _update_frontmatter_field(content, "expert_status", "collecting")
        await ts.overwrite_skill(existing["key_prefix"], new_content)

    return SkillActionResponse(
        success=True,
        message=f"Exported {len(examples)} training examples to {key}",
    )


@write_router.get("/skills/{name}/training-data", response_model=ExpertTrainingDataResponse)
async def list_training_data(
    name: str,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> ExpertTrainingDataResponse:
    """List exported training datasets for an expert skill."""
    ts = _get_tenant_storage(request, tenant)
    existing = await ts.skill_exists(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")

    # List files under the training/ subdirectory.
    files = await ts.list_skill_files(existing["key_prefix"])
    training_files = [
        f for f in files
        if f.startswith("training/") and f.endswith(".jsonl")
    ]

    return ExpertTrainingDataResponse(
        datasets=sorted(training_files),
        total=len(training_files),
    )


def _update_frontmatter_field(content: str, key: str, value: str) -> str:
    """Delegate to :func:`~surogates.tools.loader.update_frontmatter_field`."""
    from surogates.tools.loader import update_frontmatter_field
    return update_frontmatter_field(content, key, value)
