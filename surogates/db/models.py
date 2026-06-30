"""SQLAlchemy 2.x ORM models for the Surogates platform."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    Sequence,
    String,
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
from sqlalchemy.types import TypeDecorator


class GUID(TypeDecorator):
    """Platform-independent UUID column.

    Native ``UUID`` on Postgres (prod); ``String(36)`` with ``uuid.UUID``
    coercion on SQLite so a model using it is unit-testable in isolation
    without a Postgres engine.  Postgres DDL/behaviour is unchanged.
    """

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


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

    # Partial unique index — keeps Firebase-linked users uniquely
    # identifiable by (org, auth_provider, external_id) while leaving
    # database users (where external_id is NULL) unconstrained. Combined
    # with the project-scoped ``auth_provider = "firebase:{project_id}"``
    # namespacing, this is the only structural guard against
    # cross-BYO-project UID collisions.
    __table_args__ = (
        Index(
            "uq_users_org_auth_external",
            "org_id",
            "auth_provider",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )


# ---------------------------------------------------------------------------
# Channel Identities
# ---------------------------------------------------------------------------


class ChannelIdentity(Base):
    __tablename__ = "channel_identities"
    # Scoped per-org: the same platform user id (e.g. a Slack workspace member)
    # may legitimately be known to agents in different orgs, and each must
    # resolve to its OWN org-scoped user — a global (platform, platform_user_id)
    # uniqueness would attribute one org's session to another org's user.
    __table_args__ = (
        UniqueConstraint(
            "org_id", "platform", "platform_user_id", name="uq_channel_org_platform"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
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
    # Subagent task layer: when set, this session is one execution attempt
    # of the referenced Task. Nullable because plain chat / spawn_worker
    # sessions are not backed by a Task.
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
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
# Inbox Items
# ---------------------------------------------------------------------------


class InboxItem(Base):
    """A raised-hand moment for the user, mirrored from a session event."""

    __tablename__ = "inbox_items"
    __table_args__ = (
        Index(
            "idx_inbox_user_status_created",
            "user_id",
            "status",
            "created_at",
            postgresql_using="btree",
        ),
        Index(
            "idx_inbox_sa_status_created",
            "service_account_id",
            "status",
            "created_at",
            postgresql_using="btree",
        ),
        Index("idx_inbox_org_created", "org_id", "created_at"),
        Index("idx_inbox_session", "session_id"),
        CheckConstraint(
            "(user_id IS NOT NULL)::int + (service_account_id IS NOT NULL)::int = 1",
            name="ck_inbox_items_one_principal",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    source_event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("events.id"), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", server_default="pending"
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    action_ref: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
# Scheduled Sessions
# ---------------------------------------------------------------------------


class ScheduledSession(Base):
    """A persisted /loop or scheduled-prompt definition.

    A schedule is owned by exactly one principal: a human user
    (``user_id`` set) or a service account (``service_account_id`` set).
    Anonymous-channel sessions cannot create schedules — the session
    itself is the principal there and would not outlive a recurring
    loop. The DB CHECK constraint enforces the XOR; the application
    layer rejects ahead of insert so callers see a clean error instead
    of an ``IntegrityError``.
    """

    __tablename__ = "scheduled_sessions"
    __table_args__ = (
        Index(
            "idx_scheduled_sessions_principal",
            "org_id", "user_id", "service_account_id", "agent_id",
        ),
        Index(
            "idx_scheduled_sessions_due",
            "agent_id",
            "status",
            "next_run_at",
            postgresql_where=text("status = 'active'"),
        ),
        Index("idx_scheduled_sessions_lock", "locked_until"),
        CheckConstraint(
            "(user_id IS NOT NULL)::int + (service_account_id IS NOT NULL)::int = 1",
            name="ck_scheduled_sessions_one_principal",
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
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    schedule_display: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="UTC")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="tool")
    repeat_limit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    locked_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_from_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Ambient schedules (Surogate Mate)
# ---------------------------------------------------------------------------


class AmbientScheduleRow(Base):
    """One ambient-review schedule per followed channel.

    Deliberately NOT reusing ``scheduled_sessions``: that table requires a
    ``user_id XOR service_account_id`` principal, which a system-owned,
    userless channel schedule has no natural value for.  This table reuses
    the platform-ticker's claim/lock pattern (``locked_by`` / ``locked_until``
    + ``SELECT FOR UPDATE SKIP LOCKED``) without the principal constraint.
    """

    __tablename__ = "ambient_schedules"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "platform", "channel_id",
            name="uq_ambient_schedules_channel",
        ),
        Index(
            "idx_ambient_schedules_due",
            "status", "next_run_at",
            postgresql_where=text("status = 'active'"),
        ),
        Index("idx_ambient_schedules_lock", "locked_until"),
    )

    # Portable Postgres types: UUID/JSONB on Postgres (prod), String/JSON on
    # SQLite so this table is unit-testable in isolation.  The Postgres DDL is
    # unchanged.
    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The shared channel session this ambient schedule was created from
    # (provenance + the slack_channel_id/team_id source for the ambient
    # session config).
    source_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True,
    )
    # The dedicated ambient session reused across ticks (created lazily by
    # the materializer; channel="ambient").
    ambient_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True,
    )
    cadence_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1800"
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active"
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(), onupdate=func.now(),
    )


# ---------------------------------------------------------------------------
# Browser Profiles
# ---------------------------------------------------------------------------


class BrowserProfile(Base):
    """A reusable, encrypted browser login state owned by one principal.

    Like ``ScheduledSession``, a profile is owned by exactly one principal —
    a human ``user_id`` or a ``service_account_id`` (the ops-chat SA that
    work-chat requests authenticate as). The CHECK enforces the XOR.
    """

    __tablename__ = "browser_profiles"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL)::int + (service_account_id IS NOT NULL)::int = 1",
            name="ck_browser_profiles_one_principal",
        ),
        Index(
            "uq_browser_profiles_user_name",
            "org_id",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_browser_profiles_sa_name",
            "org_id",
            "service_account_id",
            "name",
            unique=True,
            postgresql_where=text("service_account_id IS NOT NULL"),
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
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'manual_vnc'")
    )
    storage_state_enc: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    cookie_domains: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


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
        Index(
            "uq_service_accounts_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("agent_id IS NOT NULL"),
        ),
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
    # Logical reference to the ops ``Agent.id`` (a different database, so no
    # FK).  Set when this service account is the agent's own principal; NULL for
    # ordinary org-scoped service accounts.  The partial unique index above
    # keeps it one-SA-per-agent.
    agent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
    __table_args__ = (
        # NULLS NOT DISTINCT (PG 15+) makes org-scoped rows (user_id IS NULL)
        # collide with each other, which the upsert in CredentialVault.store
        # relies on to be race-safe.
        UniqueConstraint(
            "org_id",
            "user_id",
            "name",
            name="uq_credentials_org_user_name",
            postgresql_nulls_not_distinct=True,
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
    """Skill definition -- prompt-based (type='skill') or model-backed (type='expert').

    Expert skills configure task-specialized reasoning models. The
    harness can route matching hard tasks using the skill trigger, and
    the base LLM can delegate explicitly via ``consult_expert``. The
    ``expert_*`` columns are only meaningful when ``type == 'expert'``.
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
        # per-tenant audit queries.  Shared-runtime
        # dashboards filter by (agent_id, created_at); without this index
        # the query degrades to a seq-scan on the org-wide partition once
        # the tenant has > a few thousand rows.
        Index("idx_audit_log_agent_time", "agent_id", "created_at"),
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
    # Nullable: rare audit events with no per-tenant context
    # (e.g. platform copilot writes) pass None.
    agent_id: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
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
# Subagent task layer
#
# A Task is a durable, DAG-aware coordination unit that wraps zero or more
# Session attempts. Created by the ``spawn_task`` tool; promoted through
# todo -> ready -> running -> done/failed/blocked/cancelled by the
# ``tasks_tick`` dispatcher loop (see ``surogates/tasks/dispatcher.py``).
#
# ``task_links`` carries the parent->child DAG edges separately so a single
# child can depend on multiple parents (fan-in synthesis pattern).
# ---------------------------------------------------------------------------


class Task(Base):
    """Durable subagent task — coordinates retries, DAG dependencies, and
    block/unblock lifecycle around a goal that may be executed by zero or
    more Session attempts.

    Status state machine (see ``surogates/tasks/dispatcher.py``):

    * ``todo``      — created; one or more parents not yet done
    * ``ready``     — parents complete; eligible for atomic claim
    * ``running``   — a Session (``current_session_id``) is executing
    * ``blocked``   — worker called ``worker_block``; awaiting unblock
    * ``done``      — worker session ended with WORKER_COMPLETE
    * ``failed``    — exhausted ``max_attempts`` after crash/timeout
    * ``cancelled`` — parent or operator aborted before terminal state

    ``cancelled`` and ``failed`` parents intentionally do **not** unblock
    children — downstream tasks stay in ``todo`` until the orchestrator
    cancels or replans them explicitly.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_org_status", "org_id", "status"),
        Index("idx_tasks_parent_session", "parent_session_id"),
        Index("idx_tasks_current_session", "current_session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    # The Session that called spawn_task. Used for ownership checks on
    # unblock_task / cancel_task and as the target of completion events.
    parent_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    # Optional pre-configured sub-agent type (matches an AgentDef name).
    # Resolved at spawn time via ``surogates.harness.agent_resolver``.
    agent_def_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    # Free-form context the parent provided at spawn time. Appended to
    # (not replaced) on each ``unblock_task`` call with a timestamp marker
    # so subsequent attempts see all accumulated context.
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Points at the in-flight Session attempt. Cleared only when
    # transitioning back to ``ready`` (retry) — terminal states keep it
    # set for history.
    current_session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="todo",
    )
    # Worker's final summary; populated from WORKER_COMPLETE event payload
    # (auto-extracted on natural session end), or set explicitly by the
    # ``worker_complete`` self-tool when a worker wants a structured handoff.
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Structured handoff metadata — set only when the worker called
    # ``worker_complete(summary, metadata=...)`` explicitly. Shape is a
    # free-form JSON object (e.g. ``{"changed_files": [...],
    # "tests_run": 12, "decisions": [...]}``).  Plain workers that
    # complete naturally never populate this field.
    result_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSONB, nullable=True,
    )
    # One-sentence reason captured by the ``worker_block`` tool.
    blocked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Number of Sessions that have been claimed for this task (including
    # in-flight). ``worker_block`` deliberately does not increment this —
    # blocking is a pause, not a failure.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="3"
    )
    # Mission layer: when set, this task belongs to a mission (the
    # coordinator session is the mission's session). Stamped at spawn
    # time by ``_spawn_task_handler`` reading
    # ``session.config["active_mission_id"]``. Nullable so non-mission
    # tasks (plain spawn_task, or spawn_task from a session without an
    # active mission) carry None.
    mission_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class TaskLink(Base):
    """Parent -> child DAG edge between Tasks. Supports fan-in: a child
    may have multiple parents and stays in ``todo`` until every parent
    reaches ``done`` (cancelled/failed parents do not unblock children).
    """

    __tablename__ = "task_links"

    parent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True,
    )
    child_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), primary_key=True,
    )


# ---------------------------------------------------------------------------
# Coordination board
# ---------------------------------------------------------------------------


# Monotonic change counter for board notes.  Bumped on INSERT (column
# default) AND on every status transition (supersede / expire / claim
# renewal) so a single ``seq`` cursor covers both new notes and state
# changes.  Created by ``Base.metadata.create_all`` because it is bound
# to the column below.
board_note_seq = Sequence("board_note_seq")


class BoardNote(Base):
    """One verified note on a coordination-group board.

    The board is the horizontal communication substrate for a fan-out
    tree (spec: docs/superpowers/specs/2026-06-11-coordination-board-design.md).
    ``group_id`` is a plain UUID with NO foreign key: in v1 it holds the
    fan-out root session id; the mission integration phase will reuse the
    same column for mission ids.

    Rows are only ever written for notes that passed admission
    (deterministic pre-checks + LLM verification).  Rejected notes are
    tool-result feedback, never rows.

    Status machine:

    * ``active``     — visible in renders
    * ``superseded`` — a newer RESULT from the same writer replaced it
    * ``expired``    — CLAIM whose TTL lapsed
    """

    __tablename__ = "board_notes"
    __table_args__ = (
        Index("idx_board_notes_group_seq", "group_id", "seq"),
        Index("idx_board_notes_group_status", "group_id", "status"),
        Index("idx_board_notes_org", "org_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    seq: Mapped[int] = mapped_column(
        BigInteger,
        board_note_seq,
        server_default=board_note_seq.next_value(),
        nullable=False,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    writer_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    writer_label: Mapped[str] = mapped_column(String(16), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(String(400), nullable=False)
    ref: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active"
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Mission layer
#
# A Mission is a long-running, durable, multi-worker objective attached
# to a chat (coordinator) session. The mission's rubric is graded by an
# LLM judge fired on (a) mission-task terminal events, or (b) an
# explicit ``[[mission-complete]]`` marker in the coordinator's prose —
# never on every no-tool-call response (that's `/goal`'s rule and it's
# wrong for orchestrator workloads).
#
# See docs/superpowers/specs/2026-05-16-mission-orchestrated-goals-design.md.
# ---------------------------------------------------------------------------


class Mission(Base):
    """A durable orchestrated objective with rubric-judged completion.

    A mission is owned by exactly one principal: either a human user
    (``user_id`` set) or a service account (``service_account_id`` set).
    Anonymous-channel sessions cannot own missions — the session itself
    is the principal there and would not outlive the mission's life-cycle.
    The DB CHECK constraint enforces the XOR; the application layer
    rejects ahead of the insert so callers see a clean error instead of
    an ``IntegrityError``.
    """

    __tablename__ = "missions"
    __table_args__ = (
        Index("idx_missions_session", "session_id"),
        Index(
            "idx_missions_principal_agent_status",
            "org_id", "user_id", "service_account_id", "agent_id", "status",
        ),
        CheckConstraint(
            "(user_id IS NOT NULL)::int + (service_account_id IS NOT NULL)::int = 1",
            name="ck_missions_one_principal",
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
    service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id"), nullable=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    rubric: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active",
    )
    iteration: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    max_iterations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="20"
    )
    last_evaluation_result: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    last_evaluation_explanation: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    last_evaluation_feedback: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    last_evaluation_at: Mapped[Optional[datetime]] = mapped_column(
        nullable=True
    )
    evaluator_parse_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    paused_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancelled_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Research missions (Arbor) — sidecar tables; `missions` is never altered.
# ---------------------------------------------------------------------------


class ResearchRun(Base):
    """Sidecar row that makes a mission a research (Arbor) run.

    Presence of this row IS the research-kind dispatch — ``missions``
    itself is never altered. ``meta`` mirrors Arbor's ``tree.meta``
    (closed key set, enforced by :class:`~surogates.arbor.store.ResearchStore`;
    machine-score keys are writable only by the merge / baseline paths).
    """

    __tablename__ = "research_runs"
    __table_args__ = (
        Index("idx_research_runs_session", "session_id"),
        UniqueConstraint("mission_id", name="uq_research_runs_mission"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    mission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("missions.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    trunk_branch: Mapped[str] = mapped_column(Text, nullable=False)
    branch_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="init"
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )


class IdeaNode(Base):
    """One hypothesis in a research run's Idea Tree.

    ``node_key`` is Arbor's dotted-decimal key ("ROOT", "1", "1.2").
    ``score`` is the absolute dev-split score (never a delta).
    ``task_id`` is the experiment ledger join: dispatch writes it,
    harvest folds by it.
    """

    __tablename__ = "idea_nodes"
    __table_args__ = (
        UniqueConstraint("run_id", "node_key", name="uq_idea_nodes_run_key"),
        Index("idx_idea_nodes_run_status", "run_id", "status"),
        CheckConstraint(
            "status IN ('pending','running','done','failed','merged','pruned')",
            name="ck_idea_nodes_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_runs.id"), nullable=False
    )
    node_key: Mapped[str] = mapped_column(Text, nullable=False)
    parent_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    depth: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="pending"
    )
    insight: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    code_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    related_work: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now()
    )
