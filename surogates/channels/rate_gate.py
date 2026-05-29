"""Per-event rate-limit gate for shared channel adapters.

Plan 6 / Task 9.  Wraps :class:`PerTenantRateLimiter` (Plan 1b)
with the channel-adapter idiom: return True to proceed, False to
drop the event.

The two adapter inbound paths (Slack / Telegram) use this gate
identically.  Helm-mode pods don't wire a limiter; ``None`` is
treated as 'pass-through' (legacy behaviour preserved).

Policy: dropping events silently is the safe default for both
adapters.  Telegram's "one-time warning reply" path is rejected
here because the warning path would itself consume Telegram
API quota and could amplify rate-limited tenants into rate-
limited *adapters*.  The Plan 6 / Task 13 audit emit surfaces
the drops on the audit dashboard so operators see them.
"""

from __future__ import annotations

from typing import Any

__all__ = ["check_inbound_rate_limit"]


async def check_inbound_rate_limit(
    limiter: Any, routing: dict,
) -> bool:
    """Return True if the inbound event is under the tenant's
    rate-limit budget; False if it must be dropped.

    ``limiter=None`` is helm-mode pass-through.
    ``routing`` is the dict returned by
    :meth:`SharedSlackInbound.resolve` /
    :meth:`SharedTelegramInbound.resolve`.
    """
    if limiter is None:
        return True
    return await limiter.try_consume(
        org_id=routing["org_id"], agent_id=routing["agent_id"],
    )
