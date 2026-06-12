"""Tests for surogates.sandbox.executor_server — the persistent in-pod daemon."""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from surogates.sandbox import executor_server


# ---------------------------------------------------------------------------
# workspace_mounted
# ---------------------------------------------------------------------------


class TestWorkspaceMounted:
    def test_fuse_mount_detected(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text(
            "overlay / overlay rw 0 0\n"
            "geesefs /workspace fuse.geesefs rw,nosuid,nodev 0 0\n"
        )
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is True

    def test_plain_bind_mount_is_not_enough(self, tmp_path):
        # The emptyDir volumeMount makes /workspace a mount point with a
        # non-FUSE fstype — that must NOT count as "geesefs is up".
        mounts = tmp_path / "mounts"
        mounts.write_text(
            "overlay / overlay rw 0 0\n"
            "/dev/sda1 /workspace ext4 rw 0 0\n"
        )
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is False

    def test_no_entry(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("overlay / overlay rw 0 0\n")
        assert executor_server.workspace_mounted("/workspace", str(mounts)) is False

    def test_trailing_slash_normalized(self, tmp_path):
        mounts = tmp_path / "mounts"
        mounts.write_text("geesefs /workspace fuse.geesefs rw 0 0\n")
        assert executor_server.workspace_mounted("/workspace/", str(mounts)) is True

    def test_unreadable_mounts_file(self):
        assert executor_server.workspace_mounted("/workspace", "/nonexistent") is False
