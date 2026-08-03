"""Approval grants: durable, scoped, expiring, and spent atomically.

Approving a blocked tool call used to emit a `[governance decision] APPROVE`
user message and nothing else — no record, no re-dispatch, nothing the gate
could consult. A grant is the durable form of that decision.

Validity is recomputed on every read from an explicit reason list rather than
stored as a flag, so a grant cannot drift into looking live after it expires.
"""

from __future__ import annotations


import pytest

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ARGS = {"command": "deploy prod"}


async def _session(session_store, session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    s = await session_store.create_session(
        org_id=org_id, user_id=user_id, agent_id="agent-1", channel="web",
    )
    return s, user_id


async def _mint(session_store, s, user_id, **kw):
    return await session_store.mint_approval_grant(
        session_id=s.id, org_id=s.org_id, tool_name="terminal",
        arguments=_ARGS, granted_by=user_id, **kw,
    )


async def test_a_granted_call_is_allowed_once(session_store, session_factory):
    s, user_id = await _session(session_store, session_factory)
    await _mint(session_store, s, user_id)

    ok, reasons = await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS,
    )
    assert ok is True and reasons == []


async def test_a_single_use_grant_is_spent(session_store, session_factory):
    s, user_id = await _session(session_store, session_factory)
    await _mint(session_store, s, user_id)

    await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS,
    )
    ok, reasons = await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS,
    )
    assert ok is False and "exhausted" in reasons


async def test_different_arguments_are_a_different_call(
    session_store, session_factory,
):
    """The scope is the call, not the tool -- approving one deploy must not
    approve every future deploy."""
    s, user_id = await _session(session_store, session_factory)
    await _mint(session_store, s, user_id)

    ok, reasons = await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal",
        arguments={"command": "deploy staging"},
    )
    assert ok is False and "no_grant" in reasons


async def test_argument_order_does_not_matter(session_store, session_factory):
    s, user_id = await _session(session_store, session_factory)
    await session_store.mint_approval_grant(
        session_id=s.id, org_id=s.org_id, tool_name="terminal",
        arguments={"a": 1, "b": 2}, granted_by=user_id,
    )
    ok, _ = await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments={"b": 2, "a": 1},
    )
    assert ok is True


async def test_another_session_cannot_use_the_grant(
    session_store, session_factory,
):
    s, user_id = await _session(session_store, session_factory)
    other, _ = await _session(session_store, session_factory)
    await _mint(session_store, s, user_id)

    ok, reasons = await session_store.consume_approval_grant(
        session_id=other.id, tool_name="terminal", arguments=_ARGS,
    )
    assert ok is False and "no_grant" in reasons


async def test_an_expired_grant_says_so(session_store, session_factory):
    s, user_id = await _session(session_store, session_factory)
    await _mint(session_store, s, user_id, ttl_seconds=-1)

    ok, reasons = await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS,
    )
    assert ok is False and "expired" in reasons


async def test_a_revoked_grant_says_so(session_store, session_factory):
    s, user_id = await _session(session_store, session_factory)
    grant_id = await _mint(session_store, s, user_id)
    await session_store.revoke_approval_grant(grant_id)

    ok, reasons = await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS,
    )
    assert ok is False and "revoked" in reasons


async def test_a_multi_use_grant_spends_down(session_store, session_factory):
    s, user_id = await _session(session_store, session_factory)
    await _mint(session_store, s, user_id, max_uses=2)

    assert (await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS))[0] is True
    assert (await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS))[0] is True
    ok, reasons = await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS,
    )
    assert ok is False and "exhausted" in reasons


async def test_concurrent_consumers_cannot_overspend(
    session_store, session_factory,
):
    """Two tool calls racing on one single-use grant must not both proceed.

    The plan flagged this: `max_uses` has to be spent atomically or parallel
    calls both see a live grant.
    """
    import asyncio

    s, user_id = await _session(session_store, session_factory)
    await _mint(session_store, s, user_id)

    results = await asyncio.gather(*[
        session_store.consume_approval_grant(
            session_id=s.id, tool_name="terminal", arguments=_ARGS,
        )
        for _ in range(5)
    ])
    assert sum(1 for ok, _ in results if ok) == 1


async def test_no_grant_at_all_is_reported(session_store, session_factory):
    s, _ = await _session(session_store, session_factory)
    ok, reasons = await session_store.consume_approval_grant(
        session_id=s.id, tool_name="terminal", arguments=_ARGS,
    )
    assert ok is False and reasons == ["no_grant"]
