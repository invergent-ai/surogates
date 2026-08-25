"""Mapping a stateless chat completion onto a stateful agent session.

The protocol is stateless — the client resends its whole ``messages`` array
every call.  An agent is not: a session carries memory, a workspace, an
optional live browser, compaction state.  Throwing that away per request would
serve a far worse agent than every other channel does, so the default is to
keep one session per conversation and append to it.

Two problems have to be solved for that to be correct.

**Which session is this?**  Chat completions carry no conversation id, so one
has to be derived — and a key derived from message content alone collides.
Two end users behind the same API key who both open with "Hi" produce the same
key, and their conversations merge.  So the key is always scoped by the caller
(see :class:`ConversationScope`), an explicit ``X-Surogate-Conversation``
header always wins, and a request carrying no prior turns never resolves to an
existing session at all — there is no history to continue.

**Did the client rewrite history?**  Appending is only sound while the client
appends.  Real clients also regenerate, edit the last message, branch, and trim
their own context, and an append-only event log silently accumulates the
debris:

    client regenerates       -> the same user turn twice, stale answer between
    client edits and resends -> the stale question AND its answer still present
    client trims its window  -> the agent still holds what the client dropped

So every request is reconciled against the session's own transcript.  A clean
prefix appends; anything else forks a fresh session seeded from the caller's
history.  Divergence is normal traffic, not an error.

Pure: this module reads events and returns a decision.  Nothing here touches
the database.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Sequence

from surogates.channels.openai_shape import Turn, normalise_text

__all__ = [
    "CONVERSATION_HEADER",
    "CONVERSATION_KEY_PREFIX",
    "ConversationScope",
    "Reconciliation",
    "ReconcileAction",
    "conversation_key",
    "idempotency_key_for",
    "reconcile",
    "resolves_to_existing_session",
    "session_user_turns",
]

#: Header a client may send to name its conversation explicitly.  This is the
#: only collision-free option and the one the docs recommend for any
#: integration serving more than one end user behind a single API key.
CONVERSATION_HEADER = "x-surogate-conversation"

#: Namespace for the value stored in ``sessions.idempotency_key``.  That column
#: carries a partial unique index on ``(org_id, idempotency_key)``, giving an
#: O(1) conversation lookup and a race guard for free — two concurrent first
#: requests on one conversation collide in the database rather than producing
#: two sessions.  The column is ORG-scoped, so the agent id is part of the key.
CONVERSATION_KEY_PREFIX = "openai"

#: How many trailing user turns take part in a derived key.  Bounded so the
#: hashed blob cannot grow without limit on a long conversation; the full
#: transcript is still compared by :func:`reconcile`, which is what actually
#: decides whether the session may be continued.
_KEY_WINDOW = 64

_MAX_EXPLICIT_ID = 200


@dataclass(frozen=True, slots=True)
class ConversationScope:
    """Who is asking — the namespace a derived key is confined to.

    Without this a derived key is global to the agent, and two end users
    served by one integration collide as soon as they say the same thing.

    * ``service_account_id`` — always known; the API key making the call.
      Confines collisions to a single customer's own traffic.
    * ``end_user`` — the caller's ``user`` field, when supplied.  This is what
      makes a multi-tenant integration correct: two of its end users can hold
      character-identical conversations without ever meeting.
    """

    service_account_id: str
    end_user: str | None = None


def _explicit_id(raw: str | None) -> str | None:
    """Normalise a client-supplied conversation id, or ``None``.

    Length-capped and hashed rather than used verbatim: the value reaches a
    unique index and appears in logs, and a caller should not be able to
    choose an unbounded key or collide with the derived namespace by crafting
    one that looks like a hash.
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned or len(cleaned) > _MAX_EXPLICIT_ID:
        return None
    return "x" + hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:31]


def conversation_key(
    user_turns: Sequence[str],
    *,
    scope: ConversationScope | None = None,
    explicit_id: str | None = None,
) -> str:
    """A key for the conversation state a request is replying to.

    An *explicit_id* short-circuits everything — it is the client telling us
    which conversation this is, which no derivation can beat.

    Otherwise the key is derived from the USER turns only, whitespace-
    normalised, inside the caller's scope.  Assistant text is deliberately
    excluded: it is the half the client re-renders between turns (trailing
    newlines, CRLF, smart quotes, "edited" markers), and hashing it would fork
    a conversation the moment a client prettified what the agent said.
    """
    resolved = _explicit_id(explicit_id)
    if resolved is not None:
        return resolved

    window = [normalise_text(t) for t in user_turns[-_KEY_WINDOW:]]
    payload = {
        "sa": scope.service_account_id if scope else "",
        "eu": (scope.end_user if scope else None) or "",
        "turns": window,
    }
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def resolves_to_existing_session(
    *, prior_turns: Sequence[Turn], explicit_id: str | None = None,
) -> bool:
    """Whether this request may continue an existing session at all.

    A request whose history is empty has nothing to continue, and every such
    request in a scope would derive the SAME key — so looking one up would
    hand every new conversation to whichever ran first.  An explicit id is
    exempt: the client has named the conversation, and a fresh one under a
    reused id is the client's own doing.
    """
    if _explicit_id(explicit_id) is not None:
        return True
    return any(t.role == "user" for t in prior_turns)


def idempotency_key_for(agent_id: str, key: str) -> str:
    """The value stored in ``sessions.idempotency_key`` for a conversation."""
    return f"{CONVERSATION_KEY_PREFIX}:{agent_id}:{key}"


def session_user_turns(events: Iterable[Any]) -> list[str]:
    """The user turns a session already holds, oldest first, normalised.

    Reads ``user.message`` events and skips the ones the agent generated for
    itself.  A synthetic turn (an outcome kickoff, a seeded transcript entry,
    a scheduled wake) is not something the caller ever sent, so counting it
    would make every following request look like a rewrite and fork the
    conversation on every turn.
    """
    turns: list[str] = []
    for event in events:
        if getattr(event, "type", None) != "user.message":
            continue
        data = getattr(event, "data", None) or {}
        if data.get("synthetic"):
            continue
        turns.append(normalise_text(data.get("content") or ""))
    return turns


class ReconcileAction(str, Enum):
    """What a request does to the session it resolved to."""

    #: No session resolved — create one and run the turn.
    CREATE = "create"
    #: The client appended; the session's transcript matches the caller's.
    APPEND = "append"
    #: The client rewrote history; fork a session seeded from what it sent.
    FORK = "fork"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    action: ReconcileAction
    #: Turns to seed into a created or forked session, oldest first.
    seed_turns: list[Turn]
    #: Why a fork was chosen.  Logged and returned in a response header, so an
    #: integrator can see that their client rewrote the conversation rather
    #: than wondering why the agent appears to have forgotten.
    reason: str | None = None


def reconcile(
    *,
    prior_turns: Sequence[Turn],
    session_events: Iterable[Any] | None,
    pinned: bool = False,
) -> Reconciliation:
    """Decide how this request relates to the session it resolved to.

    *prior_turns* is the caller's history excluding the turn being run;
    *session_events* is the resolved session's event log, or ``None`` when no
    session was found.  *pinned* means the client named the conversation with
    an explicit id rather than having one derived from its messages.

    The comparison is over user turns only, for the same reason the key is:
    assistant text is the client's to re-render, and tool turns exist only on
    the session side — the session is a strict superset of what the client
    can represent.
    """
    caller = [normalise_text(t.content) for t in prior_turns if t.role == "user"]

    if session_events is None:
        return Reconciliation(
            action=ReconcileAction.CREATE, seed_turns=list(prior_turns),
        )

    if pinned and not caller:
        # A named conversation with no history attached is a client that
        # keeps no transcript of its own and expects the server to hold it —
        # the whole point of sending the header.  Treating the absent history
        # as "the client dropped everything" would fork a fresh session on
        # every single turn.
        return Reconciliation(action=ReconcileAction.APPEND, seed_turns=[])

    existing = session_user_turns(session_events)

    if existing == caller:
        return Reconciliation(action=ReconcileAction.APPEND, seed_turns=[])

    if len(existing) > len(caller):
        return Reconciliation(
            action=ReconcileAction.FORK,
            seed_turns=list(prior_turns),
            reason="client_dropped_history",
        )

    if existing == caller[: len(existing)]:
        # The caller carries turns the session never saw — a client that ran
        # part of the conversation elsewhere and is now replaying it.  Forking
        # keeps the agent's context equal to what the caller believes it is;
        # appending would leave a hole the agent cannot see or ask about.
        return Reconciliation(
            action=ReconcileAction.FORK,
            seed_turns=list(prior_turns),
            reason="client_replayed_unseen_history",
        )

    return Reconciliation(
        action=ReconcileAction.FORK,
        seed_turns=list(prior_turns),
        reason="client_rewrote_history",
    )
