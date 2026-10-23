"""The context window ops declares on an endpoint reaches the compressor.

A builder's own model is unknown to the static catalog; without the
endpoint's window the compressor sized it at the 128k fallback and the
chat UI showed 128k for a 1M model.
"""

from __future__ import annotations

import pytest

from surogates.harness.context import _DEFAULT_CONTEXT_WINDOW, ContextCompressor
from surogates.harness.session_llm import build_session_llm_clients
from surogates.runtime.resolver import build_agent_runtime_context


def _payload(**llm_main) -> dict:
    return {
        "agent_id": "a", "org_id": "o", "project_id": "p", "enabled": True,
        "version": 1, "storage_key_prefix": "",
        "llm_main": {
            "model": "surogate3.7",
            "base_url": "http://proxy/_model/m/v1",
            "api_key_ref": "vault://k",
            **llm_main,
        },
    }


class _Vault:
    async def resolve_ref(self, ref, **_scope):
        return "sk-test"


async def test_endpoint_window_reaches_the_main_slot():
    ctx = build_agent_runtime_context(_payload(context_window=1_048_576))
    bundle = await build_session_llm_clients(ctx, vault=_Vault())
    try:
        assert bundle.main.context_window == 1_048_576
    finally:
        await bundle.aclose()


def test_payload_without_window_projects_none():
    ctx = build_agent_runtime_context(_payload())
    assert ctx.llm_main.context_window is None


@pytest.mark.parametrize(
    ("model", "window", "expected"),
    [
        ("surogate3.7", 1_048_576, 1_048_576),  # unknown model, endpoint window
        ("surogate3.7", None, _DEFAULT_CONTEXT_WINDOW),  # unknown, nothing declared
        ("surogate-pro", 100_000, 100_000),  # endpoint beats the catalog
        ("surogate-pro", None, 1_000_000),  # catalog when nothing declared
    ],
)
def test_compressor_window(model, window, expected):
    comp = ContextCompressor(model, quiet_mode=True, context_window=window)
    assert comp.context_length == expected
