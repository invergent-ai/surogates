"""Foundation tests: event types and config settings for the agent browser."""

from __future__ import annotations

import json
import os

import pytest

from surogates.session.events import EventType


def test_browser_event_types_exist() -> None:
    assert EventType.BROWSER_PROVISIONED.value == "browser.provisioned"
    assert EventType.BROWSER_DESTROYED.value == "browser.destroyed"


def test_browser_settings_defaults(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("SUROGATES_BROWSER_"):
            monkeypatch.delenv(key, raising=False)

    from surogates.config import BrowserSettings

    s = BrowserSettings()
    assert not hasattr(s, "enabled")
    assert s.backend == "process"
    assert s.image == "ghcr.io/invergent-ai/surogates-agent-browser:latest"
    assert s.rest_port_base == 30000
    assert s.cdp_port_base == 31000
    assert s.live_view_port_base == 32000
    assert s.k8s_s3fs_image == "ghcr.io/invergent-ai/surogates-s3fs:latest"
    assert s.k8s_s3_endpoint == ""
    assert s.pod_ready_timeout == 60
    # cpu/memory/cpu_limit/memory_limit/active_deadline_seconds/live_view_mode
    # were removed from BrowserSettings: they were never wired into the pod
    # spec (which uses BrowserSpec's own defaults).
    assert not hasattr(s, "cpu")
    assert not hasattr(s, "memory")
    assert not hasattr(s, "active_deadline_seconds")
    assert not hasattr(s, "live_view_mode")


def test_browser_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_BROWSER_REST_PORT_BASE", "40000")

    from surogates.config import BrowserSettings

    s = BrowserSettings()
    assert s.rest_port_base == 40000


def test_settings_includes_browser(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("SUROGATES_"):
            monkeypatch.delenv(key, raising=False)

    from surogates.config import Settings

    s = Settings()
    assert s.browser.backend == "process"


def test_llm_settings_do_not_expose_generation_defaults(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_LLM_TEMPERATURE", "0.2")
    monkeypatch.setenv("SUROGATES_LLM_MAX_TOKENS", "123")

    from surogates.config import LLMSettings

    s = LLMSettings()
    assert not hasattr(s, "temperature")
    assert not hasattr(s, "max_tokens")


def test_llm_settings_advisor_defaults(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("SUROGATES_LLM_ADVISOR_"):
            monkeypatch.delenv(key, raising=False)

    from surogates.config import LLMSettings

    s = LLMSettings()
    assert s.advisor_enabled is False
    assert s.advisor_model == ""
    assert s.advisor_base_url == ""
    assert s.advisor_api_key == ""
    assert s.advisor_max_calls_per_turn == 2
    assert s.advisor_max_tokens == 700


def test_llm_settings_advisor_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SUROGATES_LLM_ADVISOR_ENABLED", "true")
    monkeypatch.setenv("SUROGATES_LLM_ADVISOR_MODEL", "advisor-model")
    monkeypatch.setenv("SUROGATES_LLM_ADVISOR_MAX_CALLS_PER_TURN", "3")

    from surogates.config import LLMSettings

    s = LLMSettings()
    assert s.advisor_enabled is True
    assert s.advisor_model == "advisor-model"
    assert s.advisor_max_calls_per_turn == 3


def test_build_advisor_auxiliary_llm_requires_enabled_and_model() -> None:
    from surogates.config import Settings
    from surogates.harness.auxiliary_client import build_advisor_auxiliary_llm

    settings = Settings()
    settings.llm.advisor_enabled = False
    settings.llm.advisor_model = "advisor-model"
    assert build_advisor_auxiliary_llm(settings) is None

    settings.llm.advisor_enabled = True
    settings.llm.advisor_model = ""
    assert build_advisor_auxiliary_llm(settings) is None


def test_browser_status_values() -> None:
    from surogates.browser.base import BrowserStatus

    assert BrowserStatus.RUNNING.value == "running"
    assert BrowserStatus.PENDING.value == "pending"
    assert BrowserStatus.FAILED.value == "failed"
    assert BrowserStatus.TERMINATED.value == "terminated"


def test_browser_spec_defaults() -> None:
    from surogates.browser.base import BrowserSpec

    spec = BrowserSpec()
    assert spec.image == "ghcr.io/invergent-ai/surogates-agent-browser:latest"
    assert spec.cpu == "1"
    assert spec.memory == "2Gi"
    assert spec.cpu_limit == "2"
    assert spec.memory_limit == "4Gi"
    assert spec.pod_ready_timeout == 60
    assert spec.active_deadline_seconds == 3600
    assert spec.workspace_path is None
    assert spec.workspace_source_ref is None
    assert spec.env == {}


def test_browser_spec_overrides() -> None:
    from surogates.browser.base import BrowserSpec

    spec = BrowserSpec(
        image="custom:1",
        cpu="500m",
        workspace_path="/tmp/workspace",
        workspace_source_ref="s3://bucket/sessions/session-1",
        env={"FOO": "bar"},
    )
    assert spec.image == "custom:1"
    assert spec.cpu == "500m"
    assert spec.workspace_path == "/tmp/workspace"
    assert spec.workspace_source_ref == "s3://bucket/sessions/session-1"
    assert spec.env == {"FOO": "bar"}


def test_browser_unavailable_result_shape() -> None:
    from surogates.browser.base import browser_unavailable_result

    payload = json.loads(browser_unavailable_result("kubelet busy"))
    assert payload["error"] == "browser_unavailable"
    assert payload["reason"] == "kubelet busy"
    assert "guidance" in payload


def test_browser_unavailable_error_classifies() -> None:
    from surogates.browser.base import BrowserUnavailableError

    exc = BrowserUnavailableError("docker pull failed", classification="image")
    assert exc.reason == "docker pull failed"
    assert exc.classification == "image"


def test_browser_endpoint_helpers() -> None:
    from surogates.browser.base import BrowserEndpoint

    ep = BrowserEndpoint(
        rest_url="http://10.0.0.5:30000",
        cdp_url="ws://10.0.0.5:31000",
        live_view_url="ws://10.0.0.5:32000",
    )
    assert ep.rest_url.endswith(":30000")
    with pytest.raises(TypeError):
        BrowserEndpoint(rest_url="http://x")  # type: ignore[call-arg]
