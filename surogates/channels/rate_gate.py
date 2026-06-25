"""Per-event rate-limit gate for shared channel adapters.

Wraps :class:`PerTenantRateLimiter`
with the channel-adapter idiom: return True to proceed, False to
drop the event.

The two adapter inbound paths (Slack / Telegram) use this gate
identically.  Tests that don't wire a limiter pass ``None`` and the
gate becomes a pass-through.

Policy: dropping events silently is the safe default for both
adapters.  Telegram's "one-time warning reply" path is rejected
here because the warning path would itself consume Telegram
API quota and could amplify rate-limited tenants into rate-
limited *adapters*.
"""

from __future__ import annotations

from typing import Any

__all__ = ["check_inbound_rate_limit"]


async def check_inbound_rate_limit(
    limiter: Any, routing: dict,
) -> bool:
    """Return True if the inbound event is under the tenant's
    rate-limit budget; False if it must be dropped.

    ``limiter=None`` is a test-only pass-through.
    ``routing`` is the routing dict resolved by the platform webhook
    dispatcher (e.g. ``surogates.channels.platforms.slack`` or
    ``surogates.channels.platforms.telegram``).
    """
    if limiter is None:
        return True
    return await limiter.try_consume(
        org_id=routing["org_id"], agent_id=routing["agent_id"],
    )
