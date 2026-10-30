"""SQLAlchemy 2.x ORM models for the Surogates platform."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.dialects.postgresql import ARRAY, INT8RANGE, JSONB, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)
from sqlalchemy.types import TypeDecorator

_UTC = timezone.utc


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


class EventData(TypeDecorator):
    """An event's JSONB payload, read with typed browser text masked.

    Typed text is masked on write, but events stored before that still hold
    it in the clear. Masking on read here reaches every reader of the column,
    the runtime's API and replay as well as the control plane, which selects
    it directly.
    """

    impl = JSONB
    cache_ok = True
    # Without these the column loses JSONB's operators: has_key would bind
    # its key as jsonb, which Postgres has no ``?`` for.
    comparator_factory = JSONB.Comparator

    def coerce_compared_value(self, op, value):
        return self.impl.coerce_compared_value(op, value)

    def process_result_value(self, value, dialect):
        # Imported here: the harness package imports the database models.
        from surogates.harness.redact import mask_typed_arguments

        return None if value is None else mask_typed_arguments(value)


class UTCDateTime(TypeDecorator):
    """A ``timestamptz`` column that is always UTC-aware in Python.

    Two things go wrong with a plain ``DateTime(timezone=True)`` and naive
    values.  On write, asyncpg's timestamptz encoder interprets a naive
    datetime as *system local* — so a pod with ``TZ=Europe/Bucharest`` in its
    values file stores every check-in three hours off, and nothing in a
    SQLite-backed test can see it.  On read, SQLite hands back naive values
    while Postgres hands back aware ones, so the same comparison raises on one
    backend and passes on the other.

    This pins both ends: a naive value is bound as UTC (never local), and every
    read comes back aware UTC on every backend.  Postgres DDL is unchanged.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=_UTC)
        return value.astimezone(_UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=_UTC)
        return value.astimezone(_UTC)


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
    # How a Firebase user most recently signed in — the token's
    # ``firebase.sign_in_provider`` (e.g. "password", "google.com",
    # "github.com"). Drives whether the web app offers password reset:
    # only "password" users have one to reset. Local (database) accounts
    # leave this NULL; they are password-capable via ``auth_provider``.
    sign_in_provider: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Self-registration profile: a lowercase handle (unique per org via
    # the partial index in observability.sql) and an optional phone.
    username: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    memory_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    # Set on the stand-in users a rehearsal run creates, so no reader of real
    # people ever lists them.
    rehearsal_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
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
        # One account per (org, email), case-insensitive — email is the
        # login key and the Configure→Users attach-by-email guard
        # assumes it. Fresh databases get this from create_all; legacy
        # databases get it from the guarded observability.sql retrofit,
        # which skips (with a NOTICE) while old duplicates exist.
        Index(
            "uq_users_org_lower_email",
            "org_id",
            text("lower(email)"),
            unique=True,
        ),
    )


# ---------------------------------------------------------------------------
# Organisation memberships
# ---------------------------------------------------------------------------


class OrgMember(Base):
    """A user's membership of an organisation other than their home one.

    A user's home organisation is ``users.org_id``: every user belongs to
    it, and it holds their account. This table lists only the further
    organisations a user belongs to, so the writers that create users
    need not know it exists. ``surogates.tenant.memberships`` reads and
    writes it.
    """

    __tablename__ = "org_members"
    __table_args__ = (Index("ix_org_members_user_id", "user_id"),)

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
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
# Agent-user bindings
# ---------------------------------------------------------------------------


class AgentUser(Base):
    """Membership of an end-user on one agent of the org.

    User accounts are org-scoped (one account works against every
    agent in the project); this table records which agents a user
    actually belongs to, so per-agent surfaces (the Users page, its
    reports) can show *that agent's* users instead of the whole org
    roster.  A user of two agents has two rows — correct, since
    memory and reports are already per-agent per-user.

    Rows are written wherever assignment intent exists:

    * ``manual``   — operator created the account on a specific agent
      (Configure → Users, via the ops client).
    * ``login``    — the user authenticated through an agent's web
      channel (password or Firebase; the serving agent comes from the
      runtime context).
    * ``session``  — first interactive session with the agent (covers
      Slack/Telegram shadow users, which never log in).
    * ``backfill`` — derived from historical ``sessions`` rows by the
      migrate-time backfill.

    Enrollment is append-only and idempotent (INSERT .. ON CONFLICT DO
    NOTHING on the unique triple); the binding does NOT gate
    authentication — any org user can still log into any agent of the
    org, gaining a ``login`` row in the process.
    """

    __tablename__ = "agent_users"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "agent_id", "user_id", name="uq_agent_users_org_agent_user"
        ),
        Index("idx_agent_users_org_agent", "org_id", "agent_id"),
        Index("idx_agent_users_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    # Text, no FK — agents are management-plane entities mirrored into
    # this database only as ids (same convention as sessions.agent_id).
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now()
    )
    # Tombstone, not delete: a removed binding must keep existing so
    # the backfill and the automatic login/session enrollment (both
    # ON CONFLICT DO NOTHING) can never resurrect it. Cleared only by
    # the explicit manual re-attach.
    removed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


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
        # Walking a conversation's spawned work means recursing on
        # parent_id, and every level of that recursion is a fresh lookup —
        # unindexed it degrades to a sequential scan per level, which is
        # what makes an otherwise-bounded walk expensive on a large table.
        Index("idx_sessions_parent", "parent_id"),
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
    data: Mapped[dict[str, Any]] = mapped_column(EventData, nullable=True)
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
        # The expiry sweeper's working set — pending items by kind and
        # age. Partial because pending is the small minority of a table
        # that only grows; see inbox_principal.sql, which retrofits it
        # onto databases that already exist.
        Index(
            "idx_inbox_pending_kind_created",
            "kind",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
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
        # Deleting events or sessions checks every removed row against
        # these references; unindexed, each check scans the whole outbox.
        Index("idx_delivery_outbox_event", "event_id"),
        Index("idx_delivery_outbox_session", "session_id"),
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
        Index(
            "idx_scheduled_sessions_expiry",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
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
    # How the next run is computed (``scheduled.schedule.CADENCE_KINDS``) and
    # its parameters; NULL on rows made before them, read from ``schedule``.
    kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cadence: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # Who a run acts for and what it logs in as, captured at creation
    # (surogates.session.execution_context).
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ended_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_run_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # When its creating chat was told it ended on its own (expired, limit, failed).
    ended_announced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Ambient schedules (Surogate Mate)
# ---------------------------------------------------------------------------


class ApprovalGrant(Base):
    """A human's decision to let one blocked tool call through.

    Approving used to emit a chat message and nothing else — nothing the
    gate could consult, so the same call was blocked again on retry.  A
    grant is that decision made durable.

    Validity is never stored as a flag.  ``consume_approval_grant``
    recomputes it on every read from ``revoked_at`` / ``expires_at`` /
    ``used_count``, so a grant cannot drift into looking live after the
    fact.

    Scope is the *call*, not the tool: ``arguments_hash`` pins the exact
    arguments that were shown to the human, so approving one deploy does
    not approve every future one.
    """

    __tablename__ = "approval_grants"
    __table_args__ = (
        Index(
            "idx_approval_grants_lookup",
            "session_id", "tool_name", "arguments_hash",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256 of the canonical (sorted-key) tool arguments.
    arguments_hash: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    max_uses: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1",
    )
    used_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0",
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class OfficeSession(Base):
    """A document open in the document server: one open row per file."""

    __tablename__ = "office_sessions"
    __table_args__ = (
        Index(
            "uq_office_sessions_open_location", "location", unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("idx_office_sessions_pending", "updated_at", postgresql_where=text("pending_save IS NOT NULL")),
        CheckConstraint("status IN ('open', 'closed')", name="ck_office_sessions_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    # Where the file is, so one file has one row however it is reached.
    location: Mapped[str] = mapped_column(Text, nullable=False)
    # The chats whose panes opened the document (UUID strings); events go to all.
    session_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # The case whose document it is, when a case's file was opened: its files are the case's, and no chat holds it open.
    case_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    doc_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # The file's version when the document server fetched it or a save landed.
    base_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    editors: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # Who opened the document, by editor user id: people's names and the agent's, for the agent's "open by".
    user_names: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    # How many of the document server's history entries (history.changes) are already landed.
    changes_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # A save waiting to land: {"bucket", "key", "lastsave", "close", "attempts", "marks", "authors", "changes"}.
    # None is SQL NULL, not a JSON null, so a landed or closed row drops out of ``pending_save IS NOT NULL``.
    pending_save: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    connected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


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
# Check-in Programs
# ---------------------------------------------------------------------------


class ProgramScheduleRow(Base):
    """The ticker's claim/lock for one active Program.

    Mirrors an ops ``programs`` row.  Deliberately separate from the
    configuration: the runtime does not read ops tables, and this row holds
    only what ticking needs.
    """

    __tablename__ = "program_schedules"
    __table_args__ = (
        UniqueConstraint("program_id", name="uq_program_schedule_program"),
        Index(
            "idx_program_schedules_due", "active", "next_run_at",
            postgresql_where=text("active"),
        ),
    )

    # Portable Postgres types, as AmbientScheduleRow uses: UUID/JSONB on
    # Postgres (prod), String/JSON on SQLite so this table is unit-testable
    # in isolation.  The Postgres DDL is unchanged.
    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4,
    )
    program_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: The projected ops configuration, refreshed by reconcile.  The roster
    #: travels here too, so a tick never calls back into ops.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict,
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    last_run_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    locked_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )


class ProgramOccurrenceRow(Base):
    """One due time.  Carries what must not change once the check-in starts."""

    __tablename__ = "program_occurrences"
    __table_args__ = (
        UniqueConstraint(
            "program_id", "scheduled_for",
            name="uq_program_occurrence_instant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4,
    )
    program_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), nullable=False, index=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False,
    )
    #: Which skill — not its content.  The skill is read live at reply time so
    #: an operator can correct a wrong question mid-check-in.
    skill_ref: Mapped[str] = mapped_column(Text, nullable=False)
    #: Pinned, unlike the skill: Meta approved this exact text.
    template_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    template_language: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="fired",
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(),
    )


class ProgramInvitationRow(Base):
    """One user, one occurrence.

    The pending record the reply path matches on, the run-history row, and the
    deadline timer — one object, because they are one fact.

    Delivery, response and escalation are three independent axes and must
    never be collapsed: a send that failed is not a user who stayed silent.
    """

    __tablename__ = "program_invitations"
    __table_args__ = (
        UniqueConstraint(
            "occurrence_id", "user_id",
            name="uq_program_invitation_user",
        ),
        # The inbound lookup.  agent_id is part of the key because
        # channel_identities is org-scoped and one person can be bound to
        # several agents — without it, one agent's Program would swallow
        # another agent's reply.
        Index(
            "idx_program_invitations_open",
            "org_id", "agent_id", "platform", "platform_user_id",
            "response_state",
        ),
        # One person's history in one Program, newest first: the missed
        # check-in streak and the silence pause both read it per person.
        # Shipped to existing databases by observability.sql.
        Index(
            "idx_program_invitations_program_user",
            "program_id", "user_id", "created_at",
        ),
        Index(
            "idx_program_invitations_unreported",
            "created_at", postgresql_where=text("reported_at IS NULL"),
        ),
        # Whether a person already has an open question, whichever channel
        # it went out on.
        Index(
            "idx_program_invitations_person_open",
            "org_id", "agent_id", "user_id", "response_state",
        ),
        # The agent's message in a conversation it was woken in for a step,
        # matched back to that step when the dispatcher posts it.
        Index(
            "idx_program_invitations_woken",
            "session_id",
            postgresql_where=text("woken_at IS NOT NULL AND outbox_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4,
    )
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), nullable=False, index=True,
    )
    program_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: The followed person this step is about, when the recipient is one of
    #: their contacts.  Null on older rows: ``user_id`` is the person.
    person_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    platform_user_id: Mapped[str] = mapped_column(Text, nullable=False)

    delivery_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="queued",
    )
    #: The outbox row carrying this opener.  This is the join key the delivery
    #: loop reports back through: the provider's message id does not exist
    #: until the dispatcher has actually posted, minutes after enqueue.
    outbox_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, index=True,
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, index=True,
    )
    #: The provider's own wording for a failed send.  Kept apart from
    #: ``reason`` so a late status webhook can never overwrite the agent's
    #: operational note or a skip explanation — the three axes stay separate on
    #: disk too.
    delivery_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="not_started",
    )
    escalation_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="none",
    )
    #: ``reminded`` or ``reminder_failed`` once an unopened red alert of this
    #: check-in's escalation was sent again.  Its own column: the escalation
    #: stays ``raised``, which is what run counts read.
    reminder_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: When the team was told this check-in's opener did not arrive.
    reported_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    deadline_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True,
    )
    #: Which bundle version this turn ran against — recorded, not pinned.
    skill_bundle_version: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    #: Why this row is in the state it is, in operator words.
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The business sender this opener goes out from on ``platform``.  Null on
    #: rows from before a step could pick a channel: the Program's own sender.
    channel_identifier: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: What the agent recorded as the result, e.g. ``completed`` or a value a
    #: branch reads (``red``).
    outcome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The person's other reachable channels, tried once each when a send fails.
    fallback_platforms: Mapped[Optional[list]] = mapped_column(
        # None is SQL NULL, never JSON null: queries test IS NULL.
        JSONB(none_as_null=True).with_variant(JSON(none_as_null=True), "sqlite"), nullable=True,
    )
    #: Channels already tried: ``[{platform, state, error, at}]``.
    attempts: Mapped[Optional[list]] = mapped_column(
        JSONB(none_as_null=True).with_variant(JSON(none_as_null=True), "sqlite"), nullable=True,
    )
    read_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    #: Who on the team took the escalation, and when it was dealt with.
    escalation_owner: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    #: When the send pass claimed this step: its opener went out, or the
    #: agent was woken to write it.  A reply answers the most recently
    #: opened step in the conversation.
    opened_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    #: When the agent was woken to write this step itself, instead of a
    #: template going first.  Its first message in ``session_id`` is the
    #: step's message, and the delivery report is matched through it.
    woken_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    #: When the agent was woken because the deadline passed with no reply.
    #: Set once, before the wake, so a step is never woken twice for it.
    deadline_woken_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(),
    )


# ---------------------------------------------------------------------------
# Follow-up protocols
# ---------------------------------------------------------------------------


class FollowupEnrollmentRow(Base):
    """One person following one protocol on one agent.

    The anchor a Program audience measures days from.  ``protocol`` is the
    operator's own slug; nothing in the runtime knows what any protocol means.
    """

    __tablename__ = "followup_enrollments"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "agent_id", "user_id", "protocol",
            name="uq_followup_enrollment",
        ),
        # The materialize lookup: every active enrollment of a protocol.
        Index(
            "idx_followup_enrollments_protocol",
            "org_id", "agent_id", "protocol", "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    protocol: Mapped[str] = mapped_column(Text, nullable=False)
    #: Day 0 of the protocol, e.g. the procedure date.  Null until known: a
    #: person enrolled without one is reached only by audiences with no day
    #: window.
    start_date: Mapped[Optional[Any]] = mapped_column(Date, nullable=True)
    #: Set when the person only knew it roughly ("about two weeks ago").
    start_date_approximate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False,
    )
    phase: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The person's own name (``person_name`` in the tools).  The user's
    #: display name is their WhatsApp profile, which is a relative's when
    #: the relative enrolled them.
    patient_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Who answers the check-ins when not the person, e.g. "Ana, daughter".
    answered_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active",
    )
    #: Why the enrollment is paused or ended, in operator words.
    status_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: ``agent`` when the agent enrolled the person in conversation,
    #: ``operator`` when someone did it from Studio, ``purchase`` when a
    #: paid package did, ``team`` (formerly ``care_team``) when a team member
    #: did it from WhatsApp.
    enrolled_by: Mapped[str] = mapped_column(Text, nullable=False)
    #: Who paid, when a purchase enrolled them: often a relative, so not
    #: necessarily ``user_id``.
    payer_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True,
    )
    #: What created the enrollment, e.g. ``checkout:cs_…``.
    source_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The person's own zone, when it is not the journey's.
    timezone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The channel the person asked to be reached on.
    preferred_channel: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The memory partition the person's record lives in, shared by their
    #: own conversations and their contacts'.  Set on first use from the
    #: conversation already holding it, so no record is left behind.
    memory_boundary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )


class FollowupContactRow(Base):
    """Someone linked to a followed person: a relative, a carer.

    Every step reaches the person and each confirmed contact, an answer from
    any of them answers it for all, and they share the person's record.  A
    ``pending`` link waits for an already-linked person to say yes.
    """

    __tablename__ = "followup_contacts"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "agent_id", "person_user_id", "contact_user_id",
            name="uq_followup_contact",
        ),
        Index("idx_followup_contacts_contact", "org_id", "agent_id", "contact_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    person_user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    contact_user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: Who they are to the person, in the words given, e.g. "daughter".
    relation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: ``confirmed`` or ``pending``.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="confirmed")
    #: On a pending link: who was asked to confirm it.
    asked_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(),
    )


class FollowupSettingsRow(Base):
    """Where an agent's escalations go when no check-in carries a target."""

    __tablename__ = "followup_settings"

    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: The Studio inbox escalations land in.  A service account, never a
    #: human's id: inbox_items.service_account_id is a foreign key.
    escalation_service_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True,
    )
    #: The business sender alerts and operator messages go out from, e.g.
    #: the agent's WhatsApp phone_number_id.  Stored, not inferred: an
    #: escalation raised in a web conversation has no sender of its own.
    channel_identifier: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: End users on this agent who are told on their phone: the team.
    alert_recipient_user_ids: Mapped[list[str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list,
    )
    #: The agent's sender per channel, ``{platform: identifier}``.  WhatsApp
    #: falls back to ``channel_identifier`` when absent.
    senders: Mapped[dict[str, str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict,
        server_default=text("'{}'"),
    )
    #: Where each team member is told, in order: ``{user_id: [platform, …]}``.
    team_channels: Mapped[dict[str, list]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict,
        server_default=text("'{}'"),
    )
    #: What each team member receives and may do:
    #: ``{user_id: {receives, can, handles}}``.  A member not listed gets
    #: everything.
    team_permissions: Mapped[dict[str, dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict,
        server_default=text("'{}'"),
    )
    #: Shared places that also receive alerts, e.g. a Slack channel:
    #: ``[{platform, channel_id, receives: [...]}]``.
    team_destinations: Mapped[list[dict]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list,
        server_default=text("'[]'"),
    )
    #: Whether people reached on web get a browser push when a message waits
    #: for them in their chat (they must also have allowed it in a browser).
    web_push: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false"),
    )
    #: The approved template that reopens a recipient's conversation when the
    #: provider's customer-service window has closed.  One for alerts to the
    #: team, one for messages an operator sends a person.
    alert_template_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    alert_template_language: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    message_template_name: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    message_template_language: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    #: The approved template that opens a newly paid person's
    #: conversation.  The text that follows once they answer it is
    #: ``texts["welcome_text"]``.
    welcome_template_name: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    welcome_template_language: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
    )
    #: Which protocol each package enrols its buyer in:
    #: ``{commerce offer id: protocol}``.  An offer not listed enrols nobody.
    offer_protocols: Mapped[dict[str, str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict,
    )
    #: Wording in the business's language for what the platform itself
    #: writes: the checkout field labels and the notices the team gets.
    #: Keys: ``checkout_phone``, ``checkout_name``, ``checkout_consent``,
    #: ``purchase_notice``, ``invalid_number_notice``, ``welcome_text``.
    #: Missing keys use the platform's defaults.
    texts: Mapped[dict[str, str]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict,
    )
    #: How long a team member's message held behind a template waits for the
    #: person's answer before the team member is told it did not arrive.  NULL
    #: is the platform default; other held texts always wait 72 hours.
    operator_unanswered_hours: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    #: Minutes a red alert may sit unopened by a team member before it is
    #: sent again, once, to the whole team.  NULL is the platform default.
    red_alert_reminder_minutes: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(),
        onupdate=func.now(),
    )


class ChannelContactRow(Base):
    """When a person last wrote to one of our senders.

    The provider's customer-service window and the silence filter both read
    it.  A table of its own, not a key in ``channel_identities.platform_meta``:
    that blob is rewritten whole by ops when permission changes, and a write
    per inbound message would race it and could drop a permission.
    """

    __tablename__ = "channel_contacts"

    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    platform: Mapped[str] = mapped_column(Text, primary_key=True)
    platform_user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: The business sender, e.g. a WhatsApp phone_number_id: the window is
    #: per sender, not per person.
    channel_identifier: Mapped[str] = mapped_column(Text, primary_key=True)
    last_inbound_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False,
    )


class OutreachMessageRow(Base):
    """A message the platform must get to one person, unprompted.

    An alert to the team, an operator's message to a person, a notice
    that someone enrolled.  Inside the provider's window it goes out as text;
    outside it the approved template goes first and the text waits for the
    reply.
    """

    __tablename__ = "outreach_messages"
    __table_args__ = (
        # One message per cause: a retried webhook or a re-run sweep must not
        # write to a person twice.  NULL keys never collide.
        UniqueConstraint("org_id", "dedupe_key", name="uq_outreach_dedupe"),
        Index(
            "idx_outreach_messages_pending",
            "org_id", "agent_id", "user_id", "state",
        ),
        Index("idx_outreach_messages_state", "state"),
        Index(
            "idx_outreach_messages_unreported",
            "created_at", postgresql_where=text("reported_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: The recipient.
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    #: A shared place (a Slack channel id) the message goes to instead of a
    #: person; ``user_id`` is then the nil id.
    destination: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The business sender to write from.
    channel_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``alert`` · ``operator`` · ``enrolment`` · ``welcome`` · ``notice``.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    dedupe_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    template_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    template_language: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: ``queued`` → ``sent`` (inside the window) or ``awaiting_reply``
    #: (template sent) → ``delivered``; or ``failed``.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="queued",
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: What the message is about, for the run history: the person an alert
    #: concerns.
    subject_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True,
    )
    #: The recipient's session the text was or will be written into.
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True,
    )
    #: The recipient's number, recorded when a template reopens their
    #: conversation, so the send pass can tell in SQL whether they answered.
    platform_user_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: On the row that sent a reopening template: the outbox row carrying it,
    #: and the provider's id once sent.  How a failed template is traced back
    #: to the texts waiting behind it.
    outbox_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, index=True,
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, index=True,
    )
    #: The provider's id for the text itself, kept apart from the
    #: template's so a late status for either still finds the row.
    text_message_id: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, index=True,
    )
    #: On a text held behind another row's template: that row.  A failed
    #: template fails exactly the texts riding on it.
    rides_on: Mapped[Optional[uuid.UUID]] = mapped_column(
        GUID(), nullable=True, index=True,
    )
    created_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: On an alert: ``red`` when an emergency script went to the person,
    #: else ``yellow``.  A red one left unopened is sent again once.
    severity: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: What an alert is about, without the severity mark and the footer
    #: naming the person that its text carries.
    alert_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: When the one reminder for this alert's escalation went out.
    reminded_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    #: When WhatsApp said the recipient read this message's text.  A
    #: template read does not count: the text behind it is still unseen.
    read_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    #: When this message's outcome was reported to whoever asked for it,
    #: or its failure to the team.  Reported once, never re-read.
    reported_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    #: Held until then: a team member's message that would reach the person
    #: inside their quiet hours.  Null sends at once.
    not_before: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(),
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime(), nullable=True,
    )


class EscalationRow(Base):
    """Something about one person that a human on the team must deal with.

    Its own record, not a state on an invitation: most escalations happen in
    free conversation, with no step open, and the team's queue must hold
    those too.
    """

    __tablename__ = "escalations"
    __table_args__ = (
        Index("idx_escalations_agent_open", "org_id", "agent_id", "resolved_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: The person it is about; null when the conversation had no known person.
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    #: The channel the person was on when it was raised.
    platform: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    invitation_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True, index=True)
    program_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    #: ``alert`` (raised by the agent) or ``missed_replies``.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``raised``, or ``failed`` when nobody could be told.
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="raised")
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), nullable=True)
    owner: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(),
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
    # An agent-owned profile is used, without being bound, by every session
    # of the agent on these channels: the operator's grant of its login.
    use_in_channels: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
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

    ``kind`` separates two populations that share this table because they
    share an auth path, but obey opposite cardinality rules:

    * ``agent_principal`` — the machine identity an agent *acts as*.  Exactly
      one per agent, enforced by ``uq_service_accounts_agent_principal``.
      Provisioned by the control plane, never surfaced to an end user.
    * ``api_key`` — a customer-facing key for the OpenAI-compatible API
      channel.  Many per agent, minted and revoked by the agent's operator.
    * ``service`` — an ordinary org-scoped account with no agent binding
      (synthetic-data pipelines, ops-chat, evaluation identities).
    * ``case_type`` — the identities Keel's sessions run under:
      ``case-type:<case_type>``, one per case type per org, for a case type's
      agent-step and work-item sessions, and ``keel-case-agent``, one per
      org, for every person's case agent.  Each name is one per org,
      enforced by ``uq_service_accounts_case_type``.  Its token is discarded
      at creation, so nobody signs in as it.

    Without this split the partial unique index below caps an agent at one
    token, which makes "create a second API key" impossible.
    """

    __tablename__ = "service_accounts"
    __table_args__ = (
        Index("idx_service_accounts_org", "org_id"),
        # One *principal* per agent — API keys are deliberately outside the
        # predicate so an agent can carry as many of them as its operator
        # mints.  Renamed from ``uq_service_accounts_agent`` when ``kind``
        # was introduced; ``observability.sql`` drops the old name.
        Index(
            "uq_service_accounts_agent_principal",
            "agent_id",
            unique=True,
            postgresql_where=text(
                "agent_id IS NOT NULL AND kind = 'agent_principal'"
            ),
        ),
        # Listing an agent's API keys is a per-agent read; the org index
        # above does not serve it once an org owns many agents.
        Index(
            "idx_service_accounts_agent",
            "agent_id",
            postgresql_where=text("agent_id IS NOT NULL"),
        ),
        # One account per case type per org; the name carries the case type.
        Index(
            "uq_service_accounts_case_type",
            "org_id",
            "name",
            unique=True,
            postgresql_where=text("kind = 'case_type'"),
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
    # FK).  Set both for the agent's own principal and for the operator-minted
    # API keys bound to it; NULL for ordinary org-scoped service accounts.
    # ``kind`` decides which, and the partial unique index above keeps the
    # *principal* one-per-agent while leaving API keys unbounded.
    agent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # See the class docstring: ``agent_principal`` | ``api_key`` | ``service``
    # | ``case_type``.
    # Plain text rather than an enum so adding a population needs no DDL —
    # this table has no Alembic, only the retrofits in ``observability.sql``.
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'service'"), default="service",
    )

    # Relationships
    org: Mapped[Org] = relationship(
        back_populates="service_accounts", lazy="raise"
    )
    sessions: Mapped[list[Session]] = relationship(
        back_populates="service_account", lazy="raise"
    )


class Device(Base):
    """A computer running Surogate Desktop, registered by one user for one agent.

    The desktop authenticates its device link with a ``surg_dev_`` bearer
    token.  As for service accounts, only the token's SHA-256 digest is
    stored; the raw token is returned once, on creation or reauthorization.
    ``credential_generation`` advances on every reauthorization, so a socket
    still open under an older token can be recognised and closed.
    """

    __tablename__ = "devices"
    __table_args__ = (Index("idx_devices_owner", "org_id", "agent_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    # Logical reference to the ops ``Agent.id`` (another database, so no FK).
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    # When the link first sent the device its welcome: NULL for one that never connected.
    connected_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    # When its token was last reissued: a reauthorization restores a computer, and its user is told.
    reauthorized_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    credential_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), default=1
    )


class OAuthRefreshToken(Base):
    """A refresh token of a public OAuth client: Surogate Desktop's sign-in.

    Each sign-in starts a family.  A refresh spends its token (``used_at``)
    and issues the next one in the same family, which keeps the sign-in's
    ``auth_time`` and computer.  A spent token presented again means it has
    two holders, so the whole family is revoked; so does revoking the
    computer.  As for device tokens, only the token's SHA-256 digest is
    stored.
    """

    __tablename__ = "oauth_refresh_tokens"
    __table_args__ = (
        Index("idx_oauth_refresh_tokens_family", "family_id"),
        Index("idx_oauth_refresh_tokens_device", "device_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # Logical reference to the ops ``Agent.id``: a token works only at the agent that issued it.
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The computer this sign-in added or restored, a logical reference to ``devices.id``:
    # revoking the computer ends its sign-in.
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # When the user signed in, in epoch seconds, as the access tokens carry it.
    auth_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)


class DeviceOperation(Base):
    """One operation a worker asked a device to run: the durable journal entry.

    Postgres is the authority for requests and outcomes; Redis only wakes the
    API connection that delivers an operation and the worker that waits for
    it.  An operation is identified by its calling session, its invocation
    (the tool call) and its ordinal within that invocation, so running the
    same tool call again asks for the same operations and gets their recorded
    outcomes back instead of repeating them.
    """

    __tablename__ = "device_operations"
    __table_args__ = (
        UniqueConstraint(
            "calling_session_id", "invocation_id", "ordinal",
            name="uq_device_operations_invocation",
        ),
        Index(
            "idx_device_operations_open", "device_id", "created_at",
            postgresql_where=text("completed_at IS NULL"),
        ),
        # What a session asked of one kind, by when it closed: a landing reads a thread's commands, writes and records
        # since its last landing (DeviceOperations.named) without the rest of the thread's history.
        Index("idx_device_operations_session_kind", "calling_session_id", "kind", "completed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    root_session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    calling_session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invocation_id: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON, not JSONB: command output and paths may contain NUL characters,
    # which jsonb rejects.
    args: Mapped[dict] = mapped_column(JSON, nullable=False)
    # SHA-256 of the immutable request; see OperationRequest.digest.
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    # The session lease that asked for it: the execution generation.
    lease_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    outcome: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)


class DeviceTransfer(Base):
    """An operation's data too large for one link frame, which goes in chunks.

    The operation's kind says which way: a read's result comes from the
    device, a write's data goes to it.  A read's outcome names it by size and
    SHA-256, and its last chunk completes the operation in one transaction, so
    a read's transfer whose bytes are all here is one its operation was
    answered with.  A write's args name it the same way; it is stored whole
    in the operation's own transaction, so it is whole while the operation is
    open, and no connection holds it.  Payloads go on a schedule
    (``surogates.devices.operations.reap_transfers``); the operation's row stays.
    """

    __tablename__ = "device_transfers"

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_operations.id", ondelete="CASCADE"), primary_key=True,
    )
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    # Bytes stored so far, in order: the next chunk starts here.  A write's
    # is its size from the start.
    received: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    # The link connection sending a read's: another connection's chunks never
    # count.  Empty for a write's, which the worker stores whole.
    holder: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    # When the tool result that read it was committed.  A write's is marked
    # too, and nothing reads that mark.
    consumed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)


class DeviceTransferChunk(Base):
    __tablename__ = "device_transfer_chunks"

    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_transfers.operation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class Credential(Base):
    __tablename__ = "credentials"
    __table_args__ = (
        # NULLS NOT DISTINCT (PG 15+) makes org-scoped rows (both principal
        # columns NULL) collide with each other, which the upsert in
        # CredentialVault.store relies on to be race-safe.
        UniqueConstraint(
            "org_id",
            "user_id",
            "service_account_id",
            "name",
            name="uq_credentials_org_user_sa_name",
            postgresql_nulls_not_distinct=True,
        ),
        # A credential is scoped to a user OR a service account (or neither, for
        # org scope) — never both.
        CheckConstraint(
            "NOT (user_id IS NOT NULL AND service_account_id IS NOT NULL)",
            name="ck_credentials_one_principal",
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
    # Retry attempts consumed: incremented on claim, and given back by
    # ``worker_block`` / ``_block_claim`` — blocking is a pause, not a
    # failure. Not a count of Sessions spawned (a blocked attempt leaves
    # its Session behind).
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
        # Deleting a session checks its notes by writer; unindexed, a scan.
        Index("idx_board_notes_writer_session", "writer_session_id"),
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
    # Token allowance for the whole objective. NULL = unbounded.  Tokens,
    # not cost: PROD runs tier sentinels priced at 0.0, so a cost ceiling
    # would never trip (a 1.48M-token session recorded 0 USD).
    budget_tokens: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
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
    # Consecutive evaluations that did not report progress. The judge runs
    # every round regardless; without this it had no memory of having
    # already said the same thing.
    stagnant_evaluations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
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


# ---------------------------------------------------------------------------
# Projects -- a sidecar table; `sessions` is never altered.
# ---------------------------------------------------------------------------


class Workstream(Base):
    """A project: one user's coordinator chat, the master session.

    The UI says "Projects"; the code says ``workstreams``, because a project
    is already the ops tenant.  Archiving keeps the row, its files and its
    memory.  Deleting the user deletes the row (``purge_user_account``), and
    so does ops's hard delete of the master session, by the cascade.
    """

    __tablename__ = "workstreams"
    __table_args__ = (Index("idx_workstreams_owner", "org_id", "agent_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False
    )
    # Logical reference to the ops ``Agent.id`` (another database, so no FK).
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    goal: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    master_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False, unique=True,
    )
    # 'basic' | 'pro', or NULL for the agent's own tier.
    coordinator_tier: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thread_tier: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The computer a project made in Surogate Desktop works on, a logical
    # reference to ``devices.id``, and its folder there, as its user chose it:
    # both NULL for a project in the cloud.  Set when the project is made, and changed, with its master, only as
    # its user uses a computer for a project whose computer lost the folder (WorkstreamStore.move).
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    folder: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 'active' | 'archived'
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WorkstreamThread(Base):
    """A project's thread: a worker session of the master that does one piece
    of the project's work in its own sandbox and reports back.

    The row's presence is what makes the session a thread.  Both keys
    cascade: ops's hard delete of the sessions, and the deletion of the
    project with its user, take the row with them.
    """

    __tablename__ = "workstream_threads"
    __table_args__ = (Index("idx_workstream_threads_workstream", "workstream_id"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True,
    )
    workstream_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workstreams.id", ondelete="CASCADE"), nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )


class WorkstreamHistory(Base):
    """One change to a project's files, the landing saga's durable record.

    A landing writes its row before its first step, with the saga
    ``running``, then its steps once they are fixed and, as they change,
    every five seconds or every twenty times what a write of them takes,
    whichever is longer, so the next holder of the project's lock can
    finish or undo a landing whose worker died.  ``steps`` may be that far
    behind: a step shown ``pending`` may have run.
    The audit is kept without file contents: ``files`` and ``picked_up``
    hold git blob ids.  ``device_id`` and ``folder`` name a folder on that
    computer; both null, the project's cloud files.
    """

    __tablename__ = "workstream_history"
    __table_args__ = (
        Index("idx_workstream_history_workstream", "workstream_id", "created_at"),
        Index(
            "idx_workstream_history_files", "files",
            postgresql_using="gin", postgresql_ops={"files": "jsonb_path_ops"},
        ),
        # The lock holder's first look: the project's landings still running.
        Index("idx_workstream_history_running", "workstream_id", postgresql_where=text("saga_state = 'running'")),
        # The Undos that name some records, and the Undos of those, by the records each undoes (``history.undone_of``).
        Index(
            "idx_workstream_history_undoes", "undoes", postgresql_using="gin", postgresql_where=text("kind = 'undo'"),
        ),
        # Deleting a session nulls its thread's history rows; unindexed, a scan to find them.
        Index("idx_workstream_history_thread", "thread_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workstream_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workstreams.id", ondelete="CASCADE"), nullable=False,
    )
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    folder: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 'landing' | 'pickup' | 'restore' | 'undo'
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    saga_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # 'running' | 'completed' | 'compensated' | 'escalated'
    saga_state: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    commit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The thread's audit outlives its session.
    thread_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True,
    )
    tool_saga_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    events: Mapped[Optional[Any]] = mapped_column(INT8RANGE, nullable=True)
    # Logical reference to the ops ``Agent.id`` (another database, so no FK).
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    picked_up: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    files: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    undoes: Mapped[Optional[list[int]]] = mapped_column(ARRAY(BigInteger), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class WebPushSubscription(Base):
    """A browser that allowed push notifications from one agent.

    Written when the person allows them (``POST /v1/push/subscriptions``),
    forgotten when they disallow or the push service reports the browser gone.
    One row per browser: the endpoint is the browser's own address.
    """

    __tablename__ = "web_push_subscriptions"
    __table_args__ = (Index("idx_web_push_subscriptions_user", "user_id", "agent_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    #: The browser's keys (base64url): what the payload is encrypted for.
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=func.now())


# ---------------------------------------------------------------------------
# Rehearsal
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RehearsalRun(Base):
    """One rehearsal: its world, the agent's account in it, and its status.

    The account columns keep their WhatsApp names; on Telegram they hold the
    bot's username, its token and its webhook secret.
    """

    __tablename__ = "rehearsal_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orgs.id"), nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running")
    channel: Mapped[str] = mapped_column(Text, nullable=False, default="whatsapp")
    phone_number_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    app_secret: Mapped[str] = mapped_column(Text, nullable=False)
    #: The agent's own WhatsApp behaviour config, copied so the run behaves
    #: like the real channel (identity policy, multi-session, ...).
    routing_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: The agent's WhatsApp message templates as Meta lists them, so a
    #: template the agent sends shows with its real header, body and buttons.
    templates: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    #: The agent's WhatsApp Flows as ``{id, name, json}``, so a form the
    #: agent sends opens with its real screens.
    flows: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)
    ended_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)


class RehearsalPerson(Base):
    """Someone a run's user writes as."""

    __tablename__ = "rehearsal_people"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rehearsal_runs.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    #: The person's address on their channel, e.g. a WhatsApp wa_id.
    address: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)
    #: When the agent last showed "typing…" to this person; a send clears it.
    agent_typing_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    #: Who the AI plays when it plays this person; none for a hand-typed one.
    persona: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: ``ai`` while the AI writes as them, ``human`` while the user does.
    driver: Mapped[str] = mapped_column(Text, nullable=False, default="human", server_default="human")
    #: ``talking`` until the AI stops: ``done``, ``gave_up``, ``no_reply`` or ``failed``.
    ai_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ai_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: When the AI last wrote or tapped: it waits for the agent after that.
    ai_acted_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class RehearsalMessage(Base):
    """One message in a world, in Meta's own shape."""

    __tablename__ = "rehearsal_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rehearsal_runs.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rehearsal_people.id", ondelete="CASCADE"), nullable=False,
    )
    #: ``in``: the person wrote it; ``out``: the agent sent it.
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    wamid: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class RehearsalMedia(Base):
    """A file uploaded into a world, by the agent or by the person."""

    __tablename__ = "rehearsal_media"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rehearsal_runs.id", ondelete="CASCADE"), nullable=False,
    )
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
