"""What a wake builds once per process instead of once per wake.

The TLS context of the wake's HTTP clients (``harness.tls``) and the
settings the loop reads at a turn's end (``config.load_settings``): each
cost about 12 ms of CPU on the worker's event loop, several times a wake.
"""

from __future__ import annotations

import os
import shutil

import certifi
import httpx
import pytest
from openai._constants import DEFAULT_CONNECTION_LIMITS, DEFAULT_TIMEOUT

from surogates import config
from surogates.harness import tls
from surogates.harness.api_client import HarnessAPIClient
from surogates.harness.session_llm import build_session_llm_clients
from surogates.runtime.resolver import build_agent_runtime_context


def _context_of(client: httpx.AsyncClient):
    return client._transport._pool._ssl_context


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(tls, "_cached", None)
    monkeypatch.setattr(config, "_settings_cache", None)


# ---------------------------------------------------------------------------
# One TLS context
# ---------------------------------------------------------------------------


def test_the_context_is_built_once_and_verifies_as_httpx_does():
    first, second = tls.shared_ssl_context(), tls.shared_ssl_context()
    assert first is second
    default = httpx.create_ssl_context()
    assert (first.verify_mode, first.check_hostname) == (default.verify_mode, default.check_hostname)
    assert len(first.get_ca_certs()) == len(default.get_ca_certs()) > 0


def test_a_changed_bundle_or_bundle_setting_builds_a_new_one(tmp_path, monkeypatch):
    bundle = tmp_path / "ca.pem"
    shutil.copy(certifi.where(), bundle)
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    before = tls.shared_ssl_context()
    assert tls.shared_ssl_context() is before
    # A rotated bundle: replaced on disk (a new file, as a mounted secret is).
    rotated = tmp_path / "ca.new"
    shutil.copy(certifi.where(), rotated)
    with open(rotated, "ab") as f:
        f.write(b"\n")
    os.replace(rotated, bundle)
    after = tls.shared_ssl_context()
    assert after is not before
    monkeypatch.delenv("SSL_CERT_FILE")
    assert tls.shared_ssl_context() is not after


async def test_the_api_client_uses_it():
    client = HarnessAPIClient("http://api.invalid", token="t", session_id="s")
    try:
        assert _context_of(client._client) is tls.shared_ssl_context()
        assert client._client.headers["Authorization"] == "Bearer t"
    finally:
        await client.close()


def _payload() -> dict:
    endpoint = {"model": "m", "base_url": "https://proxy.invalid/_model/m/v1", "api_key_ref": "vault://k"}
    return {
        "agent_id": "a", "org_id": "o", "project_id": "p", "enabled": True,
        "version": 1, "storage_key_prefix": "",
        "llm_main": endpoint, "llm_summary": {**endpoint, "model": "s"},
    }


class _Vault:
    def __init__(self, key: str):
        self.key = key

    async def resolve_ref(self, ref, **_scope):
        return self.key


async def test_every_slot_of_every_wake_uses_it_with_its_own_key():
    ctx = build_agent_runtime_context(_payload())
    first = await build_session_llm_clients(ctx, vault=_Vault("sk-before"))
    # The key rotated between two wakes.
    second = await build_session_llm_clients(ctx, vault=_Vault("sk-after"))
    try:
        shared = tls.shared_ssl_context()
        for bundle, key in ((first, "sk-before"), (second, "sk-after")):
            for slot in (bundle.main, bundle.summary):
                assert _context_of(slot.client._client) is shared
                assert slot.client.api_key == key
                # openai's own defaults for the client it would have made.
                assert slot.client.timeout == DEFAULT_TIMEOUT
                assert slot.client._client.timeout == DEFAULT_TIMEOUT
                assert slot.client._client.follow_redirects is True
                assert slot.client._client._transport._pool._max_connections == (
                    DEFAULT_CONNECTION_LIMITS.max_connections
                )
        assert first.main.client._client is not second.main.client._client
    finally:
        await first.aclose()
        await second.aclose()
    assert first.main.client._client.is_closed


async def test_a_slot_client_left_open_is_closed_when_collected():
    """As openai's own client is: its finaliser schedules the close on the running loop."""
    import asyncio

    client = tls.llm_http_client()
    client.__del__()
    for _ in range(20):
        if client.is_closed:
            break
        await asyncio.sleep(0)
    assert client.is_closed


# ---------------------------------------------------------------------------
# Settings read again only when they changed
# ---------------------------------------------------------------------------


@pytest.fixture
def counted(monkeypatch):
    built = []
    real = config.Settings

    def build():
        built.append(1)
        return real()

    monkeypatch.setattr(config, "Settings", build)
    return built


def test_unchanged_settings_are_not_built_again(counted, monkeypatch, tmp_path):
    monkeypatch.setenv("SUROGATES_CONFIG", str(tmp_path / "none.yaml"))
    first = config.load_settings()
    second = config.load_settings()
    assert len(counted) == 1
    assert first == second


def test_each_caller_gets_its_own_copy(counted, monkeypatch, tmp_path):
    monkeypatch.setenv("SUROGATES_CONFIG", str(tmp_path / "none.yaml"))
    first = config.load_settings()
    first.worker_id = "changed"
    first.tool_output.max_bytes = 1
    second = config.load_settings()
    assert second.worker_id != "changed"
    assert second.tool_output.max_bytes != 1


def test_a_changed_environment_variable_is_read(counted, monkeypatch, tmp_path):
    monkeypatch.setenv("SUROGATES_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("SUROGATES_LOG_LEVEL", "INFO")
    assert config.load_settings().log_level == "INFO"
    monkeypatch.setenv("SUROGATES_LOG_LEVEL", "DEBUG")
    assert config.load_settings().log_level == "DEBUG"
    # Matched without regard to case, as pydantic-settings matches it.
    monkeypatch.delenv("SUROGATES_LOG_LEVEL")
    monkeypatch.setenv("surogates_log_level", "WARNING")
    assert config.load_settings().log_level == "WARNING"
    assert len(counted) == 3


def test_a_changed_config_file_is_read_even_with_the_same_size_and_time(counted, monkeypatch, tmp_path):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("SUROGATES_CONFIG", str(path))
    for name in ("SUROGATES_TOOL_OUTPUT_MAX_BYTES", "SUROGATES_TOOL_OUTPUT_MAX_LINES"):
        monkeypatch.delenv(name, raising=False)
    path.write_text("tool_output:\n  max_bytes: 11111\n")
    stamp = os.stat(path).st_mtime_ns
    try:
        assert config.load_settings().tool_output.max_bytes == 11111
        # The same size, the same mtime: only the bytes differ.  The first
        # load put the file's value in the environment, as it always has,
        # so a new key shows the change.
        path.write_text("tool_output:\n  max_lines: 22222\n")
        os.utime(path, ns=(stamp, stamp))
        assert config.load_settings().tool_output.max_lines == 22222
        assert len(counted) == 2
    finally:
        for name in ("SUROGATES_TOOL_OUTPUT_MAX_BYTES", "SUROGATES_TOOL_OUTPUT_MAX_LINES"):
            os.environ.pop(name, None)
