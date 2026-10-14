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


# ---------------------------------------------------------------------------
# Language and custom text
# ---------------------------------------------------------------------------


def _cfg(**transparency):
    from surogates.runtime.governance import disclosure_config

    return disclosure_config({"transparency": {"enabled": True, **transparency}})


def test_every_language_has_every_level():
    from surogates.governance.transparency import (
        DISCLOSURE_LANGUAGES,
        DISCLOSURE_TRANSLATIONS,
        TransparencyLevel,
    )

    assert set(DISCLOSURE_LANGUAGES) >= {"en", "ro", "hu", "de", "fr", "it", "es"}
    levels = {level for level in TransparencyLevel if level is not TransparencyLevel.NONE}
    for language in DISCLOSURE_LANGUAGES:
        assert set(DISCLOSURE_TRANSLATIONS[language]) == levels, language
        assert all("50" in text for text in DISCLOSURE_TRANSLATIONS[language].values())


def test_language_picks_the_translation():
    text = _cfg(level="basic", language="ro")["text"]
    assert text.startswith("Vorbiți cu un asistent bazat pe inteligență artificială.")
    assert "art. 50 alin. (1)" in text


def test_region_suffix_and_case_are_ignored():
    assert _cfg(level="enhanced", language="DE-at")["text"] == _cfg(
        level="enhanced", language="de",
    )["text"]


def test_unknown_or_missing_language_is_english():
    english = _cfg(level="full")["text"]
    assert english == disclosure_text("full")
    assert _cfg(level="full", language="xx")["text"] == english


def test_custom_text_replaces_the_level_copy():
    cfg = _cfg(level="enhanced", language="ro", text="  Sunt asistentul virtual al cabinetului.  ")
    assert cfg == {
        "enabled": True,
        "level": "enhanced",
        "text": "Sunt asistentul virtual al cabinetului.",
    }


def test_blank_custom_text_keeps_the_default():
    assert _cfg(level="basic", text="   ")["text"] == disclosure_text("basic")


def test_custom_text_does_not_override_level_none_or_disabled():
    assert _cfg(level="none", text="Hi")["text"] == ""
    from surogates.runtime.governance import disclosure_config

    assert disclosure_config(
        {"transparency": {"enabled": False, "text": "Hi"}},
    )["text"] == ""
