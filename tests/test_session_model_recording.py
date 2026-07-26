"""The session's model is recorded from the resolved bundle, not config.

Model selection belongs to the management plane: it arrives as the
per-agent runtime config's ``llm_main`` slot, the worker resolves it into
the session's LLM bundle, and the row records what actually ran. A
deployment-level ``llm.model`` setting could only ever disagree with that,
so it must not exist — these tests pin both halves of that contract.
"""

from __future__ import annotations

import pytest

from surogates.config import LLMSettings


def test_llm_settings_has_no_model_field():
    """A deployment-level model would be a second source of truth."""
    assert "model" not in LLMSettings.model_fields, (
        "LLMSettings.model is back; model selection belongs to the "
        "per-agent runtime config, and a settings-level model silently "
        "disagrees with the model the session actually runs on"
    )


def test_a_config_still_setting_model_fails_with_a_fixable_message():
    """A stale key must not be silently dropped — nor crash cryptically.

    Deployments carrying ``llm.model`` (the PROD runtime configmap did)
    have to be edited; the error has to say so.
    """
    with pytest.raises(Exception) as exc:
        LLMSettings(model="qwen3.7-max")
    message = str(exc.value)
    assert "no longer supported" in message
    assert "llm_main" in message


def test_llm_settings_still_carries_the_shared_upstream():
    """Endpoint/key stay: they serve model-metadata lookup and media roles."""
    assert "base_url" in LLMSettings.model_fields
    assert "api_key" in LLMSettings.model_fields


@pytest.mark.asyncio
async def test_record_session_model_writes_the_resolved_model():
    from uuid import uuid4

    from surogates.session.store import SessionStore

    captured: list[object] = []

    class _DB:
        async def execute(self, stmt):
            captured.append(stmt)

        async def commit(self):
            captured.append("commit")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    store = SessionStore.__new__(SessionStore)
    store._sf = lambda: _DB()

    await store.record_session_model(uuid4(), "surogate-pro")

    assert "commit" in captured
    compiled = str(captured[0].compile(compile_kwargs={"literal_binds": True}))
    assert "model" in compiled
    assert "surogate-pro" in compiled


@pytest.mark.asyncio
async def test_record_session_model_ignores_an_empty_model():
    """Nothing to record — must not issue a write that NULLs the column."""
    from uuid import uuid4

    from surogates.session.store import SessionStore

    class _ExplodingDB:
        async def __aenter__(self):
            raise AssertionError("must not open a session for an empty model")

        async def __aexit__(self, *exc):
            return False

    store = SessionStore.__new__(SessionStore)
    store._sf = lambda: _ExplodingDB()

    await store.record_session_model(uuid4(), "")


def test_both_platform_tiers_have_model_metadata():
    """Missing metadata makes the compressor mis-size the context window.

    The tiers are sentinels rewritten upstream by the proxy, so they never
    match a provider model name — each needs its own catalog entry.
    """
    from surogates.harness.model_metadata import get_model_info

    for tier in ("surogate", "surogate-pro"):
        info = get_model_info(tier)
        assert info is not None, f"{tier} has no model metadata entry"
        assert info.context_window > 0
