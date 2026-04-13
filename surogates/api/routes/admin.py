"""Admin endpoints -- org and user management."""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from surogates.db.models import ChannelIdentity, Org, User
from surogates.tenant.auth.database import DatabaseAuthProvider
from surogates.tenant.auth.middleware import get_current_tenant
from surogates.tenant.context import TenantContext
from surogates.tenant.models import OrgCreate, OrgResponse, UserCreate, UserResponse

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class OrgListResponse(BaseModel):
    orgs: list[OrgResponse]
    total: int


class UserListResponse(BaseModel):
    users: list[UserResponse]
    total: int


# ---------------------------------------------------------------------------
# Org CRUD
# ---------------------------------------------------------------------------


@router.post("/orgs", response_model=OrgResponse, status_code=status.HTTP_201_CREATED)
async def create_org(
    body: OrgCreate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> OrgResponse:
    """Create a new organisation."""
    if "admin" not in tenant.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required.",
        )

    session_factory = request.app.state.session_factory
    new_org = Org(
        id=uuid.uuid4(),
        name=body.name,
        config=body.config,
    )

    async with session_factory() as session:
        session.add(new_org)
        await session.commit()
        await session.refresh(new_org)

    return OrgResponse(
        id=new_org.id,
        name=new_org.name,
        config=new_org.config,
        created_at=new_org.created_at,
    )


@router.get("/orgs", response_model=OrgListResponse)
async def list_orgs(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    limit: int = 50,
    offset: int = 0,
) -> OrgListResponse:
    """List all organisations (admin only)."""
    if "admin" not in tenant.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required.",
        )

    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        count_result = await session.execute(select(func.count(Org.id)))
        total = count_result.scalar_one()

        result = await session.execute(
            select(Org).order_by(Org.created_at.desc()).limit(limit).offset(offset)
        )
        orgs = result.scalars().all()

    return OrgListResponse(
        orgs=[
            OrgResponse(
                id=org.id,
                name=org.name,
                config=org.config,
                created_at=org.created_at,
            )
            for org in orgs
        ],
        total=total,
    )


@router.get("/orgs/{org_id}", response_model=OrgResponse)
async def get_org(
    org_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> OrgResponse:
    """Retrieve a single organisation by ID."""
    if "admin" not in tenant.permissions and tenant.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required.",
        )

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        result = await session.execute(select(Org).where(Org.id == org_id))
        org = result.scalar_one_or_none()

    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Organisation {org_id} not found.",
        )

    return OrgResponse(
        id=org.id,
        name=org.name,
        config=org.config,
        created_at=org.created_at,
    )


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/orgs/{org_id}/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    org_id: UUID,
    body: UserCreate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> UserResponse:
    """Create a new user within an organisation."""
    if "admin" not in tenant.permissions and tenant.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required.",
        )

    session_factory = request.app.state.session_factory

    # Verify the org exists.
    async with session_factory() as session:
        org_result = await session.execute(select(Org).where(Org.id == org_id))
        if org_result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Organisation {org_id} not found.",
            )

    # Hash password if using database auth.
    password_hash: str | None = None
    if body.auth_provider == "database" and body.password:
        password_hash = DatabaseAuthProvider.hash_password(body.password)

    new_user = User(
        id=uuid.uuid4(),
        org_id=org_id,
        email=body.email,
        display_name=body.display_name,
        auth_provider=body.auth_provider,
        password_hash=password_hash,
    )

    async with session_factory() as session:
        session.add(new_user)
        await session.commit()
        await session.refresh(new_user)

    return UserResponse(
        id=new_user.id,
        org_id=new_user.org_id,
        email=new_user.email,
        display_name=new_user.display_name,
        auth_provider=new_user.auth_provider,
        created_at=new_user.created_at,
    )


@router.get("/orgs/{org_id}/users", response_model=UserListResponse)
async def list_users(
    org_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    limit: int = 50,
    offset: int = 0,
) -> UserListResponse:
    """List users within an organisation."""
    if "admin" not in tenant.permissions and tenant.org_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission required.",
        )

    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    if offset < 0:
        offset = 0

    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        count_result = await session.execute(
            select(func.count(User.id)).where(User.org_id == org_id)
        )
        total = count_result.scalar_one()

        result = await session.execute(
            select(User)
            .where(User.org_id == org_id)
            .order_by(User.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        users = result.scalars().all()

    return UserListResponse(
        users=[
            UserResponse(
                id=user.id,
                org_id=user.org_id,
                email=user.email,
                display_name=user.display_name,
                auth_provider=user.auth_provider,
                created_at=user.created_at,
            )
            for user in users
        ],
        total=total,
    )


# ---------------------------------------------------------------------------
# Channel Identity CRUD
# ---------------------------------------------------------------------------


class ChannelIdentityCreate(BaseModel):
    user_id: UUID
    platform: str
    platform_user_id: str
    platform_meta: dict = {}


class ChannelIdentityResponse(BaseModel):
    id: UUID
    user_id: UUID
    platform: str
    platform_user_id: str
    platform_meta: dict


@router.post(
    "/channel-identities",
    response_model=ChannelIdentityResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_channel_identity(
    body: ChannelIdentityCreate,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> ChannelIdentityResponse:
    """Register a platform user ID for a Surogates user."""
    session_factory = request.app.state.session_factory

    async with session_factory() as session:
        user = await session.get(User, body.user_id)
        if not user or user.org_id != tenant.org_id:
            raise HTTPException(status_code=404, detail="User not found in this org.")

        existing = await session.execute(
            select(ChannelIdentity)
            .where(ChannelIdentity.platform == body.platform)
            .where(ChannelIdentity.platform_user_id == body.platform_user_id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Platform identity {body.platform}:{body.platform_user_id} already registered.",
            )

        identity = ChannelIdentity(
            id=uuid.uuid4(),
            user_id=body.user_id,
            platform=body.platform,
            platform_user_id=body.platform_user_id,
            platform_meta=body.platform_meta,
        )
        session.add(identity)
        await session.commit()

    return ChannelIdentityResponse(
        id=identity.id,
        user_id=identity.user_id,
        platform=identity.platform,
        platform_user_id=identity.platform_user_id,
        platform_meta=identity.platform_meta,
    )


@router.get("/channel-identities", response_model=list[ChannelIdentityResponse])
async def list_channel_identities(
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
    platform: str | None = None,
) -> list[ChannelIdentityResponse]:
    """List channel identities for users in the tenant's org."""
    session_factory = request.app.state.session_factory

    async with session_factory() as session:
        query = (
            select(ChannelIdentity)
            .join(User, ChannelIdentity.user_id == User.id)
            .where(User.org_id == tenant.org_id)
        )
        if platform:
            query = query.where(ChannelIdentity.platform == platform)

        result = await session.execute(query)
        identities = result.scalars().all()

    return [
        ChannelIdentityResponse(
            id=i.id,
            user_id=i.user_id,
            platform=i.platform,
            platform_user_id=i.platform_user_id,
            platform_meta=i.platform_meta or {},
        )
        for i in identities
    ]


@router.delete(
    "/channel-identities/{identity_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_channel_identity(
    identity_id: UUID,
    request: Request,
    tenant: TenantContext = Depends(get_current_tenant),
) -> None:
    """Remove a channel identity."""
    session_factory = request.app.state.session_factory

    async with session_factory() as session:
        identity = await session.get(ChannelIdentity, identity_id)
        if not identity:
            raise HTTPException(status_code=404, detail="Channel identity not found.")

        user = await session.get(User, identity.user_id)
        if not user or user.org_id != tenant.org_id:
            raise HTTPException(status_code=404, detail="Channel identity not found.")

        await session.delete(identity)
        await session.commit()
