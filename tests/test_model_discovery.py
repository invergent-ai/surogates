"""Tests for model discovery, resolve chain, and compressor fallback."""

from __future__ import annotations

import logging

import httpx
import pytest

from surogates.harness import model_discovery
from surogates.harness.context import ContextCompressor, _DEFAULT_CONTEXT_WINDOW
from surogates.harness.model_discovery import (
    ModelDiscoveryCache,
    _parse_entry,
    discover_model,
    reset_discovery_cache,
)
from surogates.harness.model_metadata import (
    MODEL_CATALOG,
    ModelInfo,
    resolve_model_info,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_discovery_cache():
    """Reset the module-level cache between tests to isolate mocks."""
    reset_discovery_cache()
    yield
    reset_discovery_cache()


OPENROUTER_SAMPLE = {
    "data": [
        {
            "id": "minimax/minimax-m2.7",
            "name": "MiniMax M2.7",
            "context_length": 204800,
            "pricing": {
                "prompt": "0.0000003",
                "completion": "0.0000012",
            },
            "top_provider": {
                "context_length": 204800,
                "max_completion_tokens": 4096,
            },
        },
        {
            "id": "openai/gpt-5",
            "context_length": 400000,
            "pricing": {"prompt": "0.00001", "completion": "0.00003"},
            "top_provider": {"max_completion_tokens": 16000},
        },
        # Deliberately malformed — missing context_length entirely.
        {"id": "broken/no-context"},
        # Missing id — should be skipped.
        {"context_length": 8192},
    ],
}


def _install_mock(responses: dict[str, httpx.Response]) -> None:
    """Patch ``httpx.get`` to return canned responses keyed by URL."""
    def fake_get(url: str, *, headers=None, timeout=None):
        if url in responses:
            return responses[url]
        return httpx.Response(
            status_code=404, content=b'{"error": "not found"}',
            request=httpx.Request("GET", url),
        )
    model_discovery.httpx.get = fake_get  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Entry parsing
# ---------------------------------------------------------------------------


class TestParseEntry:
    def test_full_entry(self) -> None:
        info = _parse_entry(OPENROUTER_SAMPLE["data"][0])
        assert info is not None
        assert info.id == "minimax/minimax-m2.7"
        assert info.context_window == 204800
        assert info.max_output_tokens == 4096
        # Pricing: 0.0000003 per token -> 0.0003 per 1k
        assert info.input_cost_per_1k == pytest.approx(0.0003)
        assert info.output_cost_per_1k == pytest.approx(0.0012)

    def test_falls_back_to_top_provider_context(self) -> None:
        entry = {"id": "x/y", "top_provider": {"context_length": 128000}}
        info = _parse_entry(entry)
        assert info is not None
        assert info.context_window == 128000
        # No max_completion_tokens → default cap
        assert info.max_output_tokens == 4096

    def test_missing_context_returns_none(self) -> None:
        assert _parse_entry({"id": "x/y"}) is None

    def test_missing_id_returns_none(self) -> None:
        assert _parse_entry({"context_length": 8192}) is None

    def test_non_dict_entry_returns_none(self) -> None:
        assert _parse_entry(["not", "a", "dict"]) is None
        assert _parse_entry(None) is None

    def test_missing_pricing_uses_zero(self) -> None:
        info = _parse_entry({"id": "x/y", "context_length": 8192})
        assert info is not None
        assert info.input_cost_per_1k == 0.0
        assert info.output_cost_per_1k == 0.0

    def test_malformed_pricing_uses_zero(self) -> None:
        entry = {
            "id": "x/y",
            "context_length": 8192,
            "pricing": {"prompt": "not-a-number"},
        }
        info = _parse_entry(entry)
        assert info is not None
        assert info.input_cost_per_1k == 0.0


# ---------------------------------------------------------------------------
# ModelDiscoveryCache
# ---------------------------------------------------------------------------


class TestDiscoveryCache:
    def test_successful_fetch_and_lookup(self) -> None:
        _install_mock({
            "https://provider.example/v1/models": httpx.Response(
                status_code=200, json=OPENROUTER_SAMPLE,
                request=httpx.Request("GET", "https://provider.example/v1/models"),
            ),
        })
        cache = ModelDiscoveryCache()
        info = cache.lookup(
            "minimax/minimax-m2.7",
            base_url="https://provider.example/v1",
            api_key="sk-test",
        )
        assert info is not None
        assert info.context_window == 204800

    def test_lookup_unknown_model_returns_none(self) -> None:
        _install_mock({
            "https://provider.example/v1/models": httpx.Response(
                status_code=200, json=OPENROUTER_SAMPLE,
                request=httpx.Request("GET", "https://provider.example/v1/models"),
            ),
        })
        cache = ModelDiscoveryCache()
        assert cache.lookup(
            "not-in-response",
            base_url="https://provider.example/v1",
            api_key="sk-test",
        ) is None

    def test_empty_base_url_returns_none_without_fetch(self) -> None:
        call_count = {"n": 0}

        def fake_get(*_a, **_k):  # pragma: no cover — should not be called
            call_count["n"] += 1
            raise AssertionError("fetch should not happen when base_url is empty")

        model_discovery.httpx.get = fake_get  # type: ignore[assignment]
        cache = ModelDiscoveryCache()
        assert cache.lookup("x", base_url="", api_key="") is None
        assert call_count["n"] == 0

    def test_cache_is_per_key_and_idempotent(self) -> None:
        call_count = {"n": 0}

        def fake_get(url: str, *, headers=None, timeout=None):
            call_count["n"] += 1
            return httpx.Response(
                status_code=200, json=OPENROUTER_SAMPLE,
                request=httpx.Request("GET", url),
            )

        model_discovery.httpx.get = fake_get  # type: ignore[assignment]
        cache = ModelDiscoveryCache()

        # Two lookups on the same (base_url, key) — one fetch.
        cache.lookup("minimax/minimax-m2.7", base_url="http://a", api_key="k")
        cache.lookup("openai/gpt-5", base_url="http://a", api_key="k")
        assert call_count["n"] == 1

        # Different base_url → second fetch.
        cache.lookup("minimax/minimax-m2.7", base_url="http://b", api_key="k")
        assert call_count["n"] == 2

    def test_network_failure_is_cached_as_empty(self, caplog) -> None:
        def fake_get(*_a, **_k):
            raise httpx.ConnectError("dns fail")

        model_discovery.httpx.get = fake_get  # type: ignore[assignment]
        cache = ModelDiscoveryCache()
        with caplog.at_level(logging.WARNING, logger="surogates.harness.model_discovery"):
            assert cache.lookup(
                "any", base_url="http://broken", api_key="k",
            ) is None
        assert any(
            "Model discovery failed" in rec.getMessage() for rec in caplog.records
        )

        # Second call does not retry (cached as empty dict).
        call_count = {"n": 0}

        def counting_get(*_a, **_k):
            call_count["n"] += 1
            raise httpx.ConnectError("still broken")

        model_discovery.httpx.get = counting_get  # type: ignore[assignment]
        cache.lookup("any", base_url="http://broken", api_key="k")
        assert call_count["n"] == 0

    def test_http_error_is_cached_as_empty(self) -> None:
        _install_mock({})  # all URLs 404
        cache = ModelDiscoveryCache()
        assert cache.lookup(
            "any", base_url="https://nothing.here/v1", api_key="k",
        ) is None

    def test_malformed_payload_returns_empty(self) -> None:
        _install_mock({
            "http://bad/models": httpx.Response(
                status_code=200, json={"not_data": []},
                request=httpx.Request("GET", "http://bad/models"),
            ),
        })
        cache = ModelDiscoveryCache()
        assert cache.lookup("x", base_url="http://bad", api_key="k") is None


# ---------------------------------------------------------------------------
# resolve_model_info chain
# ---------------------------------------------------------------------------


class TestResolveModelInfo:
    def test_static_catalog_wins_for_known_id(self) -> None:
        # gpt-4o is in the static catalog.
        info = resolve_model_info("gpt-4o")
        assert info is not None
        assert info.context_window == MODEL_CATALOG["gpt-4o"].context_window

    def test_discovery_fills_gap_when_static_misses(self) -> None:
        _install_mock({
            "https://provider.example/v1/models": httpx.Response(
                status_code=200, json=OPENROUTER_SAMPLE,
                request=httpx.Request("GET", "https://provider.example/v1/models"),
            ),
        })
        info = resolve_model_info(
            "minimax/minimax-m2.7",
            base_url="https://provider.example/v1",
            api_key="sk-test",
        )
        assert info is not None
        assert info.context_window == 204800

    def test_override_beats_static(self) -> None:
        info = resolve_model_info(
            "gpt-4o",
            overrides={"gpt-4o": {"context_window": 999_000}},
        )
        assert info is not None
        assert info.context_window == 999_000
        # Other fields preserved from the static entry.
        assert info.input_cost_per_1k == MODEL_CATALOG["gpt-4o"].input_cost_per_1k

    def test_override_beats_discovery(self) -> None:
        _install_mock({
            "https://provider.example/v1/models": httpx.Response(
                status_code=200, json=OPENROUTER_SAMPLE,
                request=httpx.Request("GET", "https://provider.example/v1/models"),
            ),
        })
        info = resolve_model_info(
            "minimax/minimax-m2.7",
            base_url="https://provider.example/v1",
            api_key="sk-test",
            overrides={"minimax/minimax-m2.7": {"context_window": 1_000_000}},
        )
        assert info is not None
        assert info.context_window == 1_000_000

    def test_override_only_builds_skeleton(self) -> None:
        info = resolve_model_info(
            "some/unknown",
            overrides={"some/unknown": {"context_window": 32_000}},
        )
        assert info is not None
        assert info.id == "some/unknown"
        assert info.context_window == 32_000

    def test_all_sources_miss_returns_none(self) -> None:
        _install_mock({})  # 404 for every URL
        assert resolve_model_info(
            "totally/unknown",
            base_url="https://nothing.here/v1",
            api_key="k",
        ) is None

    def test_no_base_url_skips_discovery(self) -> None:
        # Unknown model with no base_url — nothing to try.
        assert resolve_model_info("totally/unknown") is None


# ---------------------------------------------------------------------------
# ContextCompressor fallback integration
# ---------------------------------------------------------------------------


class TestCompressorFallback:
    def test_warns_and_falls_back_when_model_unknown(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="surogates.harness.context"):
            compressor = ContextCompressor("totally/unknown")
        assert compressor.context_length == _DEFAULT_CONTEXT_WINDOW
        assert any(
            "totally/unknown" in rec.getMessage()
            and "fall" in rec.getMessage().lower()
            for rec in caplog.records
        ), f"expected fallback warning, got: {[r.getMessage() for r in caplog.records]}"

    def test_discovery_avoids_fallback(self, caplog) -> None:
        _install_mock({
            "https://provider.example/v1/models": httpx.Response(
                status_code=200, json=OPENROUTER_SAMPLE,
                request=httpx.Request("GET", "https://provider.example/v1/models"),
            ),
        })
        with caplog.at_level(logging.WARNING, logger="surogates.harness.context"):
            compressor = ContextCompressor(
                "minimax/minimax-m2.7",
                base_url="https://provider.example/v1",
                api_key="sk-test",
            )
        assert compressor.context_length == 204800
        # No fallback warning was logged.
        assert not any(
            "falling back" in rec.getMessage().lower() for rec in caplog.records
        )

    def test_override_avoids_fallback(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="surogates.harness.context"):
            compressor = ContextCompressor(
                "totally/unknown",
                model_overrides={"totally/unknown": {"context_window": 256_000}},
            )
        assert compressor.context_length == 256_000
        assert not any(
            "falling back" in rec.getMessage().lower() for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Module-level discover_model accessor
# ---------------------------------------------------------------------------


class TestDiscoverModel:
    def test_uses_module_singleton(self) -> None:
        _install_mock({
            "https://provider.example/v1/models": httpx.Response(
                status_code=200, json=OPENROUTER_SAMPLE,
                request=httpx.Request("GET", "https://provider.example/v1/models"),
            ),
        })
        info = discover_model(
            "openai/gpt-5",
            base_url="https://provider.example/v1",
            api_key="sk-test",
        )
        assert info is not None
        assert info.context_window == 400000
        assert isinstance(info, ModelInfo)
