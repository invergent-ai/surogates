"""Pydantic models for tenant-related API request / response payloads.

These are *not* SQLAlchemy ORM models -- they are pure Pydantic v2 schemas
used by the FastAPI route layer for validation and serialisation.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

__all__ = [
    "OrgCreate",
    "OrgResponse",
    "UserCreate",
    "UserResponse",
    "ChannelIdentityCreate",
]


# ---------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------


class OrgCreate(BaseModel):
    """Payload for creating a new organisation."""

    name: str = Field(..., min_length=1, max_length=256)
    config: dict = Field(default_factory=dict)


class OrgResponse(BaseModel):
    """Serialised organisation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    config: dict
    created_at: datetime


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserCreate(BaseModel):
    """Payload for creating a new user within an organisation."""

    email: EmailStr
    display_name: str = Field(..., min_length=1, max_length=256)
    password: str | None = None
    auth_provider: str = "database"
    # Opt-in override for the Firebase-collision guard. When False
    # (default), admin attempts to create a user whose email is already
    # linked to a BYO Firebase user are rejected with 409. Setting
    # ``force=True`` deliberately creates a duplicate row so an admin
    # can re-issue local credentials to a self-registered user (e.g.
    # after the project's Firebase config is removed).
    force: bool = False


class UserResponse(BaseModel):
    """Serialised user returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    org_id: UUID
    email: str | None = None
    display_name: str | None = None
    auth_provider: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Channel identities
# ---------------------------------------------------------------------------


class ChannelIdentityCreate(BaseModel):
    """Payload for linking a channel-specific identity to a user."""

    platform: str = Field(..., min_length=1, max_length=64)
    platform_user_id: str = Field(..., min_length=1, max_length=512)
    platform_meta: dict = Field(default_factory=dict)
