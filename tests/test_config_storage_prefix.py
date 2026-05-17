"""Verify the storage key prefix loads from SUROGATES_STORAGE_KEY_PREFIX."""

import os
from unittest import mock

from surogates.config import StorageSettings


def test_storage_key_prefix_defaults_to_empty():
    cfg = StorageSettings()
    assert cfg.key_prefix == ""


def test_storage_key_prefix_loads_from_env():
    with mock.patch.dict(
        os.environ,
        {"SUROGATES_STORAGE_KEY_PREFIX": "p-123/a-456"},
        clear=False,
    ):
        cfg = StorageSettings()
    assert cfg.key_prefix == "p-123/a-456"
