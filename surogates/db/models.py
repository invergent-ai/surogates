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
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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
    agents: Mapped[list[Agent]] = relationship(back_populates="org", lazy="raise")
    mcp_servers: Mapped[list[McpServer]] = relationship(
        back_populates="org", lazy="raise"
    )
    service_accounts: Mapped[list[ServiceAccount]] = relationship(
        back_populates="org", lazy="raise"
    )
    website_agents: Mapped[list[WebsiteAgent]] = relationship(
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
    agents: Mapped[list[Agent]] = relationship(back_populates="user", lazy="raise")
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
# Sub-agent types
# ---------------------------------------------------------------------------


class Agent(Base):
    """Declarative sub-agent type -- preset bundle of system prompt, tool
    filter, model, iteration cap, and governance policy profile.

    A sub-agent type is referenced by name when the coordinator spawns
    a child session (``session.config.agent_type = <name>``).  The child
    harness resolves the :class:`Agent` row at wake-time and applies the
    preset to its loop.  Sub-agents inherit the parent's skills, MCP
    servers, experts, tenant memory, and workspace; this table only
    carries the per-type presets.

    ``config`` JSONB holds:
        - ``tools`` (list[str] | None)          -- allowlist
        - ``disallowed_tools`` (list[str] | None) -- denylist
        - ``model`` (str | None)                -- model override
        - ``max_iterations`` (int | None)       -- iteration cap
        - ``policy_profile`` (str | None)       -- governance profile name
        - ``category`` (str | None)             -- subdirectory grouping
        - ``tags`` (list[str] | None)           -- metadata tags
    """

    __tablename__ = "agents"

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
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    org: Mapped[Org] = relationship(back_populates="agents", lazy="raise")
    user: Mapped[Optional[User]] = relationship(
        back_populates="agents", lazy="raise"
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


# ---------------------------------------------------------------------------
# Website agents -- public-website channel configuration
# ---------------------------------------------------------------------------


class WebsiteAgent(Base):
    """Configuration for a public-website embed of the agent.

    Each row corresponds to one named agent an org admin has provisioned
    for embedding on a public website (e.g. a support bot).  The row
    carries the CORS allow-list, the publishable key (stored hashed) the
    embed presents on bootstrap, the tool allow-list the anonymous
    visitor may invoke, and per-session caps.  Visitors are anonymous —
    no :class:`User` row exists for them; identity is the server-side
    session cookie alone.

    The publishable key is safe to ship to the browser: its authority is
    only recognised together with an ``Origin`` header in
    :attr:`allowed_origins`.  A stolen key used from a different origin
    is rejected.
    """

    __tablename__ = "website_agents"
    __table_args__ = (
        Index("idx_website_agents_org", "org_id"),
        # Unique publishable-key hash so token lookups are O(1) and every
        # key globally identifies exactly one agent row.
        Index(
            "uq_website_agents_publishable_key",
            "publishable_key_hash",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # SHA-256 hex digest of the raw publishable key.  Raw key never hits
    # the database; the admin route returns it once on creation.
    publishable_key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Display-safe prefix (e.g. "surg_wk_abcd…") for UIs.
    publishable_key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    # Exact-match origin allow-list (scheme + host + port, no wildcards).
    # Browser-origin validation uses this list on both CORS preflight and
    # on every message/events request; the cookie JWT also binds one
    # origin on issue so a stolen cookie cannot be reused cross-origin.
    allowed_origins: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # Subset of registered tools this agent's visitors may invoke.  A
    # non-empty list enforces strict membership -- any tool outside it
    # is rejected by ``execute_single_tool`` in ``harness/tool_exec.py``
    # before dispatch, with a ``policy.denied`` event and an error tool
    # result.  An empty list means "no per-session restriction" and
    # falls back to the platform-wide governance rules; ops should set
    # an explicit list for every website agent, because the default
    # platform rules are calibrated for authenticated users, not
    # anonymous website visitors.
    tool_allow_list: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # Body prepended to the harness system prompt for visitor sessions.
    # Populated at session bootstrap onto ``session.config.system_prompt``
    # so the harness doesn't need a live DB lookup on every wake.
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Names of skills to pin into every session started from this agent.
    skill_pins: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # Per-session caps; the worker enforces these before enqueuing more
    # work on the session.  0 means "no cap".
    session_message_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    session_token_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    # Idle minutes before the session is auto-reset; kept distinct from
    # the platform default so high-traffic embeds can tune retention.
    session_idle_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="30"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    org: Mapped[Org] = relationship(
        back_populates="website_agents", lazy="raise"
    )


# ---------------------------------------------------------------------------
# Knowledge Bases
#
# Two-layer model:
#   - ``kb_raw_doc``: immutable, source of truth, ingested from a ``kb_source``.
#   - ``kb_wiki_entry``: LLM-authored, denoised, cross-referenced wiki layer
#     (one or more raw docs per entry). Chunked into ``kb_chunk`` for hybrid
#     retrieval (BM25 via tsvector + vector via pgvector HNSW).
#
# Storage:
#   - Postgres: metadata, chunks, embeddings, RLS policies (see ``kb.sql``).
#   - Garage S3: raw bytes + wiki entry markdown at
#     ``tenant-{org_id}/shared/knowledge_bases/{kb_name}/...`` for tenant KBs,
#     ``platform-shared/knowledge_bases/{kb_name}/...`` for platform KBs
#     (where ``kb.org_id IS NULL``).
#
# Tenancy is enforced three ways: (1) tools derive ``org_id`` from kwargs,
# never from a tool argument; (2) every query joins ``kb`` and filters
# ``kb.org_id``; (3) Postgres row-level security on every KB table tied to
# the ``app.org_id`` session GUC (set in ``kb.sql``).
#
# DDL that ``Base.metadata.create_all`` cannot express (GENERATED tsvector
# column, HNSW + GIN indexes, RLS policies, pgvector extension) lives in
# ``surogates/db/kb.sql`` and is applied by ``apply_kb_ddl`` in
# ``surogates/db/engine.py``.
# ---------------------------------------------------------------------------


class KnowledgeBase(Base):
    """A named knowledge base owned by an org, or a platform-shared KB
    (``org_id IS NULL`` and ``is_platform = True``).

    Platform KBs live in a single ``platform-shared/`` Garage bucket and
    are implicitly granted to every agent in every org. Org KBs live in
    the org's tenant bucket and require an explicit ``agent_kb_grant`` row
    for an agent to use them via ``kb_search``.
    """

    __tablename__ = "kb"
    __table_args__ = (
        # Per-org name uniqueness (org KBs).
        Index(
            "uq_kb_org_name",
            "org_id", "name",
            unique=True,
            postgresql_where=text("org_id IS NOT NULL"),
        ),
        # Platform-wide name uniqueness (platform KBs).
        Index(
            "uq_kb_platform_name",
            "name",
            unique=True,
            postgresql_where=text("org_id IS NULL"),
        ),
        Index("idx_kb_org", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # NULL for platform KBs (visible to all orgs); UUID for org-owned KBs.
    org_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Per-KB conventions document, used as the wiki-maintainer's system prompt
    # and surfaced in the management UI as an editable doc.
    agents_md: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    embedding_model: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="mxbai-embed-large"
    )
    # Locked at create-time. Switching ``embedding_model`` to one with a
    # different dim requires a re-index job (sets ``status = 'reindexing'``).
    embedding_dim: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1024"
    )
    is_platform: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    # 'active' | 'reindexing'.
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active"
    )
    # Watermark: max ``kb_raw_doc.ingested_at`` the maintainer has compiled.
    # Maintainer reads only rows ingested at or before this watermark on the
    # next run, then advances it. Lets ingest run concurrently with compile.
    last_compiled_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # Daily LLM budget (USD) for the wiki maintainer on this KB. 0 = unlimited.
    maintenance_budget_usd_per_day: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships (one-way; we don't expose ``org.kbs`` to keep the diff
    # to the existing ``Org`` model nil).
    org: Mapped[Optional[Org]] = relationship(lazy="raise")
    sources: Mapped[list[KbSource]] = relationship(
        back_populates="kb", lazy="raise", cascade="all, delete-orphan"
    )
    raw_docs: Mapped[list[KbRawDoc]] = relationship(
        back_populates="kb", lazy="raise", cascade="all, delete-orphan"
    )
    wiki_entries: Mapped[list[KbWikiEntry]] = relationship(
        back_populates="kb", lazy="raise", cascade="all, delete-orphan"
    )


class KbSource(Base):
    """An ingestion source feeding a ``KnowledgeBase``.

    The ``kind`` discriminator selects the runner module under
    ``surogates.jobs.kb_sources`` (``markdown_dir``, ``web_scraper``,
    ``pdf``, ...). The ``config`` JSONB holds kind-specific parameters
    (URL, glob patterns, credentials reference, etc.).

    Tombstone-on-delete: setting ``deleted_at`` stops further ingest;
    associated raw docs remain so the wiki maintainer can rewrite affected
    entries. Hard purge is a separate admin operation.
    """

    __tablename__ = "kb_source"
    __table_args__ = (
        Index("idx_kb_source_kb", "kb_id"),
        # Backing index for the cron scheduler scan.
        Index(
            "idx_kb_source_schedule",
            "schedule",
            postgresql_where=text(
                "schedule IS NOT NULL AND deleted_at IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kb.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'markdown_dir' | 'web_scraper' | 'github' | 'pdf' | 'notion' | 'slack' | ...
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    # Cron expression or NULL (manual-trigger only).
    schedule: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # 'success' | 'failed' | 'partial' | 'running' | NULL (never synced).
    last_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    kb: Mapped[KnowledgeBase] = relationship(
        back_populates="sources", lazy="raise"
    )


class KbRawDoc(Base):
    """A raw document ingested from a ``KbSource`` -- the immutable source
    of truth. Bytes live in Garage at
    ``{bucket}/shared/knowledge_bases/{kb_name}/raw/{id}.md`` (or the
    platform bucket equivalent for platform KBs).

    Hashed on ingest; ingest is idempotent on ``content_sha`` so re-running
    a sync only re-embeds rows that actually changed.
    """

    __tablename__ = "kb_raw_doc"
    __table_args__ = (
        Index("idx_kb_raw_doc_kb", "kb_id"),
        Index("idx_kb_raw_doc_source", "source_id"),
        Index("idx_kb_raw_doc_kb_ingested", "kb_id", "ingested_at"),
        UniqueConstraint("kb_id", "path", name="uq_kb_raw_doc_kb_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kb.id", ondelete="CASCADE"),
        nullable=False,
    )
    # NULL when the source was deleted but the raw doc kept (orphaned).
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kb_source.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Source-relative path, e.g. "docs/sub-agents.md" or
    # "https://docs.bigconnect.io/concepts/security".
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha: Mapped[str] = mapped_column(Text, nullable=False)
    # Live URL for the raw doc, when applicable. Lets the agent surface a
    # back-pointer to the original location in citations.
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )

    # Relationships
    kb: Mapped[KnowledgeBase] = relationship(
        back_populates="raw_docs", lazy="raise"
    )


class KbWikiEntry(Base):
    """An LLM-authored wiki entry compiled from one or more raw docs.

    Bytes live in Garage at
    ``{bucket}/shared/knowledge_bases/{kb_name}/{path}``, e.g.
    ``wiki/summaries/sub-agents.md`` or ``wiki/concepts/sandbox.md``.

    Chunked into ``kb_chunk`` rows for hybrid retrieval. The maintainer
    rewrites entries by atomic ``.tmp``-then-rename in Garage and updates
    ``content_sha`` last (the truth-marker on crash recovery).
    """

    __tablename__ = "kb_wiki_entry"
    __table_args__ = (
        Index("idx_kb_wiki_entry_kb", "kb_id"),
        Index("idx_kb_wiki_entry_kb_kind", "kb_id", "kind"),
        UniqueConstraint("kb_id", "path", name="uq_kb_wiki_entry_kb_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kb.id", ondelete="CASCADE"),
        nullable=False,
    )
    # KB-bucket-relative path, e.g. "wiki/summaries/sub-agents.md",
    # "wiki/concepts/sandbox.md", "index.md", "log.md".
    path: Mapped[str] = mapped_column(Text, nullable=False)
    # 'summary' | 'concept' | 'exploration' | 'index' | 'log'.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha: Mapped[str] = mapped_column(Text, nullable=False)
    # IDs of raw docs that contributed to this entry. Used by the maintainer
    # to know which entries to rewrite when their source raw docs change.
    sources: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default="{}"
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    kb: Mapped[KnowledgeBase] = relationship(
        back_populates="wiki_entries", lazy="raise"
    )
    chunks: Mapped[list[KbChunk]] = relationship(
        back_populates="entry", lazy="raise", cascade="all, delete-orphan"
    )


class KbChunk(Base):
    """A retrieval chunk over a ``KbWikiEntry``.

    Hybrid index targets:
      - ``tsv``: tsvector GENERATED column (added in ``kb.sql``; not declared
        here because SQLAlchemy DDL for PG GENERATED columns is dialect-
        specific and awkward). GIN-indexed for BM25-ish lexical search.
      - ``embedding``: pgvector cosine-similarity vector. HNSW-indexed
        (m=16, ef_construction=64) in ``kb.sql``.

    ``kb_search`` runs both, merges via reciprocal-rank-fusion (per-KB
    top_k normalised so one big KB doesn't drown others).
    """

    __tablename__ = "kb_chunk"
    __table_args__ = (
        UniqueConstraint(
            "wiki_entry_id", "chunk_index", name="uq_kb_chunk_entry_idx"
        ),
        Index("idx_kb_chunk_entry", "wiki_entry_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    wiki_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kb_wiki_entry.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Heading breadcrumb, e.g. "Sub-Agents > What is a Sub-Agent?". Aids
    # ranking and citation.
    heading_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Vector(1024) — fixed at platform default in MVP. Per-KB override in
    # ``KnowledgeBase.embedding_dim`` is documented but not yet supported
    # for retrieval (HNSW index dim is fixed at table-create time).
    # Nullable so a chunk can exist mid-reindex before its embedding is
    # populated; HNSW handles NULLs by skipping.
    embedding = mapped_column(Vector(1024), nullable=True)

    # Relationships
    entry: Mapped[KbWikiEntry] = relationship(
        back_populates="chunks", lazy="raise"
    )


class AgentKbGrant(Base):
    """An agent's grant to read a specific (org-owned) ``KnowledgeBase``.

    Default-deny: an agent without a grant row for an org KB cannot see
    it via ``kb_search``. Platform KBs (``kb.is_platform = True``) are
    implicitly granted to all agents and need no row here.
    """

    __tablename__ = "agent_kb_grant"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("kb.id", ondelete="CASCADE"),
        primary_key=True,
    )
    granted_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
