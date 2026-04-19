"""SQLAlchemy 2.x ORM models for the Surogates platform."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""

    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


# ---------------------------------------------------------------------------
# Orgs
# ---------------------------------------------------------------------------


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    users: Mapped[list[User]] = relationship(back_populates="org", lazy="raise")
    sessions: Mapped[list[Session]] = relationship(
        back_populates="org", lazy="raise"
    )
    credentials: Mapped[list[Credential]] = relationship(
        back_populates="org", lazy="raise"
    )
    skills: Mapped[list[Skill]] = relationship(back_populates="org", lazy="raise")
    mcp_servers: Mapped[list[McpServer]] = relationship(
        back_populates="org", lazy="raise"
    )
    service_accounts: Mapped[list[ServiceAccount]] = relationship(
        back_populates="org", lazy="raise"
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    auth_provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="database"
    )
    external_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    memory_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    org: Mapped[Org] = relationship(back_populates="users", lazy="raise")
    channel_identities: Mapped[list[ChannelIdentity]] = relationship(
        back_populates="user", lazy="raise"
    )
    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", lazy="raise"
    )
    credentials: Mapped[list[Credential]] = relationship(
        back_populates="user", lazy="raise"
    )
    skills: Mapped[list[Skill]] = relationship(back_populates="user", lazy="raise")
    mcp_servers: Mapped[list[McpServer]] = relationship(
        back_populates="user", lazy="raise"
    )


# ---------------------------------------------------------------------------
# Channel Identities
# ---------------------------------------------------------------------------


class ChannelIdentity(Base):
    __tablename__ = "channel_identities"
    __table_args__ = (
        UniqueConstraint("platform", "platform_user_id", name="uq_channel_platform"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    platform_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    platform_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Relationships
    user: Mapped[User] = relationship(
        back_populates="channel_identities", lazy="raise"
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_user", "user_id"),
        Index("idx_sessions_org", "org_id"),
        Index("idx_sessions_agent", "agent_id"),
        Index("idx_sessions_service_account", "service_account_id"),
        # Partial unique index: each (org, idempotency_key) is unique when
        # the key is present.  Supports fire-and-forget `POST /v1/api/prompts`
        # retries by making a duplicate insert fail fast with IntegrityError.
        Index(
            "uq_sessions_idempotency",
            "org_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Nullable: API-channel sessions are owned by a service account, not a user.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_accounts.id"),
        nullable=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="web"
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active"
    )
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    tool_call_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    input_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user: Mapped[Optional[User]] = relationship(
        back_populates="sessions", lazy="raise"
    )
    org: Mapped[Org] = relationship(back_populates="sessions", lazy="raise")
    service_account: Mapped[Optional[ServiceAccount]] = relationship(
        back_populates="sessions", lazy="raise"
    )
    parent: Mapped[Optional[Session]] = relationship(
        remote_side=[id], lazy="raise"
    )
    events: Mapped[list[Event]] = relationship(
        back_populates="session", lazy="raise"
    )
    lease: Mapped[Optional[SessionLease]] = relationship(
        back_populates="session", lazy="raise", uselist=False
    )
    cursor: Mapped[Optional[SessionCursor]] = relationship(
        back_populates="session", lazy="raise", uselist=False
    )
    delivery_outbox_items: Mapped[list[DeliveryOutbox]] = relationship(
        back_populates="session", lazy="raise"
    )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class Event(Base):
    """Append-only event log row.

    ``org_id`` and ``user_id`` are denormalized from the owning session so
    audit queries can filter by tenant without joining ``sessions``.  They
    are populated automatically by the ``events_populate_tenant`` trigger
    (see ``surogates/db/observability.sql``) when a row is inserted with
    these columns NULL — callers never need to set them explicitly.
    """

    __tablename__ = "events"
    __table_args__ = (
        Index("idx_events_session", "session_id", "id"),
        Index("idx_events_trace", "trace_id"),
        # Cross-session audit: tenant + type + time.
        Index(
            "idx_events_audit_type_time",
            "org_id", "type", "created_at",
            postgresql_using="btree",
        ),
        # Per-user activity: "top tools for user X in last week".
        Index(
            "idx_events_audit_user_time",
            "org_id", "user_id", "type", "created_at",
            postgresql_using="btree",
        ),
        # Session-internal filtering by type (e.g. all tool.call in session).
        Index("idx_events_session_type", "session_id", "type"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    # Denormalized from sessions for cheap tenant-scoped audit queries.
    # Populated by trigger on insert; nullable so trigger can do the work.
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=True)
    trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    span_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    session: Mapped[Session] = relationship(back_populates="events", lazy="raise")


# ---------------------------------------------------------------------------
# Session Leases
# ---------------------------------------------------------------------------


class SessionLease(Base):
    __tablename__ = "session_leases"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), primary_key=True
    )
    owner_id: Mapped[str] = mapped_column(Text, nullable=False)
    lease_token: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    session: Mapped[Session] = relationship(back_populates="lease", lazy="raise")


# ---------------------------------------------------------------------------
# Session Cursors
# ---------------------------------------------------------------------------


class SessionCursor(Base):
    __tablename__ = "session_cursors"

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), primary_key=True
    )
    harness_cursor: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    session: Mapped[Session] = relationship(back_populates="cursor", lazy="raise")


# ---------------------------------------------------------------------------
# Delivery Outbox
# ---------------------------------------------------------------------------


class DeliveryOutbox(Base):
    __tablename__ = "delivery_outbox"
    __table_args__ = (
        UniqueConstraint("channel", "dedupe_key", name="uq_delivery_outbox_dedupe"),
        Index(
            "idx_delivery_outbox_pending",
            "status",
            "available_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    destination: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=True)
    dedupe_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    available_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    session: Mapped[Session] = relationship(
        back_populates="delivery_outbox_items", lazy="raise"
    )
    event: Mapped[Event] = relationship(lazy="raise")


# ---------------------------------------------------------------------------
# Delivery Cursors
# ---------------------------------------------------------------------------


class DeliveryCursor(Base):
    __tablename__ = "delivery_cursors"

    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    destination_key: Mapped[str] = mapped_column(Text, primary_key=True)
    last_outbox_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Service Accounts
# ---------------------------------------------------------------------------


class ServiceAccount(Base):
    """Org-scoped API key for programmatic access.

    Synthetic data pipelines and other non-interactive clients authenticate
    with a bearer token issued from this table.  The raw token is returned
    once on creation and only a SHA-256 hash is persisted.
    """

    __tablename__ = "service_accounts"
    __table_args__ = (
        Index("idx_service_accounts_org", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # SHA-256 hex digest of the raw token.  Unique so token lookups are
    # constant-time regardless of how many service accounts exist.
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # First characters of the token for display (e.g. ``surg_sk_abcd…``).
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    # Relationships
    org: Mapped[Org] = relationship(
        back_populates="service_accounts", lazy="raise"
    )
    sessions: Mapped[list[Session]] = relationship(
        back_populates="service_account", lazy="raise"
    )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    org: Mapped[Org] = relationship(back_populates="credentials", lazy="raise")
    user: Mapped[Optional[User]] = relationship(
        back_populates="credentials", lazy="raise"
    )


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class Skill(Base):
    """Skill definition -- prompt-based (type='skill') or SLM-backed (type='expert').

    Expert skills delegate to a fine-tuned small language model via the
    ``consult_expert`` tool.  The ``expert_*`` columns are only meaningful
    when ``type == 'expert'``.
    """

    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="skill"
    )
    content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Expert-specific columns (NULL for regular skills).
    expert_model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expert_endpoint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expert_adapter: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expert_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    expert_status: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, server_default="draft"
    )
    expert_stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    # Relationships
    org: Mapped[Org] = relationship(back_populates="skills", lazy="raise")
    user: Mapped[Optional[User]] = relationship(
        back_populates="skills", lazy="raise"
    )


# ---------------------------------------------------------------------------
# MCP Servers
# ---------------------------------------------------------------------------


class AuditLog(Base):
    """Tenant-scoped audit log for events that are not bound to a session.

    Complements the ``events`` table (which is session-scoped and carries
    the full conversation trail).  ``audit_log`` holds decisions that
    happen outside the session lifecycle: authentication outcomes, MCP
    tool safety scans, credential vault accesses.

    External audit and compliance tooling queries both tables directly;
    the UI typically joins them on ``org_id`` / ``user_id`` to build a
    complete activity timeline.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index(
            "idx_audit_log_org_type_time",
            "org_id", "type", "created_at",
            postgresql_using="btree",
        ),
        Index(
            "idx_audit_log_type_time",
            "type", "created_at",
            postgresql_using="btree",
        ),
        Index("idx_audit_log_user_time", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    # Nullable: some audit events are org-scoped without a specific user
    # (e.g. MCP tool scan on platform-wide server definitions).
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=True)
    trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    span_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )


class McpServer(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        # Partial unique indexes enforce at-most-one registration per
        # (org, user, name) scope.  Split in two because PostgreSQL
        # treats NULLs as distinct in a standard UNIQUE constraint
        # (prior to "UNIQUE NULLS NOT DISTINCT" in PG 15+).
        Index(
            "uq_mcp_servers_org_name",
            "org_id", "name",
            unique=True,
            postgresql_where=text("user_id IS NULL"),
        ),
        Index(
            "uq_mcp_servers_org_user_name",
            "org_id", "user_id", "name",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="stdio"
    )
    command: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    args: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    env: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    credential_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    timeout: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="120"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    org: Mapped[Org] = relationship(back_populates="mcp_servers", lazy="raise")
    user: Mapped[Optional[User]] = relationship(
        back_populates="mcp_servers", lazy="raise"
    )
