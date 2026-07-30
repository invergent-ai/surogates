"""Per-agent AI disclosure: endpoint resolution + channel delivery.

Covers the Art. 50 chain: the public transparency endpoint resolving
per-agent config (agent beats deployment, graceful fallbacks), the
disclosure text helper, and the inbound pipeline's first-contact
disclosure delivery with its ``disclosure.presented`` audit event.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from surogates.api.routes.transparency import router as transparency_router
from surogates.channels.inbound import ChannelInboundPipeline
from surogates.runtime.governance import disclosure_text
from surogates.session.events import EventType


# ---------------------------------------------------------------------------
# disclosure_text
# ---------------------------------------------------------------------------


def test_disclosure_text_levels():
    assert disclosure_text("none") == ""
    assert "AI assistant" in disclosure_text("basic")
    assert "Art. 50" in disclosure_text("full")
    # Unknown levels degrade to the basic text, never to silence.
    assert disclosure_text("partial") == disclosure_text("basic")
    # No level self-classifies the system as high-risk.
    for level in ("basic", "enhanced", "full"):
        assert "high-risk" not in disclosure_text(level)


# ---------------------------------------------------------------------------
# GET /transparency
# ---------------------------------------------------------------------------


class _FakeCache:
    def __init__(self, payloads: dict[str, dict]):
        self._payloads = payloads

    async def get(self, agent_id: str) -> dict:
        try:
            return self._payloads[agent_id]
        except KeyError:
            raise LookupError(agent_id)


def _make_client(
    *,
    payloads: dict[str, dict] | None = None,
    deployment_enabled: bool = False,
    deployment_level: str = "basic",
) -> TestClient:
    app = FastAPI()
    app.include_router(transparency_router)
    app.state.settings = SimpleNamespace(
        governance=SimpleNamespace(
            transparency=SimpleNamespace(
                enabled=deployment_enabled, level=deployment_level,
            ),
        ),
    )
    app.state.runtime_config_cache = _FakeCache(payloads or {})
    return TestClient(app)


def test_agent_transparency_wins_and_carries_text():
    client = _make_client(payloads={
        "agent-1": {
            "governance": {
                "transparency": {"enabled": True, "level": "full"},
            },
        },
    })
    body = client.get("/transparency?agent_id=agent-1").json()
    assert body["enabled"] is True
    assert body["level"] == "full"
    assert "Art. 50" in body["text"]


def test_agent_explicit_disabled_beats_deployment_enabled():
    client = _make_client(
        payloads={
            "agent-1": {
                "governance": {
                    "transparency": {"enabled": False, "level": "full"},
                },
            },
        },
        deployment_enabled=True,
    )
    assert client.get("/transparency?agent_id=agent-1").json() == {
        "enabled": False,
    }


def test_agent_without_block_falls_back_to_deployment():
    client = _make_client(
        payloads={"agent-1": {"governance": {"enabled": True}}},
        deployment_enabled=True,
        deployment_level="basic",
    )
    body = client.get("/transparency?agent_id=agent-1").json()
    assert body["enabled"] is True
    assert body["level"] == "basic"
    assert body["text"]


def test_unknown_agent_falls_back_not_errors():
    client = _make_client(deployment_enabled=False)
    assert client.get("/transparency?agent_id=ghost").json() == {
        "enabled": False,
    }


def test_master_switch_off_falls_back_to_deployment():
    client = _make_client(
        payloads={
            "agent-1": {
                "governance": {
                    "enabled": False,
                    "transparency": {"enabled": True, "level": "full"},
                },
            },
        },
        deployment_enabled=True,
        deployment_level="basic",
    )
    body = client.get("/transparency?agent_id=agent-1").json()
    assert body["enabled"] is True
    assert body["level"] == "basic"


def test_no_agent_uses_deployment_setting():
    client = _make_client(deployment_enabled=True, deployment_level="enhanced")
    body = client.get("/transparency").json()
    assert body == {
        "enabled": True,
        "level": "enhanced",
        "text": disclosure_text("enhanced"),
    }


# ---------------------------------------------------------------------------
# Channel first-contact disclosure
# ---------------------------------------------------------------------------


def _disclosure_deps(
    *,
    governance: dict | None,
    message_count: int = 0,
) -> SimpleNamespace:
    store = SimpleNamespace(
        get_session=AsyncMock(
            return_value=SimpleNamespace(message_count=message_count),
        ),
        emit_event=AsyncMock(return_value=1),
    )
    nudges: list[str] = []

    async def _nudge(session_id, msg, text):
        nudges.append(text)

    deps = SimpleNamespace(session_store=store, input_nudge=_nudge)
    deps._nudges = nudges
    # The pipeline resolves the runtime payload once per message and
    # hands it to the hook, so the fixture supplies it directly.
    deps._payload = (
        {"governance": governance} if governance is not None else {}
    )
    return deps


def _msg() -> SimpleNamespace:
    return SimpleNamespace(identifier="C1", thread_key=None)


def _routing() -> SimpleNamespace:
    return SimpleNamespace(agent_id="agent-1", platform="telegram")


@pytest.mark.asyncio
async def test_first_contact_sends_disclosure_and_emits_event():
    deps = _disclosure_deps(
        governance={"transparency": {"enabled": True, "level": "basic"}},
    )
    await ChannelInboundPipeline._maybe_send_disclosure(
        _msg(), routing=_routing(), deps=deps, session_id=uuid4(),
        runtime_payload=deps._payload,
    )
    assert deps._nudges == [disclosure_text("basic")]
    event_call = deps.session_store.emit_event.call_args
    assert event_call.args[1] is EventType.DISCLOSURE_PRESENTED
    assert event_call.args[2]["level"] == "basic"
    assert event_call.args[2]["channel"] == "telegram"


@pytest.mark.asyncio
async def test_no_disclosure_on_established_conversation():
    deps = _disclosure_deps(
        governance={"transparency": {"enabled": True, "level": "basic"}},
        message_count=7,
    )
    await ChannelInboundPipeline._maybe_send_disclosure(
        _msg(), routing=_routing(), deps=deps, session_id=uuid4(),
        runtime_payload=deps._payload,
    )
    assert deps._nudges == []
    deps.session_store.emit_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_disclosure_when_transparency_disabled_or_absent():
    for governance in (None, {}, {"transparency": {"enabled": False}}):
        deps = _disclosure_deps(governance=governance)
        await ChannelInboundPipeline._maybe_send_disclosure(
            _msg(), routing=_routing(), deps=deps, session_id=uuid4(),
            runtime_payload=deps._payload,
        )
        assert deps._nudges == []


@pytest.mark.asyncio
async def test_disclosure_failure_never_raises():
    deps = _disclosure_deps(
        governance={"transparency": {"enabled": True, "level": "basic"}},
    )
    deps.session_store.get_session = AsyncMock(side_effect=RuntimeError("db"))
    await ChannelInboundPipeline._maybe_send_disclosure(
        _msg(), routing=_routing(), deps=deps, session_id=uuid4(),
        runtime_payload=deps._payload,
    )
    assert deps._nudges == []


@pytest.mark.asyncio
async def test_disclosure_skipped_without_wiring():
    """No nudge seam, or no resolvable config, means no attempt."""
    deps = SimpleNamespace(input_nudge=None)
    await ChannelInboundPipeline._maybe_send_disclosure(
        _msg(), routing=_routing(), deps=deps, session_id=uuid4(),
        runtime_payload={"governance": {"transparency": {"enabled": True}}},
    )
    deps = _disclosure_deps(
        governance={"transparency": {"enabled": True, "level": "basic"}},
    )
    await ChannelInboundPipeline._maybe_send_disclosure(
        _msg(), routing=_routing(), deps=deps, session_id=uuid4(),
        runtime_payload=None,
    )
    assert deps._nudges == []
