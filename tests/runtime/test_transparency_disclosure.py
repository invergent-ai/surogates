"""The public transparency endpoint resolves agent and deployment settings."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from surogates.api.routes.transparency import router as transparency_router
from surogates.runtime.governance import disclosure_text


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
