"""Conversation-privacy boundary tokens for agent memory isolation.

A channel agent's long-term memory is keyed by the token computed here so a
private conversation never shares memory with another.  Only a Slack *public*
channel collapses to the shared ``public`` token; every other surface — Slack
private channels and DMs, all Telegram chats, unknown platforms, blank ids —
gets an isolated token.  Fail closed: when in doubt, isolate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from surogates.channels.constants import WEBSITE_CHANNEL

__all__ = [
    "MANAGED_CHANNELS",
    "BUYER_BOUNDARY_PREFIX",
    "CASE_AGENT_KEY",
    "CASE_BOUNDARY_PREFIX",
    "CASE_RERUN_KEY",
    "EVAL_BOUNDARY_PREFIX",
    "PROJECT_BOUNDARY_PREFIX",
    "RERUN_KINDS",
    "VISITOR_BOUNDARY_PREFIX",
    "boundary_token",
    "is_case_agent_config",
    "is_case_agent_session",
    "is_case_config",
    "is_case_session",
    "is_visitor_boundary",
    "is_eval_session",
    "rerun_kind_in",
    "rerun_kind_of",
    "session_memory_boundary",
    "visitor_boundary",
]

# Channel platforms whose sessions are memory-partitioned by conversation.
# ``voice`` always persists its boundary at session creation: one call, or one
# caller's number when the agent remembers callers.
MANAGED_CHANNELS: frozenset[str] = frozenset({"slack", "teams", "telegram", "whatsapp", "voice", "discord"})

# Memory-boundary namespace for evaluation sessions, which need a scratch
# partition. Outside the managed channels only this and
# ``PROJECT_BOUNDARY_PREFIX`` are honoured, and the server stamps both:
# letting a caller name any boundary would let it read or overwrite the
# memory of a private conversation on the same agent.
EVAL_BOUNDARY_PREFIX = "eval:"

# A project's sessions share one partition, ``workstream:<project id>``: the
# server stamps it on the master and every thread, children inherit it, and
# the session create route strips it from a client's config.
PROJECT_BOUNDARY_PREFIX = "workstream:"

# A monetized agent's buyers all arrive through the agent's one principal,
# so without their own partition every buyer would share one memory. The
# session route stamps ``buyer:<buyer id>`` only for that principal.
BUYER_BOUNDARY_PREFIX = "buyer:"

# A public website's visitors all arrive anonymously through the agent's
# one website channel, so without their own partition every visitor would
# read and write the agent's shared memory. Each gets ``visitor:<digest>``,
# derived by :func:`visitor_boundary` from who the server knows the visitor
# to be; nothing the browser sends names it directly.
VISITOR_BOUNDARY_PREFIX = "visitor:"

def is_visitor_boundary(boundary: str | None) -> bool:
    """Whether *boundary* is a website visitor's own memory partition."""
    return (boundary or "").startswith(VISITOR_BOUNDARY_PREFIX)


# Every session of a Keel case, the step's own and each child it makes,
# carries ``case:<case id>``: its memory is per agent and case, and the case's
# workspace is shared by every agent working on it.  Only the Keel link stamps
# it; the session create routes strip a client's, and a child copies its
# parent's.
CASE_BOUNDARY_PREFIX = "case:"

# A person's case agent is a session of the case too: it carries the case's
# ``case:`` boundary for its memory alone, never the case's workspace, and
# holds which case and which person under this key.  Only the Keel link
# stamps it; every other create path strips it, and a child never copies it.
CASE_AGENT_KEY = "case_agent"

# A step the Keel link starts for a builder, not for a live case, is a step
# session like any other and holds, under this key, which kind it is: a
# ``rerun`` of an agent over a case it already worked, or a ``trial`` of a
# draft agent on a case's inputs.  The kind decides where its wakes queue
# and what its tools may reach, so it is the Keel link's to stamp: every
# other create path strips it, and a child copies its parent's.
CASE_RERUN_KEY = "case_rerun"
RERUN_KINDS: frozenset[str] = frozenset({"rerun", "trial"})


def boundary_token(
    *,
    platform: str,
    channel_id: str,
    visibility: str,
    source: dict,
    fallback_id: str,
) -> str:
    """Return the memory-boundary token for one inbound conversation.

    ``visibility`` is the coarse privacy (``public`` | ``private`` | ``dm``);
    ``source`` carries raw adapter metadata (``chat_type`` for Telegram);
    ``fallback_id`` is a deterministic isolated id used when ``channel_id`` is
    blank (so two unknown conversations never collide).
    """
    cid = (channel_id or "").strip()

    if platform == "slack":
        if not cid:
            return f"slack:iso:{fallback_id}"
        if visibility == "public":
            return "public"
        if visibility == "dm":
            return f"slack:d:{cid}"
        # private channel, or anything unrecognized → isolated (fail closed)
        return f"slack:c:{cid}"

    if platform == "telegram":
        if not cid:
            return f"telegram:iso:{fallback_id}"
        chat_type = str((source or {}).get("chat_type", ""))
        if visibility == "dm" or chat_type == "private":
            return f"tg:d:{cid}"
        if chat_type == "channel":
            return f"tg:c:{cid}"
        if chat_type in ("group", "supergroup"):
            return f"tg:g:{cid}"
        return f"telegram:iso:{fallback_id}"  # unknown telegram surface → isolated

    if platform == "discord":
        # Server channels never collapse to ``public``: the bot may read a
        # channel that is open to the whole server or to one role only, and
        # nothing in the message says which.
        if not cid:
            return f"discord:iso:{fallback_id}"
        return f"discord:d:{cid}" if visibility == "dm" else f"discord:c:{cid}"

    # Unknown platform → fail closed, isolated.
    return f"{platform}:iso:{fallback_id}"


def visitor_boundary(agent_id: str, embed_end_user_id: str | None, identity: str) -> str:
    """The memory boundary of one website visitor of *agent_id*.

    *identity* is the most durable thing the server knows the visitor by: a
    verified Firebase uid, else the visitor's own cookie, else the session
    id. *embed_end_user_id* keeps a buyer's resold embed apart from the
    operator's own. Hashed so the boundary, an object-key segment, is a
    fixed alphabet and gives away none of its inputs.
    """
    seed = f"{agent_id}:{embed_end_user_id or ''}:{identity}"
    return VISITOR_BOUNDARY_PREFIX + hashlib.sha256(seed.encode()).hexdigest()[:32]


def is_eval_session(session: object) -> bool:
    """True when this session is one row of an evaluation run.

    Keyed on the server-stamped ``memory_boundary``, which is unforgeable:
    ``apply_eval_isolation`` strips any client-supplied boundary before
    deciding whether to stamp its own.  This prevents a client from
    supplying ``eval_partition_id`` in config and claiming evaluation
    treatment for a session the server never isolated.
    """
    cfg = getattr(session, "config", None) or {}
    boundary = str(cfg.get("memory_boundary") or "").strip()
    return boundary.startswith(EVAL_BOUNDARY_PREFIX)


def is_case_config(config: Mapping | None) -> bool:
    """True when a session's config carries a case's ``case:`` memory boundary.

    Tools hold only the session's config; anything that is not a mapping
    is no case.
    """
    if not isinstance(config, Mapping):
        return False
    boundary = str(config.get("memory_boundary") or "").strip()
    return boundary.startswith(CASE_BOUNDARY_PREFIX)


def is_case_session(session: object) -> bool:
    """True when this session works on a Keel case: a step's own session, or a child of one.

    Keyed on the server-stamped boundary, as :func:`is_eval_session` is, so
    the children a step makes are case sessions too.
    """
    return is_case_config(getattr(session, "config", None))


def is_case_agent_config(config: Mapping | None) -> bool:
    """True when a session's config is a person's case agent's: a case's
    config (:func:`is_case_config`) holding a mapping under :data:`CASE_AGENT_KEY`.

    Tools hold only the session's config; anything that is not a mapping
    is no case agent.
    """
    return is_case_config(config) and isinstance((config or {}).get(CASE_AGENT_KEY), Mapping)


def is_case_agent_session(session: object) -> bool:
    """True when this session is a person's case agent, by its config alone (:func:`is_case_agent_config`)."""
    return is_case_agent_config(getattr(session, "config", None))


def rerun_kind_in(config: Mapping | None) -> str | None:
    """The kind of rerun a session's config makes it, or ``None``: a case's config (:func:`is_case_config`)
    holding a mapping under :data:`CASE_RERUN_KEY` whose ``kind`` is one of :data:`RERUN_KINDS`.

    Tools hold only the session's config; anything that is not a mapping is no rerun.
    """
    if not is_case_config(config):
        return None
    marked = (config or {}).get(CASE_RERUN_KEY)
    kind = marked.get("kind") if isinstance(marked, Mapping) else None
    return kind if isinstance(kind, str) and kind in RERUN_KINDS else None


def rerun_kind_of(session: object) -> str | None:
    """The kind of rerun *session* is, or of the rerun it works for, by its config alone (:func:`rerun_kind_in`)."""
    return rerun_kind_in(getattr(session, "config", None))


def _legacy_boundary_fallback_id(session: object, cfg: dict) -> str:
    return str(cfg.get("channel_session_key") or getattr(session, "id", ""))


def session_memory_boundary(session: object) -> str | None:
    """Memory boundary for a session, or ``None`` to keep the per-user layout.

    Managed-channel sessions key memory per conversation: the persisted
    ``config["memory_boundary"]`` when present, otherwise a fail-closed legacy
    boundary.  Only older Slack rows with a confident public channel id
    (``C...``) collapse to ``public``; every other older row is isolated by
    ``channel_session_key`` or ``session.id``.  Every non-channel session
    returns ``None`` so the caller keeps today's per-user / shared memory,
    except an evaluation session, which carries an ``eval:`` boundary stamped
    by the session route, a project's session, which carries its
    project's ``workstream:`` boundary, a buyer's session, which carries
    its ``buyer:`` boundary, and a case's session, which carries its case's
    ``case:`` boundary.

    A website session always has a ``visitor:`` boundary: the one bootstrap
    stamped, or, for a session that has none, one of its own session, so an
    anonymous visitor never reaches the agent's shared memory.
    """
    channel = getattr(session, "channel", None)
    cfg = getattr(session, "config", None) or {}
    persisted = str(cfg.get("memory_boundary") or "").strip()
    if channel == WEBSITE_CHANNEL:
        if persisted.startswith(VISITOR_BOUNDARY_PREFIX):
            return persisted
        return visitor_boundary(
            str(getattr(session, "agent_id", "") or ""),
            cfg.get("embed_end_user_id"),
            str(getattr(session, "id", "")),
        )
    if channel not in MANAGED_CHANNELS:
        if is_eval_session(session) or persisted.startswith(
            (PROJECT_BOUNDARY_PREFIX, BUYER_BOUNDARY_PREFIX, CASE_BOUNDARY_PREFIX)
        ):
            return persisted
        return None
    if persisted:
        return persisted

    fallback_id = _legacy_boundary_fallback_id(session, cfg)
    if channel == "slack":
        slack_channel_id = str(cfg.get("slack_channel_id") or "").strip()
        if slack_channel_id.startswith("C"):
            return "public"
        return f"slack:iso:{fallback_id}"
    if channel == "telegram":
        return f"telegram:iso:{fallback_id}"
    return f"{channel}:iso:{fallback_id}"
