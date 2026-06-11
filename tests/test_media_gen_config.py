"""Tests for image/video generation settings on LLMSettings."""

from __future__ import annotations


def test_llm_settings_media_defaults():
    from surogates.config import LLMSettings

    s = LLMSettings()
    assert s.image_model == ""
    assert s.image_base_url == ""
    assert s.image_api_key == ""
    assert s.video_model == ""
    assert s.video_base_url == ""
    assert s.video_api_key == ""
    assert s.video_timeout == 600
    assert s.video_poll_interval == 10


def test_llm_settings_media_env_overrides(monkeypatch):
    from surogates.config import LLMSettings

    monkeypatch.setenv("SUROGATES_LLM_IMAGE_MODEL", "google/gemini-2.5-flash-image")
    monkeypatch.setenv("SUROGATES_LLM_VIDEO_MODEL", "google/veo-3.1")
    monkeypatch.setenv("SUROGATES_LLM_VIDEO_TIMEOUT", "900")

    s = LLMSettings()
    assert s.image_model == "google/gemini-2.5-flash-image"
    assert s.video_model == "google/veo-3.1"
    assert s.video_timeout == 900
