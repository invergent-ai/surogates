"""Session source identification and deterministic routing keys.

A :class:`SessionSource` captures *where* a message came from -- the
platform, chat, user, and optional thread.  It is frozen (immutable) and
travels through the entire request pipeline unchanged so that any layer
can inspect the origin without consulting external state.

:func:`build_session_key` converts a ``SessionSource`` into a
deterministic string key used to map inbound messages to Surogates
sessions.  The key format is designed to be human-readable in logs and
Redis while remaining collision-free across platforms.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "SessionSource",
    "build_session_key",
]


@dataclass(frozen=True)
class SessionSource:
    """Immutable origin descriptor for an inbound message.

    Constructed once by the receiving channel adapter and threaded through
    the entire processing pipeline (session resolution, harness dispatch,
    delivery fan-out) without mutation.
    """

    platform: str
    """Canonical platform name: ``'web'``, ``'slack'``, ``'teams'``, ``'telegram'``."""

    chat_id: str
    """Platform-specific chat / conversation identifier."""

    chat_type: str
    """One of ``'dm'``, ``'group'``, ``'channel'``, ``'thread'``."""

    user_id: str
    """Platform-specific user identifier (not the Surogates UUID)."""

    user_name: str | None = None
    """Human-readable display name, if the platform provides one."""

    thread_id: str | None = None
    """Platform thread / reply-chain identifier, when applicable."""

    chat_name: str | None = None
    """Human-readable chat / channel name, when available."""


def build_session_key(
    source: SessionSource,
    *,
    per_user_groups: bool = False,
    single_session: bool = False,
) -> str:
    """Derive a deterministic routing key from a :class:`SessionSource`.

    The routing key decides which Surogates session an inbound message is
    directed to.  Rules:

    * **DMs** -- one session per user per platform.
    * **Groups / channels** -- by default a single shared session per
      chat.  Set *per_user_groups* to ``True`` to give each user their
      own session within the same group chat.
    * **Threads** -- appended to the parent key so that each thread maps
      to its own session.

    *single_session* implements the agent's "multi session" capability
    being off: the key collapses to one session per chat — thread
    suffixes are dropped (every thread continues the same conversation)
    and *per_user_groups* is ignored (a per-user split inside a group
    would mint extra sessions).  DMs already map one-to-one to the user,
    so their key shape is unchanged.

    Returns a colon-separated string suitable for use as a Redis key or
    database lookup value, e.g.::

        agent:slack:dm:U12345
        agent:teams:group:CONV_ID:USER_ID
        agent:telegram:group:CHAT_ID:THREAD_ID
    """
    parts: list[str] = ["agent", source.platform, source.chat_type, source.chat_id]

    if source.chat_type in ("group", "channel") and per_user_groups and not single_session:
        parts.append(source.user_id)

    if source.thread_id and not single_session:
        parts.append(source.thread_id)

    return ":".join(parts)
