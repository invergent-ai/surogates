"""Artifacts created by a delegated child must surface on the parent.

The artifact spec is stored under the *child's* session prefix in S3
(``{child_session_id}/_artifacts/...``).  The SDK fetches it via
``GET /api/sessions/{session_id}/artifacts/{artifact_id}`` -- which
session id it uses determines whether the spec resolves at all.

Without propagation, ``artifact.created`` events fire only on the
writer's session, never on the planner's or the root's, so the
user's chat thread (rooted at the base agent's session) never shows
the report card.

These tests pin the contract that ``_run_single_delegation`` re-emits
``artifact.created`` on the parent with ``originating_session_id``
set to the session that actually owns the spec.  Propagation
cascades naturally up the chain because the parent's own
``delegate_task`` handler runs the same scan on the events it gets
back from THIS session, preserving the original
``originating_session_id`` rather than rewriting it to the
intermediate session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

import surogates.tools.builtin.delegate as delegate_module
from surogates.harness.budget import IterationBudget
from surogates.session.events import EventType
from surogates.session.models import Event, Session


pytestmark = pytest.mark.asyncio


def _parent_session() -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        id=uuid4(),
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="agent-A",
        channel="web",
        status="active",
        config={},
        created_at=now,
        updated_at=now,
    )


class _CapturingStore:
    """Captures emit_event calls on the parent session."""

    def __init__(self, parent: Session, child_events: list[Event]) -> None:
        self._parent = parent
        self._child_events = child_events
        self._child_id: Any = None
        self.parent_emitted: list[tuple[EventType, dict[str, Any]]] = []

    def set_child_id(self, child_id: Any) -> None:
        self._child_id = child_id
        for ev in self._child_events:
            ev.session_id = child_id

    async def get_session(self, session_id: Any) -> Session:
        if session_id == self._parent.id:
            return self._parent
        raise KeyError(session_id)

    async def emit_event(
        self, session_id: Any, event_type: EventType, data: dict[str, Any],
    ) -> int:
        if session_id == self._parent.id:
            self.parent_emitted.append((event_type, dict(data)))
        return len(self.parent_emitted)

    async def get_events(self, session_id: Any) -> list[Event]:
        return list(self._child_events)


def _install_stub_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    import surogates.harness.agent_resolver as resolver_module

    class _AgentDef:
        tools = None
        disallowed_tools = None
        model = None
        max_iterations = None
        policy_profile = None

    async def _stub(name, tenant, *, session_factory=None, bundle=None):  # noqa: ARG001
        return _AgentDef()

    monkeypatch.setattr(resolver_module, "resolve_agent_by_name", _stub)


def _install_stub_child_session(monkeypatch: pytest.MonkeyPatch, store: _CapturingStore) -> None:
    from surogates.session import provisioning as provisioning_module

    async def _stub(*, store, parent, channel, model, config, **_kwargs):
        child_id = uuid4()
        store.set_child_id(child_id)
        return Session(
            id=child_id,
            user_id=parent.user_id,
            org_id=parent.org_id,
            agent_id=parent.agent_id,
            channel=channel,
            status="active",
            model=model or parent.model,
            config={**(parent.config or {}), **(config or {})},
            parent_id=parent.id,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(provisioning_module, "create_child_session", _stub)


def _event(session_id: Any, etype: EventType, data: dict[str, Any]) -> Event:
    return Event(
        session_id=session_id,
        type=etype.value,
        data=data,
        created_at=datetime.now(timezone.utc),
    )


def _completing_child_events(*extra: Event) -> list[Event]:
    """Minimal child event log that exits the poll loop cleanly."""
    placeholder = uuid4()
    return [
        *extra,
        _event(placeholder, EventType.LLM_RESPONSE, {
            "message": {"role": "assistant", "content": "done"},
        }),
        _event(placeholder, EventType.SESSION_COMPLETE, {}),
    ]


async def test_artifact_created_propagates_to_parent_with_originating_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that creates an artifact triggers a propagated
    ``artifact.created`` event on the parent carrying the child's
    session id under ``originating_session_id``.  The SDK uses that
    id to fetch the spec from the right session prefix in S3."""
    parent = _parent_session()
    placeholder = uuid4()
    child_artifact_event = _event(
        placeholder, EventType.ARTIFACT_CREATED,
        {
            "artifact_id": "art-1",
            "name": "Report",
            "kind": "markdown",
            "version": 1,
            "size": 1234,
        },
    )
    store = _CapturingStore(
        parent, _completing_child_events(child_artifact_event),
    )
    _install_stub_resolver(monkeypatch)
    _install_stub_child_session(monkeypatch, store)
    monkeypatch.setattr(
        "surogates.tools.builtin.delegate.enqueue_session", AsyncMock(),
    )
    monkeypatch.setattr(
        delegate_module, "_POLL_INTERVAL_SECONDS", 0.01,
    )

    await delegate_module._delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    propagated = [
        d for (et, d) in store.parent_emitted
        if et == EventType.ARTIFACT_CREATED
    ]
    assert len(propagated) == 1, (
        "expected exactly one artifact.created propagated to parent; "
        f"got {len(propagated)} (parent emitted: "
        f"{[et.value for et, _ in store.parent_emitted]})"
    )
    payload = propagated[0]
    assert payload["artifact_id"] == "art-1"
    assert payload["name"] == "Report"
    assert payload["kind"] == "markdown"
    # The critical field: SDK uses this to fetch the spec from the
    # session that owns the S3 prefix.
    assert "originating_session_id" in payload
    assert payload["originating_session_id"]  # non-empty


async def test_propagation_preserves_originating_session_from_grandchild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the child has ALREADY propagated an artifact from its own
    descendant (the deep-research case: writer -> planner ->
    base agent), the intermediate session must NOT overwrite the
    ``originating_session_id`` -- the artifact still lives under the
    grandchild's S3 prefix, not the intermediate's."""
    parent = _parent_session()
    placeholder = uuid4()
    grandchild_session_id = str(uuid4())
    # The child's events show an artifact event that itself carries
    # an ``originating_session_id`` -- meaning the child already
    # propagated this artifact from its own descendant.
    propagated_artifact = _event(
        placeholder, EventType.ARTIFACT_CREATED,
        {
            "artifact_id": "art-1",
            "name": "Report",
            "kind": "markdown",
            "version": 1,
            "originating_session_id": grandchild_session_id,
        },
    )
    store = _CapturingStore(
        parent, _completing_child_events(propagated_artifact),
    )
    _install_stub_resolver(monkeypatch)
    _install_stub_child_session(monkeypatch, store)
    monkeypatch.setattr(
        "surogates.tools.builtin.delegate.enqueue_session", AsyncMock(),
    )
    monkeypatch.setattr(
        delegate_module, "_POLL_INTERVAL_SECONDS", 0.01,
    )

    await delegate_module._delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    propagated = [
        d for (et, d) in store.parent_emitted
        if et == EventType.ARTIFACT_CREATED
    ]
    assert len(propagated) == 1
    # The grandchild's id, not the intermediate child's, survives the
    # second propagation hop.
    assert propagated[0]["originating_session_id"] == grandchild_session_id


async def test_no_artifact_no_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delegation that produces no artifact must not emit an
    artifact.created on the parent (sanity check that the scan
    doesn't false-positive on unrelated event types)."""
    parent = _parent_session()
    store = _CapturingStore(parent, _completing_child_events())
    _install_stub_resolver(monkeypatch)
    _install_stub_child_session(monkeypatch, store)
    monkeypatch.setattr(
        "surogates.tools.builtin.delegate.enqueue_session", AsyncMock(),
    )
    monkeypatch.setattr(
        delegate_module, "_POLL_INTERVAL_SECONDS", 0.01,
    )

    await delegate_module._delegate_handler(
        {"goal": "x", "agent_type": "engineer"},
        session_store=store,
        redis=None,
        tenant=object(),
        session_id=str(parent.id),
        budget=IterationBudget(max_total=10),
    )

    propagated = [
        d for (et, _d) in store.parent_emitted
        if et == EventType.ARTIFACT_CREATED
    ]
    assert propagated == []
