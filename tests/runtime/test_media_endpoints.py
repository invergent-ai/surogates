"""Tests for the llm_image / llm_video runtime-context slots."""

from __future__ import annotations

from surogates.runtime.resolver import build_agent_runtime_context


def _payload(**extra):
    base = {
        "agent_id": "agent-1",
        "org_id": "org-1",
        "project_id": "proj-1",
        "enabled": True,
        "version": 1,
        "storage_key_prefix": "org-1/agent-1",
    }
    base.update(extra)
    return base


def test_build_context_parses_image_and_video_slots():
    ctx = build_agent_runtime_context(_payload(
        llm_image={
            "model": "google/gemini-2.5-flash-image",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_ref": "vault://img-key",
        },
        llm_video={
            "model": "google/veo-3.1",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_ref": "vault://vid-key",
        },
    ))
    assert ctx.llm_image is not None
    assert ctx.llm_image.model == "google/gemini-2.5-flash-image"
    assert ctx.llm_video is not None
    assert ctx.llm_video.api_key_ref == "vault://vid-key"


def test_build_context_defaults_image_and_video_to_none():
    ctx = build_agent_runtime_context(_payload())
    assert ctx.llm_image is None
    assert ctx.llm_video is None
