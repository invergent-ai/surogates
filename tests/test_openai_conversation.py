"""Conversation keying and reconciliation.

The scenarios here are the ones a real OpenAI client produces and a naive
stateful mapping gets wrong: regenerate, edit-and-resend, branch, trim. Each
was measured against the harness's real context replay before this module
existed — a session that is merely appended to accumulates the stale turns and
the agent answers with them still in context.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from surogates.channels.openai_conversation import (
    ConversationScope,
    ReconcileAction,
    conversation_key,
    idempotency_key_for,
    reconcile,
    resolves_to_existing_session,
    session_user_turns,
)
from surogates.channels.openai_shape import Turn

U1, A1 = "What is the capital of Romania?", "Bucharest."
U2, A2 = "And its population?", "About 1.7 million."


def ev(etype: str, data: dict, i: int = 0):
    return SimpleNamespace(id=i, type=etype, data=data)


def user_ev(text: str, **extra):
    return ev("user.message", {"content": text, **extra})


def assistant_ev(text: str):
    return ev("llm.response", {"message": {"role": "assistant", "content": text}})


def turns(*pairs) -> list[Turn]:
    return [Turn(role, content) for role, content in pairs]


# ---------------------------------------------------------------------------
# keying
# ---------------------------------------------------------------------------

SCOPE_A = ConversationScope(service_account_id="sa-1", end_user="alice")
SCOPE_B = ConversationScope(service_account_id="sa-1", end_user="bob")
SCOPE_KEYLESS = ConversationScope(service_account_id="sa-1")
SCOPE_OTHER_KEY = ConversationScope(service_account_id="sa-2")


def test_the_key_is_stable_across_assistant_text_drift():
    """The client owns the rendering of the agent's answer; hashing it would
    fork the conversation the moment a client prettifies what was said."""
    base = conversation_key([U1], scope=SCOPE_A)
    assert conversation_key([U1], scope=SCOPE_A) == base
    assert conversation_key([U1, U2], scope=SCOPE_A) != base


def test_the_key_ignores_whitespace_the_client_re_renders():
    assert conversation_key(["hello  world"], scope=SCOPE_A) == conversation_key(
        ["hello world\n"], scope=SCOPE_A,
    )


def test_different_conversations_get_different_keys():
    assert conversation_key(["a"], scope=SCOPE_A) != conversation_key(["b"], scope=SCOPE_A)
    assert conversation_key(["a", "b"], scope=SCOPE_A) != conversation_key(
        ["b", "a"], scope=SCOPE_A,
    )


def test_two_end_users_behind_one_key_never_collide():
    """The bug this scoping exists for: two end users of one integration both
    opening with the same words would otherwise share a session, and each
    would see the other's turns in context."""
    assert conversation_key(["Hi"], scope=SCOPE_A) != conversation_key(
        ["Hi"], scope=SCOPE_B,
    )
    assert conversation_key(["Hi", "how are you"], scope=SCOPE_A) != conversation_key(
        ["Hi", "how are you"], scope=SCOPE_B,
    )


def test_two_api_keys_never_collide():
    assert conversation_key(["Hi"], scope=SCOPE_KEYLESS) != conversation_key(
        ["Hi"], scope=SCOPE_OTHER_KEY,
    )


def test_an_explicit_id_wins_over_everything_derived():
    """The only collision-free option, and what the docs recommend."""
    a = conversation_key(["totally"], scope=SCOPE_A, explicit_id="thread-7")
    b = conversation_key(["different"], scope=SCOPE_B, explicit_id="thread-7")
    assert a == b, "the client named the conversation; content must not matter"
    assert a != conversation_key([], scope=SCOPE_A, explicit_id="thread-8")


def test_a_blank_or_oversized_explicit_id_falls_back_to_derivation():
    derived = conversation_key(["q"], scope=SCOPE_A)
    assert conversation_key(["q"], scope=SCOPE_A, explicit_id="   ") == derived
    assert conversation_key(["q"], scope=SCOPE_A, explicit_id="x" * 5000) == derived


def test_a_request_with_no_history_never_resolves_to_an_existing_session():
    """Every opening request in a scope derives the same key, so a lookup
    would hand every new conversation to whichever one ran first."""
    assert resolves_to_existing_session(prior_turns=[]) is False
    assert resolves_to_existing_session(
        prior_turns=turns(("assistant", "preamble")),
    ) is False
    assert resolves_to_existing_session(prior_turns=turns(("user", U1))) is True


def test_an_explicit_id_may_resolve_even_with_no_history():
    assert resolves_to_existing_session(prior_turns=[], explicit_id="thread-7") is True


def test_the_idempotency_key_namespaces_by_agent():
    """``sessions.idempotency_key`` is org-scoped, so two agents in one org
    would otherwise share a conversation."""
    key = conversation_key([U1], scope=SCOPE_A)
    a = idempotency_key_for("agent-a", key)
    b = idempotency_key_for("agent-b", key)
    assert a != b
    assert a.startswith("openai:agent-a:")


def test_the_key_windows_long_conversations_without_erroring():
    long_history = [f"turn-{i}" for i in range(500)]
    assert isinstance(conversation_key(long_history, scope=SCOPE_A), str)
    assert conversation_key(long_history, scope=SCOPE_A) == conversation_key(
        long_history, scope=SCOPE_A,
    )


# ---------------------------------------------------------------------------
# reading a session's user turns
# ---------------------------------------------------------------------------

def test_only_real_user_turns_count():
    """A synthetic turn is not something the caller sent; counting it would
    make every following request look like a rewrite."""
    events = [
        user_ev(U1),
        assistant_ev(A1),
        ev("tool.call", {"name": "web_search"}),
        ev("tool.result", {"content": "..."}),
        user_ev("outcome kickoff", synthetic="outcome_kickoff"),
        user_ev("seeded", synthetic="seed"),
        user_ev(U2),
    ]
    assert session_user_turns(events) == [U1, U2]


def test_session_turns_are_normalised_like_the_caller_side():
    assert session_user_turns([user_ev("  hello   world \n")]) == ["hello world"]


# ---------------------------------------------------------------------------
# reconciliation — the happy path
# ---------------------------------------------------------------------------

def test_no_session_means_create():
    r = reconcile(prior_turns=[], session_events=None)
    assert r.action is ReconcileAction.CREATE


def test_a_first_turn_against_no_session_creates():
    r = reconcile(prior_turns=turns(("user", U1), ("assistant", A1)), session_events=None)
    assert r.action is ReconcileAction.CREATE
    assert len(r.seed_turns) == 2, "history the session never saw must be seeded"


def test_a_clean_append_appends():
    """The normal case: the client added one turn to what it already had."""
    r = reconcile(
        prior_turns=turns(("user", U1), ("assistant", A1)),
        session_events=[user_ev(U1), assistant_ev(A1)],
    )
    assert r.action is ReconcileAction.APPEND
    assert r.seed_turns == []


def test_appending_ignores_the_agents_own_tool_traffic():
    """A tool-using turn puts roles in the session the client never sent; the
    session is a superset and comparing anything but user turns would fork."""
    r = reconcile(
        prior_turns=turns(("user", U1), ("assistant", A1)),
        session_events=[
            user_ev(U1),
            ev("llm.response", {"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1"}]}}),
            ev("tool.result", {"tool_call_id": "c1", "content": "…"}),
            assistant_ev(A1),
        ],
    )
    assert r.action is ReconcileAction.APPEND


def test_a_long_appending_conversation_keeps_appending():
    history, events = [], []
    for i in range(30):
        history += [Turn("user", f"q{i}"), Turn("assistant", f"a{i}")]
        events += [user_ev(f"q{i}"), assistant_ev(f"a{i}")]
    assert reconcile(
        prior_turns=history, session_events=events,
    ).action is ReconcileAction.APPEND


# ---------------------------------------------------------------------------
# reconciliation — the rewrites that break a naive mapping
# ---------------------------------------------------------------------------

def test_regenerate_forks_instead_of_duplicating_the_turn():
    """Client resends the same last user message. Appending would put that
    turn in the log twice with the stale answer between them."""
    r = reconcile(
        prior_turns=turns(("user", U1), ("assistant", A1)),
        session_events=[user_ev(U1), assistant_ev(A1), user_ev(U2), assistant_ev(A2)],
    )
    assert r.action is ReconcileAction.FORK
    assert r.reason == "client_dropped_history"


def test_editing_the_last_message_forks_and_seeds_without_the_stale_turn():
    """The stale question AND its answer are still in the session; appending
    the edit leaves the agent answering with both in context. The fork must
    carry only what the client believes the history to be."""
    r = reconcile(
        prior_turns=turns(("user", U1), ("assistant", A1)),
        session_events=[user_ev(U1), assistant_ev(A1), user_ev(U2), assistant_ev(A2)],
    )
    assert r.action is ReconcileAction.FORK
    seeded = [t.content for t in r.seed_turns]
    assert seeded == [U1, A1]
    assert U2 not in seeded and A2 not in seeded


def test_rewriting_an_earlier_turn_forks():
    r = reconcile(
        prior_turns=turns(("user", "rewritten"), ("assistant", A1)),
        session_events=[user_ev(U1), assistant_ev(A1)],
    )
    assert r.action is ReconcileAction.FORK
    assert r.reason == "client_rewrote_history"


def test_a_client_trimming_its_context_forks():
    """The agent still holds what the client dropped; continuing would answer
    with context the caller believes is gone."""
    r = reconcile(
        prior_turns=turns(("user", U2), ("assistant", A2)),
        session_events=[user_ev(U1), assistant_ev(A1), user_ev(U2), assistant_ev(A2)],
    )
    assert r.action is ReconcileAction.FORK


def test_replaying_history_the_session_never_saw_forks():
    """Appending would leave a hole the agent cannot see and cannot ask about."""
    r = reconcile(
        prior_turns=turns(
            ("user", U1), ("assistant", A1), ("user", U2), ("assistant", A2),
        ),
        session_events=[user_ev(U1), assistant_ev(A1)],
    )
    assert r.action is ReconcileAction.FORK
    assert r.reason == "client_replayed_unseen_history"


def test_a_fork_always_carries_the_callers_full_history_to_seed():
    prior = turns(("user", U1), ("assistant", A1), ("user", U2), ("assistant", A2))
    r = reconcile(prior_turns=prior, session_events=[user_ev("something else")])
    assert r.action is ReconcileAction.FORK
    assert r.seed_turns == prior


@pytest.mark.parametrize("reason", [
    "client_dropped_history",
    "client_replayed_unseen_history",
    "client_rewrote_history",
])
def test_every_fork_names_a_reason(reason):
    """The reason is surfaced to the integrator; a fork with no explanation
    reads as the agent having forgotten."""
    cases = {
        "client_dropped_history": (
            turns(("user", U1)),
            [user_ev(U1), assistant_ev(A1), user_ev(U2)],
        ),
        "client_replayed_unseen_history": (
            turns(("user", U1), ("assistant", A1), ("user", U2)),
            [user_ev(U1)],
        ),
        "client_rewrote_history": (
            turns(("user", "different")),
            [user_ev(U1)],
        ),
    }
    prior, events = cases[reason]
    r = reconcile(prior_turns=prior, session_events=events)
    assert r.action is ReconcileAction.FORK
    assert r.reason == reason


def test_an_empty_session_with_caller_history_forks_rather_than_appending():
    r = reconcile(prior_turns=turns(("user", U1), ("assistant", A1)), session_events=[])
    assert r.action is ReconcileAction.FORK
