"""Retiring inbox notifications that no surface can clear.

A ``task_complete`` item is cleared by opening its session's
conversation, so one raised for a session with no conversation — a
subagent, an API run, a messaging thread that already carried the answer
— stays pending forever. The emission rule in
:func:`surogates.session.inbox_payload.raises_completion_inbox_item` now
refuses to create them, but the rows already in the table outlive that
fix and keep every list unreadable.

The predicate below is exactly the negation of that rule, which is what
makes the statement safe to run on every migrate: once the backlog is
drained there is nothing left for it to match, because nothing new of
that shape is ever written.
"""

from __future__ import annotations

from surogates.channels.constants import INBOX_NOTIFY_CHANNELS

_NOTIFY_CHANNELS_SQL = ", ".join(
    sorted(repr(channel) for channel in INBOX_NOTIFY_CHANNELS)
)

# ``expired`` rather than ``acknowledged``: nobody ever saw these, and
# expired is the status the inbox already uses for "no longer actionable,
# hidden from every view that does not ask for it by name" — the same one
# the delete action writes.
#
# Scheduled runs are excluded to match the emission rule: they are
# unwatched work by construction and their announcement is the only one
# they get. ``progress_checkin`` is excluded because it is still raised
# for any session that opts into it — retiring those would fight the
# opt-in on every migrate instead of settling.
RETIRE_UNREACHABLE_INBOX_SQL = f"""
UPDATE inbox_items AS i
SET status = 'expired',
    updated_at = now()
FROM sessions AS s
WHERE s.id = i.session_id
  AND i.status = 'pending'
  AND i.kind = 'task_complete'
  AND NOT (
        s.channel = 'scheduled'
     OR s.config ->> 'scheduled_session_id' IS NOT NULL
     OR (s.parent_id IS NULL AND s.channel IN ({_NOTIFY_CHANNELS_SQL}))
  )
"""
