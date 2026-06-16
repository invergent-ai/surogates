"""Tests for worker model metadata warnings."""

from __future__ import annotations

import logging

from surogates.orchestrator.worker import _warn_if_base_model_missing_from_metadata


def test_warns_when_base_model_missing_from_metadata(caplog):
    caplog.set_level(logging.WARNING, logger="surogates.orchestrator.worker")

    _warn_if_base_model_missing_from_metadata("missing-model")

    rendered = "\n".join(r.getMessage() for r in caplog.records)
    assert "Base LLM model 'missing-model' is not present" in rendered


def test_does_not_warn_for_known_base_model(caplog):
    caplog.set_level(logging.WARNING, logger="surogates.orchestrator.worker")

    _warn_if_base_model_missing_from_metadata("gpt-5.5")

    assert caplog.text == ""
