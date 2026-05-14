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
    the three principal shapes don't propagate as stringly-typed
    literals across the codebase.
    """

    USER = "user"
    JUDGE = "judge"
    CHANNEL = "channel"


@dataclass(frozen=True)
class TenantContext:
    """Immutable snapshot of the current tenant + principal identity.

    Three valid principal shapes:

    * **User** — *user_id* set, *service_account_id* unset.  Produced
      by an ``access`` JWT (interactive UI sessions, programmatic
      callers with a human identity).
    * **Service account** — *service_account_id* set, *user_id* unset.
      Produced by bare ``surg_sk_`` tokens and by worker-minted
      ``service_account_session`` JWTs.  The latter additionally set
      *session_scope_id*.
    * **Channel session** — only *session_scope_id* set (neither
      *user_id* nor *service_account_id*).  Produced by worker-minted
      ``channel_session`` JWTs for anonymous-channel sessions (e.g. the
      public-website widget).  Routes that need a user or
      service-account principal must refuse this context via
      :func:`surogates.api.routes._shared.require_not_channel_principal`
      — :class:`~surogates.storage.tenant.TenantStorage` maps
      ``user_id=None`` to ``shared/*`` paths, which is the correct
      semantics for service-account contexts but a write-anywhere
      hazard for anonymous visitors.

    Routes that require a human user must check *user_id* is not None
    before using it.

    *session_scope_id* is set on worker-minted JWTs (``service_account_session``
    and ``channel_session``) — a long-lived, session-scoped token that
    lets the harness call ``/v1/skills`` and other api-client-gated
    routes for that one session.  Bare ``surg_sk_`` tokens (which can
    submit new prompts) leave it ``None``; the ``/v1/api/prompts``
    route refuses contexts that are not service accounts so a leaked
    session JWT cannot open new sessions.
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

        ``kind`` is one of:

        * :attr:`PrincipalKind.USER` — JWT callers (``user_id`` set).
        * :attr:`PrincipalKind.JUDGE` — bare or session-scoped
          service-account tokens (``service_account_id`` set); the
          discriminator persisted on feedback events and exposed by
          audit views.
        * :attr:`PrincipalKind.CHANNEL` — anonymous-channel session
          JWTs (only ``session_scope_id`` set).  The session itself is
          the principal — there is no human user and no service
          account behind it.

        Raises if none of the three identifiers is set; the auth
        middleware guarantees one is, so this only fires on genuine
        invariant violations.
        """
        if self.service_account_id is not None:
            return PrincipalKind.JUDGE, self.service_account_id
        if self.user_id is not None:
            return PrincipalKind.USER, self.user_id
        if self.session_scope_id is not None:
            return PrincipalKind.CHANNEL, self.session_scope_id
        raise RuntimeError(
            "TenantContext has no principal; auth middleware failed to "
            "set user_id, service_account_id, or session_scope_id",
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
