"""Tests for StorageSettings.memory_bucket.

Optional dedicated bucket for per-user memory
(some deployments isolate memory in a separate R2 bucket for
billing / replication policy).  Defaults to '' which the harness
treats as 'reuse settings.storage.bucket'.
"""

from __future__ import annotations

from surogates.config import StorageSettings


def test_memory_bucket_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("SUROGATES_STORAGE_MEMORY_BUCKET", raising=False)
    s = StorageSettings()
    assert s.memory_bucket == ""


def test_memory_bucket_reads_env(monkeypatch):
    monkeypatch.setenv(
        "SUROGATES_STORAGE_MEMORY_BUCKET", "surogates-memory-prod",
    )
    s = StorageSettings()
    assert s.memory_bucket == "surogates-memory-prod"
