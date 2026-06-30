"""Read-only cached resolver: agent_id → its service-account principal.

The single public surface the agent-owned-toolkit and agent-workspace surfaces
consume to learn the identity an agent acts as.  Memoized per ``(org_id,
agent_id)`` with negative caching (an agent with no service account does not
re-hit the DB each lookup); evicted cross-process by the
``agent_principal_changed:`` invalidation channel on revoke/rotate.

This module adds NO callers — provisioning (ops) writes the row; the consumers
are separate sub-projects.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from surogates.db.models import ServiceAccount

__all__ = ["ServiceAccountPrincipal", "make_cached_agent_principal_resolver"]


@dataclass(frozen=True, slots=True)
class ServiceAccountPrincipal:
    """The identity an agent acts as — a service account it owns."""

    id: UUID
    org_id: UUID
    name: str


def make_cached_agent_principal_resolver(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    ttl_seconds: float = 30.0,
    max_entries: int = 10_000,
):
    """Return ``resolver(org_id, agent_id) -> ServiceAccountPrincipal | None``.

    ``resolver.cache`` is the backing :class:`ChannelRoutingCache`; wire it into
    the invalidator with key ``f"{org_id}\\x00{agent_id}"`` (the publisher emits
    ``agent_principal_changed:<org_id>\\x00<agent_id>``).
    """
    from surogates.runtime.channel_routing_cache import ChannelRoutingCache

    def _key(org_id: object, agent_id: str) -> str:
        return f"{org_id}\x00{agent_id}"

    async def _loader(key: str) -> ServiceAccountPrincipal | None:
        org_id_raw, agent_id = key.split("\x00", 1)
        try:
            org_id = UUID(org_id_raw)
        except ValueError:
            return None
        async with session_factory() as db:
            row = (
                await db.execute(
                    select(ServiceAccount).where(
                        ServiceAccount.org_id == org_id,
                        ServiceAccount.agent_id == agent_id,
                        ServiceAccount.revoked_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return ServiceAccountPrincipal(id=row.id, org_id=row.org_id, name=row.name)

    cache = ChannelRoutingCache(
        loader=_loader, ttl_seconds=ttl_seconds, max_entries=max_entries,
    )

    async def _resolver(org_id: object, agent_id: str) -> ServiceAccountPrincipal | None:
        return await cache.get(_key(org_id, agent_id))

    _resolver.cache = cache
    return _resolver
