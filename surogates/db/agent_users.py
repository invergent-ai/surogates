"""Enrollment writes for the ``agent_users`` binding table.

One tiny module so every writer — session creation, the web-channel
login paths, and the ops management client — shares the same
idempotent INSERT instead of three hand-rolled variants.
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from surogates.db.models import AgentUser

logger = logging.getLogger(__name__)

# Where a binding came from. Kept as plain strings (not an enum) so a
# future writer can add a source without a schema migration.
SOURCE_MANUAL = "manual"
SOURCE_LOGIN = "login"
SOURCE_SESSION = "session"
SOURCE_BACKFILL = "backfill"

# Idempotent backfill from historical sessions — safe to run on every
# migrate. Interactive channels only: api/service-account traffic has
# no end-user, and subagent rows inherit the parent's attribution.
BACKFILL_SQL = """
INSERT INTO agent_users (id, org_id, agent_id, user_id, source, created_at)
SELECT gen_random_uuid(), s.org_id, s.agent_id, s.user_id,
       'backfill', MIN(s.created_at)
FROM sessions s
WHERE s.user_id IS NOT NULL
  AND s.agent_id IS NOT NULL
  AND s.agent_id <> ''
GROUP BY s.org_id, s.agent_id, s.user_id
ON CONFLICT (org_id, agent_id, user_id) DO NOTHING
"""


async def ensure_agent_user(
    db: AsyncSession,
    *,
    org_id: UUID,
    agent_id: str,
    user_id: UUID,
    source: str,
) -> None:
    """Record that *user_id* belongs to *agent_id* (idempotent).

    First write wins — an existing binding keeps its original source
    and timestamp. Callers commit; this only stages the INSERT so it
    rides in the caller's transaction.
    """
    await db.execute(
        pg_insert(AgentUser)
        .values(
            id=uuid.uuid4(),
            org_id=org_id,
            agent_id=agent_id,
            user_id=user_id,
            source=source,
        )
        .on_conflict_do_nothing(
            constraint="uq_agent_users_org_agent_user",
        )
    )
