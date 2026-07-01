"""The acting principal for a turn: who sent the triggering message.

In a shared channel thread the session row's owner is frozen to the first
poster; the acting principal is the sender of the message that triggered this
wake, resolved from the durable USER_MESSAGE stamp with the session row as a
fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActingPrincipal:
    user_id: UUID | None
    service_account_id: UUID | None


def _parse_uuid(value, *, field: str) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        logger.warning("Ignoring invalid %s on user-message stamp: %r", field, value)
        return None


def resolve_principal_from_event_data(
    data: dict | None, *, fallback: ActingPrincipal
) -> ActingPrincipal:
    """Acting principal from a USER_MESSAGE payload, else *fallback*.

    A stamped user id and a stamped service-account id are mutually exclusive;
    an invalid uuid is treated as unstamped (never a crash).
    """
    data = data or {}
    uid = _parse_uuid(data.get("principal_user_id"), field="principal_user_id")
    if uid is not None:
        return ActingPrincipal(user_id=uid, service_account_id=None)
    sa = _parse_uuid(
        data.get("principal_service_account_id"), field="principal_service_account_id"
    )
    if sa is not None:
        return ActingPrincipal(user_id=None, service_account_id=sa)
    return fallback
