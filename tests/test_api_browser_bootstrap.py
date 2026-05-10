"""Tests for API-side browser dependency bootstrap."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from surogates.api.app import _install_browser_api_dependencies
from surogates.browser.control import BrowserControlStore
from surogates.browser.resolver import BrowserResolver
from surogates.session.events import EventType


class FakeRedis:
    def __init__(self) -> None:
        self.zadds: list[tuple[str, dict[str, float]]] = []

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.zadds.append((key, mapping))


class FakeSessionStore:
    def __init__(self) -> None:
        self.events: list[tuple[UUID, EventType, dict]] = []

    async def emit_event(
        self,
        session_id: UUID,
        event_type: EventType,
        data: dict,
    ) -> None:
        self.events.append((session_id, event_type, data))


@pytest.mark.asyncio
async def test_install_browser_api_dependencies_sets_state_and_callables() -> None:
    redis = FakeRedis()
    store = FakeSessionStore()
    app = SimpleNamespace(state=SimpleNamespace(redis=redis, session_store=store))
    settings = SimpleNamespace(
        agent_id="agent-1",
        browser=SimpleNamespace(
            backend="process",
            k8s_namespace="test-ns",
            k8s_service_account="browser-sa",
            pod_ready_timeout=1,
            image="browser:test",
        ),
    )

    _install_browser_api_dependencies(app, settings)

    assert isinstance(app.state.browser_resolver, BrowserResolver)
    assert isinstance(app.state.browser_control, BrowserControlStore)

    session_id = "00000000-0000-0000-0000-000000000001"
    await app.state.session_event_emitter(
        session_id,
        EventType.BROWSER_CONTROL_GRANTED,
        {"ok": True},
    )
    await app.state.session_wake(session_id)

    assert store.events == [
        (
            UUID(session_id),
            EventType.BROWSER_CONTROL_GRANTED,
            {"ok": True},
        )
    ]
    assert redis.zadds == [
        ("surogates:work_queue:agent-1", {session_id: 0}),
    ]
