"""Tenant context propagation via contextvars.

Every authenticated request establishes a ``TenantContext`` that travels
implicitly through the async call-chain so that business logic never
needs an explicit *org_id* parameter.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

__all__ = [
    "PrincipalKind",
    "TenantContext",
    "get_tenant",
    "set_tenant",
]


class PrincipalKind(StrEnum):
    """The principal type authenticated on a request.

    Used for event-payload discriminators and audit-view projections so
    the two principal shapes don't propagate as stringly-typed literals
    across the codebase.
    """

    USER = "user"
    JUDGE = "judge"


@dataclass(frozen=True)
class TenantContext:
    """Immutable snapshot of the current tenant + principal identity.

    Exactly one of *user_id* or *service_account_id* is non-None.  A
    JWT-authenticated request sets *user_id*; a service-account request
    (``POST /v1/api/prompts`` et al.) sets *service_account_id* and
    leaves *user_id* as ``None``.  Routes that require a human user must
    check *user_id* is not None before using it.

    *session_scope_id* is set only on worker-minted service-account
    *session* JWTs — a short-lived, session-scoped token that lets the
    harness call ``/v1/skills`` and ``/v1/memory`` for an SA-owned
    session.  Bare ``surg_sk_`` tokens (which can submit new prompts)
    leave it ``None``; the ``/v1/api/prompts`` route refuses any context
    where it is set so a leaked session JWT cannot open new sessions.
    """

    org_id: UUID
    user_id: UUID | None
    org_config: dict
    user_preferences: dict
    permissions: frozenset[str]
    asset_root: str  # path to tenant's asset directory
    service_account_id: UUID | None = None
    session_scope_id: UUID | None = None

    def covers_session(self, session_id: UUID) -> bool:
        """Return True when this context is allowed to act on *session_id*.

        A context without *session_scope_id* (regular user JWT or bare
        service-account token) covers every session in its org.  A
        session-scoped context (worker-minted ``service_account_session``
        JWT) covers only the one session it was minted for — session
        routes use this to reject cross-session access even when the
        org_id matches.
        """
        return self.session_scope_id is None or self.session_scope_id == session_id

    def owns_session(self, session_org_id: UUID, session_id: UUID) -> bool:
        """Return True when this context may act on the given session row.

        Combines the org scope check (session must belong to the
        tenant's org) with :meth:`covers_session`.  Session-scoped
        routes use this as a single predicate so the two conjuncts
        cannot drift apart across call sites.
        """
        return session_org_id == self.org_id and self.covers_session(session_id)

    def principal(self) -> tuple[PrincipalKind, UUID]:
        """Return the authenticated principal as ``(kind, id)``.

        ``kind`` is ``USER`` for JWT callers and ``JUDGE`` for bare or
        session-scoped service-account tokens — the discriminator
        persisted on feedback events and exposed by audit views.
        Raises if somehow neither identifier is set (the auth middleware
        guarantees one is, so this only fires on genuine invariant
        violations).
        """
        if self.service_account_id is not None:
            return PrincipalKind.JUDGE, self.service_account_id
        if self.user_id is not None:
            return PrincipalKind.USER, self.user_id
        raise RuntimeError(
            "TenantContext has no principal; auth middleware failed to "
            "set either user_id or service_account_id",
        )


_tenant_ctx: contextvars.ContextVar[TenantContext] = contextvars.ContextVar(
    "tenant_ctx"
)


def get_tenant() -> TenantContext:
    """Return the active ``TenantContext``.

    Raises ``LookupError`` when called outside an authenticated request
    (i.e. the context variable has never been set in this async task).
    """
    return _tenant_ctx.get()


def set_tenant(ctx: TenantContext) -> contextvars.Token[TenantContext]:
    """Bind *ctx* as the current tenant context.

    Returns a reset token that callers can pass to
    ``_tenant_ctx.reset()`` to restore the previous value.
    """
    return _tenant_ctx.set(ctx)
